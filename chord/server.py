"""
Flask HTTP server exposing all Chord RPC endpoints and the public data/job API.
"""

import os
import sys
import json
import random
import threading
import time
import logging
import pathlib
import subprocess
import requests as _requests
from typing import List, Dict, Set, Optional
from flask import Flask, request, jsonify, send_from_directory
from chord.node import ChordNode, sha1_id
from chord.transport import HttpTransport
from chord.job import make_job, job_key, ACTIVE_STATUSES, PENDING
from chord.dummy_client import file_type
import chord.activity as activity
from chord.metrics_registry import (
    FILE_REQUESTS, FILE_REQUEST_HOPS, FILE_REQUEST_DURATION,
    QUEUE_DEPTH, RING_SIZE, DATA_KEYS, STABILIZE_RUNS,
    FINGER_FIX_RUNS, PREDECESSOR_FAILURES,
)
from storage.task_service import (
    TaskConflictError,
    TaskNotFoundError,
    TaskService,
    TaskValidationError,
)
from chord.worker_registry import WorkerRegistry
from chord.conversation_agent import ConversationAgent, ScriptedAgent
from chord.retry import with_timeout_and_retry
from storage.result_record import build_result_record, ResultValidationError
from storage.result_service import ResultService
from storage.worker_assignment import WorkerAssignmentService
from chord.recovery import RecoveryManager

logger = logging.getLogger(__name__)

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


# In-memory request log — last 100 entries, shared across all threads
_request_log: List[Dict] = []
_request_lock = threading.Lock()
_request_log_path = pathlib.Path(os.environ.get("REQUEST_LOG_PATH", "request_log.jsonl"))


def create_app(node: ChordNode) -> Flask:
    global _request_log
    app = Flask(__name__)
    app.config["node"] = node

    # Load persisted request log on startup
    if _request_log_path.exists():
        try:
            lines = _request_log_path.read_text().strip().splitlines()
            for line in lines[-100:]:  # Keep last 100 entries
                try:
                    _request_log.append(json.loads(line))
                except Exception:
                    pass
            logger.info(f"[Server] Loaded {len(_request_log)} persisted request log entries")
        except Exception as e:
            logger.warning(f"[Server] Failed to load request log: {e}")
    task_service = TaskService(node=node, transport=node._transport)

    # Phase 2: live worker registry + conversational agent.  Storage is
    # stubbed in this phase — actual DHT writes land in Phase 3.
    # HEARTBEAT_TIMEOUT_S env var controls expiration; <=0 disables it entirely.
    _hb_timeout = float(os.environ.get("HEARTBEAT_TIMEOUT_S", "10"))
    worker_registry = WorkerRegistry(heartbeat_timeout_s=_hb_timeout)
    app.config["worker_registry"] = worker_registry

    # Phase 3: HT1 (result:<task_id>) and HT2 (worker:<worker_id>) services.
    # Both replicate k=3 quorum like Phase 1's task_service, on the same ring.
    result_service = ResultService(node=node, transport=node._transport, replication_k=3)
    worker_assignment_service = WorkerAssignmentService(
        node=node, transport=node._transport, replication_k=3,
    )
    app.config["result_service"] = result_service
    app.config["worker_assignment_service"] = worker_assignment_service

    # Phase 7: Recovery manager daemon for intelligent failure recovery.
    recovery_manager = RecoveryManager(result_service, worker_assignment_service, worker_registry)
    app.config["recovery_manager"] = recovery_manager
    recovery_manager.start()
    logger.info("[Server] RecoveryManager started")

    _conv_agent_holder: Dict[str, ConversationAgent] = {}

    def _get_conversation_agent():
        if "agent" not in _conv_agent_holder:
            base_url = f"http://{node.address}"
            api_key = app.config.get("agent_key") or os.environ.get("ANTHROPIC_API_KEY")
            mode = (os.environ.get("CONVERSATION_AGENT_MODE") or "").lower()
            if mode == "scripted" or not api_key:
                logger.info("[ConversationAgent] using ScriptedAgent (no API key set or mode=scripted)")
                _conv_agent_holder["agent"] = ScriptedAgent(base_url=base_url)
            else:
                _conv_agent_holder["agent"] = ConversationAgent(
                    api_key=api_key, base_url=base_url,
                )
        return _conv_agent_holder["agent"]

    # ------------------------------------------------------------------
    # Chord internal RPC endpoints
    # ------------------------------------------------------------------

    @app.get("/chord/find_successor")
    def find_successor():
        key_id = int(request.args.get("id"))
        result = node.find_successor(key_id)
        return jsonify(result)

    @app.get("/chord/predecessor")
    def get_predecessor():
        pred = node.predecessor
        if pred is None:
            return jsonify({"id": None, "address": None})
        return jsonify(pred)

    @app.post("/chord/notify")
    def notify():
        candidate = request.get_json()
        node.notify(candidate)
        return jsonify({"ok": True})

    @app.post("/chord/update_predecessor")
    def update_predecessor():
        new_pred = request.get_json()
        node.predecessor = new_pred
        return jsonify({"ok": True})

    @app.post("/chord/update_successor")
    def update_successor():
        new_succ = request.get_json()
        node.successor = new_succ
        return jsonify({"ok": True})

    @app.post("/chord/bulk_put")
    def bulk_put():
        items = request.get_json()
        node.bulk_put(items)
        return jsonify({"ok": True, "count": len(items)})

    @app.get("/chord/ping")
    def ping():
        return jsonify({"id": node.node_id, "address": node.address})

    @app.get("/chord/state")
    def state():
        return jsonify(node.state())

    # ------------------------------------------------------------------
    # Low-level local data store (no routing)
    # ------------------------------------------------------------------

    @app.get("/data")
    def data_list():
        """Return all key→value pairs stored locally on this node."""
        with node._lock:
            snapshot = dict(node.data_store)
        return jsonify({"ok": True, "node_id": node.node_id, "data": snapshot})

    @app.post("/data/<key>")
    def data_put(key):
        value = request.get_json()
        node.put(key, value)
        return jsonify({"ok": True, "key": key, "stored_at": node.node_id})

    @app.get("/data/<key>")
    def data_get(key):
        value = node.get(key)
        if value is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(value)

    @app.delete("/data/<key>")
    def data_delete(key):
        deleted = node.delete(key)
        return jsonify({"ok": deleted})

    # ------------------------------------------------------------------
    # Routed data API
    # ------------------------------------------------------------------
    #
    # Retry strategy for routed endpoints:
    # When we forward to the "responsible" node and it is unreachable
    # (the ring is mid-stabilisation because a node just joined/left),
    # we re-resolve the successor and try once more.  Two attempts are
    # enough: the second find_successor() call will reflect the updated
    # finger table that stabilisation has already repaired.
    #
    # This is distinct from the transport-level retry (which handles
    # transient TCP failures to a *known-good* address).  Here we
    # handle the case where the *routing* was stale.
    # ------------------------------------------------------------------

    ROUTED_RETRIES = 2  # re-resolve + retry this many times

    @app.post("/put/<key>")
    def routed_put(key):
        value = request.get_json()
        key_id = sha1_id(key)
        last_exc = None
        for attempt in range(ROUTED_RETRIES + 1):
            responsible = node.find_successor(key_id)
            if responsible["id"] == node.node_id:
                node.put(key, value)
                return jsonify({"ok": True, "key": key, "stored_at": node.node_id})
            try:
                node._transport.put(responsible["address"], key, value)
                return jsonify({"ok": True, "key": key, "stored_at": responsible["id"]})
            except Exception as e:
                last_exc = e
                logger.warning(
                    "[Server] routed PUT %s → %s failed (attempt %d/%d): %s",
                    key, responsible["address"], attempt + 1, ROUTED_RETRIES + 1, e,
                )
                time.sleep(0.1 * (2 ** attempt))
        return jsonify({"error": str(last_exc)}), 502

    @app.get("/get/<key>")
    def routed_get(key):
        key_id = sha1_id(key)
        last_exc = None
        for attempt in range(ROUTED_RETRIES + 1):
            responsible = node.find_successor(key_id)
            if responsible["id"] == node.node_id:
                value = node.get(key)
                if value is None:
                    return jsonify({"error": "not found"}), 404
                return jsonify(value)
            try:
                value = node._transport.get(responsible["address"], key)
                if value is None:
                    return jsonify({"error": "not found"}), 404
                return jsonify(value)
            except Exception as e:
                last_exc = e
                logger.warning(
                    "[Server] routed GET %s → %s failed (attempt %d/%d): %s",
                    key, responsible["address"], attempt + 1, ROUTED_RETRIES + 1, e,
                )
                time.sleep(0.1 * (2 ** attempt))
        return jsonify({"error": str(last_exc)}), 502

    @app.delete("/del/<key>")
    def routed_delete(key):
        """Routed delete: finds the responsible node and deletes the key there."""
        key_id = sha1_id(key)
        last_exc = None
        for attempt in range(ROUTED_RETRIES + 1):
            responsible = node.find_successor(key_id)
            if responsible["id"] == node.node_id:
                deleted = node.delete(key)
                return jsonify({"ok": deleted, "key": key, "deleted_from": node.node_id})
            try:
                node._transport.delete(responsible["address"], key)
                return jsonify({"ok": True, "key": key, "deleted_from": responsible["id"]})
            except Exception as e:
                last_exc = e
                logger.warning(
                    "[Server] routed DELETE %s → %s failed (attempt %d/%d): %s",
                    key, responsible["address"], attempt + 1, ROUTED_RETRIES + 1, e,
                )
                time.sleep(0.1 * (2 ** attempt))
        return jsonify({"error": str(last_exc)}), 502

    # ------------------------------------------------------------------
    # Job submission API — agent-orchestrated placement + replication
    # ------------------------------------------------------------------

    @app.post("/jobs")
    def submit_job():
        """
        Body: {"type": str, "payload": {}, "replicas": int (optional)}
        The agent selects the target node; the job key is engineered to hash there.
        """
        body = request.get_json(force=True) or {}
        job_type = body.get("type", "echo")
        payload = body.get("payload", {})
        requested_replicas = int(body.get("replicas", 1))

        valid_types = {"echo", "sleep", "compute"}
        if job_type not in valid_types:
            return jsonify({"error": f"Unknown job type '{job_type}'. Valid: {sorted(valid_types)}"}), 400
        if requested_replicas < 1 or requested_replicas > 10:
            return jsonify({"error": "replicas must be between 1 and 10"}), 400

        agent = app.config.get("agent")
        transport = node._transport

        # Collect ring metrics (self + reachable fingers)
        ring_metrics = _collect_ring_metrics(node, transport)

        job = make_job(job_type, payload)

        # --- Placement decision ---
        if agent and ring_metrics:
            placement = agent.select_placement(job, ring_metrics)
            target_node_id = placement["node_id"]
            placement_reasoning = placement["reasoning"]
        else:
            # No agent or metrics — fall back to standard Chord routing
            key_id = sha1_id(job_key(job["job_id"]))
            responsible = node.find_successor(key_id)
            target_node_id = responsible["id"]
            placement_reasoning = "no-agent fallback: standard Chord routing"

        # --- Replication decision ---
        replica_node_ids = []
        if agent and ring_metrics and requested_replicas > 1:
            rep_plan = agent.decide_replication(job, ring_metrics, requested_replicas)
            target_node_id = rep_plan["primary_node_id"]
            replica_node_ids = rep_plan.get("replica_node_ids", [])
            requested_replicas = rep_plan.get("replication_factor", 1)

        # Find address of chosen target
        target_address = _address_for(node, transport, target_node_id)

        # Store primary copy
        primary_key = _store_job(node, transport, job, target_address, target_node_id)

        # Store replicas
        replica_results = []
        for rid in replica_node_ids:
            if rid == target_node_id:
                continue
            replica_address = _address_for(node, transport, rid)
            replica_job = dict(job)
            replica_job["replica_of"] = job["job_id"]
            try:
                rkey = _store_job(node, transport, replica_job, replica_address, rid)
                replica_results.append({"node_id": rid, "key": rkey})
            except Exception as e:
                logger.warning(f"[Server] Replica to node {rid} failed: {e}")

        # Activity log — job submitted
        jid_short = job["job_id"][:12]
        strat = placement_reasoning[:60] if placement_reasoning else "—"
        replica_note = f" + {len(replica_results)} replica(s)" if replica_results else ""
        activity.log(
            activity.JOB_SUBMIT,
            f"Job {jid_short}… ({job_type}) → Node {target_node_id}{replica_note}",
            {"job_id": job["job_id"], "job_type": job_type,
             "node_id": target_node_id, "reasoning": strat,
             "replicas": len(replica_results)},
        )

        return jsonify({
            "ok": True,
            "job_id": job["job_id"],
            "primary_key": primary_key,
            "stored_at_node": target_node_id,
            "placement_reasoning": placement_reasoning,
            "replicas": replica_results,
        }), 201

    @app.get("/jobs")
    def list_jobs():
        """List all jobs known to this node — filterable by ?status=pending|running|done|failed."""
        status_filter = request.args.get("status")
        with node._lock:
            jobs = [
                v for k, v in node.data_store.items()
                if k.startswith("job:") and isinstance(v, dict)
                and (status_filter is None or v.get("status") == status_filter)
            ]
        jobs.sort(key=lambda j: j.get("created_at", 0), reverse=True)
        return jsonify({"jobs": jobs, "count": len(jobs), "node_id": node.node_id})

    @app.get("/jobs/<job_id>")
    def get_job(job_id):
        """Retrieve a job by ID from whichever node holds it.

        Uses the same re-resolve retry pattern as the routed data endpoints
        so a mid-stabilisation ring doesn't surface spurious 502s.
        """
        key = job_key(job_id)
        key_id = sha1_id(key)
        last_exc = None
        for attempt in range(ROUTED_RETRIES + 1):
            responsible = node.find_successor(key_id)
            if responsible["id"] == node.node_id:
                value = node.get(key)
                if value is None:
                    return jsonify({"error": "not found"}), 404
                return jsonify(value)
            try:
                value = node._transport.get(responsible["address"], key)
                if value is None:
                    return jsonify({"error": "not found"}), 404
                return jsonify(value)
            except Exception as e:
                last_exc = e
                logger.warning(
                    "[Server] GET /jobs/%s → %s failed (attempt %d/%d): %s",
                    job_id, responsible["address"], attempt + 1, ROUTED_RETRIES + 1, e,
                )
                time.sleep(0.1 * (2 ** attempt))
        return jsonify({"error": str(last_exc)}), 502

    # ------------------------------------------------------------------
    # Metrics endpoint (used by AgentLoop and transport.get_metrics)
    # ------------------------------------------------------------------

    @app.get("/metrics")
    def metrics():
        return jsonify(node.metrics())

    @app.get("/api/status")
    def api_status():
        """Ring health summary — used by monitoring and the dashboard header."""
        m = node.metrics()
        return jsonify({
            "node_id": node.node_id,
            "address": node.address,
            "successor": node.successor,
            "predecessor": node.predecessor,
            "queue_depth": m["queue_depth"],
            "jobs_completed": m["jobs_completed"],
            "jobs_failed": m["jobs_failed"],
            "ring_size_estimate": sum(
                1 for f in node.fingers if f.node_id is not None and f.node_id != node.node_id
            ) + 1,
        })

    @app.get("/api/nodes/count")
    def api_nodes_count():
        """Quick endpoint returning just the number of known unique nodes."""
        seen = {node.node_id}
        for f in node.fingers:
            if f.node_id is not None:
                seen.add(f.node_id)
        return jsonify({"count": len(seen), "this_node": node.node_id})

    # ------------------------------------------------------------------
    # Dashboard (served from chord/static/index.html)
    # ------------------------------------------------------------------

    # Landing page is the chatbot (user-facing surface).
    # The dashboard lives at /dashboard. /chat kept as alias for old links.

    @app.get("/")
    def landing():
        return send_from_directory(_STATIC_DIR, "chat.html")

    @app.get("/dashboard")
    def dashboard():
        return send_from_directory(_STATIC_DIR, "index.html")

    @app.get("/chat")
    def chat_alias():
        return send_from_directory(_STATIC_DIR, "chat.html")
    @app.get("/recovery")
    def recovery():
        return send_from_directory(_STATIC_DIR, "recovery.html")
    # ------------------------------------------------------------------
    # Dashboard API — ring topology
    # ------------------------------------------------------------------

    @app.get("/api/ring")
    def api_ring():
        """Walk the successor chain and return all reachable node states + metrics."""
        seen = {}
        to_visit = [node.address]
        visited = set()

        while to_visit:
            addr = to_visit.pop(0)
            if addr in visited:
                continue
            visited.add(addr)

            try:
                if addr == node.address:
                    state = node.state()
                    state["metrics"] = node.metrics()
                else:
                    r = _requests.get(f"http://{addr}/chord/state", timeout=1.5)
                    state = r.json()
                    try:
                        mr = _requests.get(f"http://{addr}/metrics", timeout=1.0)
                        state["metrics"] = mr.json()
                    except Exception:
                        state["metrics"] = None

                nid = state["node_id"]
                if nid not in seen:
                    seen[nid] = state
                    # Enqueue successor first, then all finger addresses so the
                    # walk can bridge over any dead node in the successor chain.
                    succ_addr = state.get("successor", {}).get("address")
                    if succ_addr and succ_addr not in visited:
                        to_visit.append(succ_addr)
                    for finger in state.get("fingers", []):
                        fa = finger.get("node_address")
                        if fa and fa not in visited:
                            to_visit.append(fa)
            except Exception:
                pass

        return jsonify({"nodes": list(seen.values()), "this_node": node.node_id})

    # ------------------------------------------------------------------
    # Dashboard API — job list (aggregate across ring)
    # ------------------------------------------------------------------

    @app.get("/api/jobs_local")
    def api_jobs_local():
        with node._lock:
            jobs = [v for k, v in node.data_store.items()
                    if k.startswith("job:") and isinstance(v, dict)]
        return jsonify({"jobs": jobs})

    @app.get("/api/jobs")
    def api_jobs():
        all_jobs = {}
        ring_addrs = {node.address}
        for f in node.fingers:
            if f.node_address:
                ring_addrs.add(f.node_address)

        for addr in ring_addrs:
            try:
                if addr == node.address:
                    with node._lock:
                        local = [v for k, v in node.data_store.items()
                                 if k.startswith("job:") and isinstance(v, dict)]
                else:
                    r = _requests.get(f"http://{addr}/api/jobs_local", timeout=1.5)
                    local = r.json().get("jobs", [])

                for j in local:
                    jid = j.get("job_id")
                    if jid and not j.get("replica_of"):
                        all_jobs[jid] = j
            except Exception:
                pass

        jobs = sorted(all_jobs.values(), key=lambda j: j.get("created_at", 0), reverse=True)
        return jsonify({"jobs": jobs[:60]})

    # ------------------------------------------------------------------
    # Dashboard API — real-time ring activity feed
    # ------------------------------------------------------------------

    @app.get("/chord/activity_local")
    def chord_activity_local():
        """Return this node's local activity entries (called by /api/activity aggregator)."""
        return jsonify({"entries": activity.get_entries(100)})

    @app.get("/api/logs")
    def api_logs():
        """
        Returns the merged activity feed from all reachable ring nodes, newest first.
        Falls back gracefully if peer nodes are unreachable.
        """
        # Collect addresses of all ring nodes via finger table
        ring_addrs = {node.address}
        for f in node.fingers:
            if f.node_address:
                ring_addrs.add(f.node_address)

        all_entries: List[Dict] = []

        # Fetch local entries
        all_entries.extend(activity.get_entries(100))

        # Fetch from peer nodes
        for addr in ring_addrs:
            if addr == node.address:
                continue
            try:
                r = _requests.get(f"http://{addr}/chord/activity_local", timeout=1.5)
                peer_entries = r.json().get("entries", [])
                all_entries.extend(peer_entries)
            except Exception:
                pass

        # Also include the file-based agent decision log (backward compat)
        log_path = pathlib.Path(os.environ.get("AGENT_LOG_PATH", "agent_decisions.jsonl"))
        if log_path.exists():
            try:
                lines = log_path.read_text().strip().splitlines()
                for line in lines[-40:]:
                    try:
                        e = json.loads(line)
                        # Convert to activity format for the renderer
                        e.setdefault("type", "agent")
                        e.setdefault("msg", f"{e.get('agent','Agent')} · {e.get('tool','')} · {e.get('strategy','')}")
                        all_entries.append(e)
                    except Exception:
                        pass
            except Exception:
                pass

        # Deduplicate by (ts, msg), sort newest last, return last 150
        seen = set()
        unique = []
        for e in all_entries:
            key = (round(e.get("ts", 0), 2), e.get("msg", ""))
            if key not in seen:
                seen.add(key)
                unique.append(e)

        unique.sort(key=lambda e: e.get("ts", 0))
        return jsonify({"entries": unique[-150:]})

    # ------------------------------------------------------------------
    # Dashboard API — remove a ring node (proxy to avoid CORS)
    # ------------------------------------------------------------------

    @app.delete("/api/nodes/<path:address>")
    def api_remove_node(address):
        try:
            _requests.post(f"http://{address}/admin/leave", timeout=2)
        except Exception:
            pass  # Node likely shut down before responding — fine
        return jsonify({"ok": True})

    @app.post("/admin/leave")
    def admin_leave():
        def _do():
            time.sleep(0.3)
            try:
                node.leave()
            except Exception:
                pass
            time.sleep(0.4)
            os._exit(0)
        threading.Thread(target=_do, daemon=True).start()
        return jsonify({"ok": True})

    # ------------------------------------------------------------------
    # Dashboard API — automatically add a new node to the ring
    # ------------------------------------------------------------------

    @app.post("/api/nodes/add")
    def api_add_node():
        """
        Auto-add a new Chord node to the ring.
        Determines the next available port and spawns a subprocess.
        """
        try:
            # Get all current nodes and their ports
            ports = set()
            ports.add(int(node.address.split(':')[1]))  # Add current node's port
            
            # Full ring walk to find all used ports
            seen: Set[int] = set()
            current = node.successor
            
            while current and current["id"] not in seen and len(seen) < 1000:  # Limit to 1000 nodes
                seen.add(current["id"])
                try:
                    port_str = current["address"].split(':')[1]
                    ports.add(int(port_str))
                    # Get successor of this node via HTTP /chord/state endpoint
                    resp = _requests.get(f"http://{current['address']}/chord/state", timeout=2)
                    if resp.status_code == 200:
                        state_data = resp.json()
                        current = state_data.get("successor")
                    else:
                        break
                except Exception as e:
                    logger.debug(f"[API] Ring walk stopped at {current['address']}: {e}")
                    break
            
            # Find next available port — probe until no node responds on that port
            next_port = 5002 if not ports else max(ports) + 1
            while True:
                try:
                    _requests.get(f"http://127.0.0.1:{next_port}/chord/ping", timeout=0.5)
                    next_port += 1  # port is occupied, try next
                except Exception:
                    break  # no response → port is free
            logger.info(f"[API] Used ports: {sorted(ports)}, next port: {next_port}")
            
            # Get the join address (current node)
            join_addr = node.address
            
            # Spawn new node in background
            spawn_error = []  # mutable container so the thread can write to it

            def _spawn():
                try:
                    env = os.environ.copy()
                    env['AGENT_STRATEGY'] = 'heuristic'
                    # Use the same interpreter that is running right now — works
                    # in virtualenvs, Docker, and any custom Python installation.
                    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    proc = subprocess.Popen(
                        [
                            sys.executable, 'run_node.py',
                            '--host', '127.0.0.1',
                            '--port', str(next_port),
                            '--join', join_addr,
                            '--worker',
                            '--log', 'INFO',
                        ],
                        env=env,
                        cwd=project_root,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                    )
                    logger.info(f"[API] Spawned new node on port {next_port} (pid={proc.pid}) from {project_root}")
                except Exception as e:
                    spawn_error.append(str(e))
                    logger.error(f"[API] Failed to spawn node: {e}", exc_info=True)

            t = threading.Thread(target=_spawn, daemon=True)
            t.start()
            # Give the thread a short moment to detect immediate failures
            # (e.g. executable not found) before we respond.
            t.join(timeout=0.3)

            if spawn_error:
                return jsonify({
                    "ok": False,
                    "error": spawn_error[0],
                    "hint": (
                        f"Run manually: {sys.executable} run_node.py "
                        f"--port {next_port} --join {join_addr} --worker"
                    ),
                    "port": next_port,
                }), 500

            activity.log(activity.NODE_JOIN,
                         f"New node spawning on port {next_port} (joining via {join_addr})",
                         {"port": next_port, "join_via": join_addr})
            return jsonify({
                "ok": True,
                "port": next_port,
                "address": f"127.0.0.1:{next_port}",
                "cmd": f"{sys.executable} run_node.py --port {next_port} --join {join_addr} --worker",
                "message": f"New node spawning on port {next_port}",
            })
        except Exception as e:
            logger.error(f"[API] Error in add_node: {e}")
            return jsonify({"ok": False, "error": str(e)}), 500

    # ------------------------------------------------------------------
    # File request routing — demonstrates DHT as a distributed file store
    # ------------------------------------------------------------------

    @app.post("/request")
    def handle_request():
        """
        Receive a file request (from DummyClient or any HTTP client).
        Hash the filename → find the responsible Chord node → route there.
        The responsible node stores/serves dummy file content.
        """
        body     = request.get_json(force=True) or {}
        filename = (body.get("filename") or "").strip()
        client   = body.get("client", "unknown")

        if not filename:
            return jsonify({"error": "filename is required"}), 400

        t0          = time.time()
        ftype       = file_type(filename)
        key_id      = sha1_id(filename)
        responsible = node.find_successor(key_id)
        hops        = 1
        nid_str     = str(node.node_id)

        if responsible["id"] == node.node_id:
            content = _ensure_file(node, filename)
            served_addr = node.address
        else:
            hops = 2
            served_addr = responsible["address"]
            try:
                r = _requests.post(
                    f"http://{served_addr}/files/{filename}",
                    json={"client": client},
                    timeout=4,
                )
                content = r.json()
            except Exception as e:
                logger.warning(f"[Server] File routing failed for '{filename}': {e}")
                content = {}

        # ── Prometheus instrumentation ──
        duration_s = time.time() - t0
        FILE_REQUESTS.labels(node_id=nid_str, file_type=ftype).inc()
        FILE_REQUEST_HOPS.labels(node_id=nid_str).observe(hops)
        FILE_REQUEST_DURATION.labels(node_id=nid_str).observe(duration_s)

        entry = {
            "ts":             time.time(),
            "filename":       filename,
            "file_type":      ftype,
            "client":         client,
            "key_id":         key_id,
            "routed_from":    node.node_id,
            "served_by_node": responsible["id"],
            "served_by_addr": served_addr,
            "hops":           hops,
            "duration_ms":    round(duration_s * 1000, 1),
        }
        with _request_lock:
            _request_log.append(entry)
            if len(_request_log) > 100:
                _request_log.pop(0)
            # Persist to file
            try:
                with _request_log_path.open('a') as f:
                    f.write(json.dumps(entry) + '\n')
            except Exception as e:
                logger.warning(f"[Server] Failed to persist request log: {e}")

        return jsonify({
            "ok":             True,
            "filename":       filename,
            "key_id":         key_id,
            "served_by_node": responsible["id"],
            "served_by_addr": served_addr,
            "hops":           hops,
            "content":        content,
        })

    @app.post("/files/<path:filename>")
    def file_put(filename):
        """Called by a routing node to store/serve a file on this node."""
        content = _ensure_file(node, filename)
        content["serve_count"] = content.get("serve_count", 0) + 1
        content["last_served"] = time.time()
        node.put(f"file:{filename}", content)
        return jsonify(content)

    @app.get("/files/<path:filename>")
    def file_get(filename):
        content = node.get(f"file:{filename}")
        if content is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(content)

    @app.get("/api/requests")
    def api_requests():
        """Last 30 file requests — polled by the dashboard."""
        with _request_lock:
            return jsonify({"requests": list(reversed(_request_log[-30:]))})

    # ------------------------------------------------------------------
    # Prometheus metrics endpoint
    # ------------------------------------------------------------------

    @app.get("/prom_metrics")
    def prom_metrics():
        """Prometheus text-format scrape endpoint."""
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        nid_str = str(node.node_id)
        m       = node.metrics()
        QUEUE_DEPTH.labels(node_id=nid_str).set(m["queue_depth"])
        DATA_KEYS.labels(node_id=nid_str).set(len(node.data_store))
        # Ring size: count unique non-self fingers + self
        unique = {f.node_id for f in node.fingers if f.node_id is not None}
        RING_SIZE.labels(node_id=nid_str).set(len(unique))
        return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}

    # ------------------------------------------------------------------
    # Metrics snapshot — time-series data for analytics charts
    # ------------------------------------------------------------------

    @app.get("/api/metrics/snapshot")
    def metrics_snapshot():
        now = time.time()
        with _request_lock:
            all_reqs = list(_request_log)

        recent_60s = [r for r in all_reqs if now - r["ts"] < 60]
        recent_30  = all_reqs[-30:] if all_reqs else []

        hop_dist: Dict[str, int] = {}
        for r in all_reqs:
            h = str(r.get("hops", 1))
            hop_dist[h] = hop_dist.get(h, 0) + 1

        hops_list = [r.get("hops", 1) for r in recent_30]
        avg_hops  = sum(hops_list) / len(hops_list) if hops_list else 0

        # Collect per-node metrics via ring walk
        node_loads: Dict[str, int] = {}
        jobs_completed = 0
        jobs_failed    = 0
        seen: Set[int] = set()
        to_visit = [node.address]
        visited:  Set[str] = set()

        while to_visit:
            addr = to_visit.pop(0)
            if addr in visited:
                continue
            visited.add(addr)
            try:
                if addr == node.address:
                    m = node.metrics()
                else:
                    r = _requests.get(f"http://{addr}/metrics", timeout=0.8)
                    m = r.json()
                nid = m["node_id"]
                if nid not in seen:
                    seen.add(nid)
                    node_loads[str(nid)] = m.get("queue_depth", 0)
                    jobs_completed += m.get("jobs_completed", 0)
                    jobs_failed    += m.get("jobs_failed", 0)
                    for f in node.fingers:
                        if f.node_id not in seen and f.node_address and f.node_address not in visited:
                            to_visit.append(f.node_address)
                            break
            except Exception:
                pass

        jobs_running = sum(node_loads.values())

        return jsonify({
            "ts":             now,
            "req_per_min":    len(recent_60s),
            "avg_hops":       round(avg_hops, 2),
            "hop_dist":       hop_dist,
            "node_loads":     node_loads,
            "total_requests": len(all_reqs),
            "jobs_completed": jobs_completed,
            "jobs_failed":    jobs_failed,
            "jobs_running":   jobs_running,
            "ring_size":      len(seen),
        })
    # Configuration endpoint for frontend
    # ------------------------------------------------------------------

    @app.get("/api/config")
    def config():
        """Return frontend config including Grafana URL."""
        grafana_url = os.environ.get("GRAFANA_URL", "http://localhost:3000")
        return jsonify({"grafana_url": grafana_url}), 200

    # Recovery API endpoints for monitoring failure detection and recovery
    # ------------------------------------------------------------------

    @app.get("/api/workers/status")
    def api_workers_status():
        """Return status of all workers in the cluster."""
        try:
            worker_registry = app.config.get("worker_registry")
            if not worker_registry:
                return jsonify({"error": "Worker registry not available"}), 500

            workers = []
            # Get scores once (includes only live workers)
            scored = worker_registry.score_all_workers()
            score_map = dict(scored)
            
            for worker_id in worker_registry.live_workers():
                metrics = worker_registry.get_metrics(worker_id)
                stats = worker_registry.get_stats(worker_id)
                score = score_map.get(worker_id, 0.0)
                
                workers.append({
                    "id": worker_id,
                    "status": "alive",
                    "pending_tasks": metrics.pending_tasks if metrics else 0,
                    "latency_ms": metrics.latency_ms if metrics else 0,
                    "success_rate": stats.success_rate if stats else 0.0,
                    "score": score,
                })
            
            # Also include recently dead workers
            live_ids = set(worker_registry.live_workers())
            for worker_id, _ts, _age in worker_registry.all_workers():
                if worker_id not in live_ids:
                    workers.append({
                        "id": worker_id,
                        "status": "dead",
                        "pending_tasks": 0,
                        "latency_ms": 0,
                        "success_rate": 0.0,
                        "score": 0.0,
                    })

            return jsonify({"workers": workers}), 200
        except Exception as e:
            logger.error(f"[/api/workers/status] error: {e}")
            return jsonify({"error": str(e)}), 500

    @app.get("/api/tasks/active")
    def api_tasks_active():
        """Return active task count for dashboard visualization."""
        try:
            # Simplified: return empty list (0 active tasks)
            # The frontend uses this to display count: tasks.length
            return jsonify({"tasks": []}), 200
        except Exception as e:
            logger.error(f"[/api/tasks/active] error: {e}")
            return jsonify({"tasks": []}), 200

    @app.get("/api/recovery/events")
    def api_events():
        """Return recovery events for the timeline."""
        try:
            recovery_manager = app.config.get("recovery_manager")
            if not recovery_manager:
                return jsonify({"events": [], "total_recovery_attempts": 0}), 200

            since = request.args.get("since", default=0.0, type=float)
            events = recovery_manager.get_events(since)

            # Convert ts -> timestamp for frontend compatibility
            converted = [{
                "type": e["type"],
                "timestamp": e["ts"],
                "task_id": e["task_id"],
                "worker_id": e["worker_id"],
                "message": e["message"],
            } for e in events]

            # Count retry-type events as recovery attempts
            attempts = sum(1 for e in converted if e["type"] in ("retry", "wait_retry", "give_up"))

            return jsonify({"events": converted, "total_recovery_attempts": attempts}), 200
        except Exception as e:
            logger.error(f"[/api/recovery/events] error: {e}")
            return jsonify({"error": str(e)}), 500

    @app.post("/debug/trigger-recovery")
    def debug_trigger_recovery():
        """Manually trigger an immediate recovery scan."""
        try:
            recovery_manager = app.config.get("recovery_manager")
            if not recovery_manager:
                return jsonify({"error": "Recovery manager not available"}), 500

            recovery_manager.trigger()
            return jsonify({"ok": True, "scanned_at": time.time()}), 200
        except Exception as e:
            logger.error(f"[/debug/trigger-recovery] error: {e}")
            return jsonify({"error": str(e)}), 500

    # ------------------------------------------------------------------
    # Fault injection — hard kill (no graceful handoff)
    # ------------------------------------------------------------------

    @app.post("/admin/crash")
    def admin_crash():
        """Immediately kill this process — simulates a sudden node failure."""
        threading.Thread(
            target=lambda: (time.sleep(0.15), os._exit(1)),
            daemon=True
        ).start()
        return jsonify({"ok": True})

    @app.post("/api/nodes/<path:address>/crash")
    def api_crash_node(address):
        """Proxy crash command to another node (avoids CORS from browser)."""
        try:
            _requests.post(f"http://{address}/admin/crash", timeout=2)
        except Exception:
            pass  # Node dies before it can reply — expected
        return jsonify({"ok": True})

    # Task service API

    @app.post("/tasks")
    def register_task():
        payload = request.get_json() or {}
        try:
            result = task_service.register_task(payload)
            return jsonify({"ok": True, "data": result}), 201
        except TaskValidationError as e:
            return jsonify({"ok": False, "error": {"code": "VALIDATION_ERROR", "message": str(e)}}), 422
        except TaskConflictError as e:
            return jsonify({"ok": False, "error": {"code": "TASK_CONFLICT", "message": str(e)}}), 409
        except Exception as e:
            logger.error("[register_task] Unexpected error: %s", e, exc_info=True)
            return jsonify({"ok": False, "error": {"code": "INTERNAL_ERROR", "message": str(e)}}), 500

    @app.get("/tasks/<task_id>")
    def get_task(task_id):
        allow_replica_read = request.args.get("allow_replica_read", "true").lower() != "false"
        try:
            task = task_service.get_task(task_id, allow_replica_read=allow_replica_read)
            if task is None:
                return jsonify({"ok": False, "error": {"code": "TASK_NOT_FOUND", "message": f"task not found: {task_id}"}}), 404
            return jsonify({"ok": True, "data": {"task": task}})
        except TaskValidationError as e:
            return jsonify({"ok": False, "error": {"code": "VALIDATION_ERROR", "message": str(e)}}), 422

    @app.delete("/tasks/<task_id>")
    def deregister_task(task_id):
        hard_delete = request.args.get("hard", "false").lower() == "true"
        try:
            result = task_service.deregister_task(task_id, hard_delete=hard_delete)
            return jsonify({"ok": True, "data": result})
        except TaskNotFoundError as e:
            return jsonify({"ok": False, "error": {"code": "TASK_NOT_FOUND", "message": str(e)}}), 404
        except TaskValidationError as e:
            return jsonify({"ok": False, "error": {"code": "VALIDATION_ERROR", "message": str(e)}}), 422

    @app.patch("/tasks/<task_id>")
    def patch_task(task_id):
        """Partially update a task (status, result, error, payload, priority)."""
        patch = request.get_json() or {}
        try:
            result = task_service.update_task(task_id, patch)
            return jsonify({"ok": True, "data": result})
        except TaskNotFoundError as e:
            return jsonify({"ok": False, "error": {"code": "TASK_NOT_FOUND", "message": str(e)}}), 404
        except TaskValidationError as e:
            return jsonify({"ok": False, "error": {"code": "VALIDATION_ERROR", "message": str(e)}}), 422

    @app.get("/tasks")
    def query_tasks():
        job_id = request.args.get("job_id")
        status = request.args.get("status")
        include_deleted = request.args.get("include_deleted", "false").lower() == "true"
        limit = int(request.args.get("limit", "100"))

        tasks = task_service.query_local_tasks(
            job_id=job_id,
            status=status,
            include_deleted=include_deleted,
            limit=limit,
        )
        return jsonify({"ok": True, "data": {"tasks": tasks, "count": len(tasks)}})

    @app.get("/ring/lookup/<task_id>")
    def lookup_task(task_id):
        try:
            result = task_service.lookup_owner(task_id)
            return jsonify({"ok": True, "data": result})
        except TaskValidationError as e:
            return jsonify({"ok": False, "error": {"code": "VALIDATION_ERROR", "message": str(e)}}), 422

    @app.get("/chord/key-owner/<path:key>")
    def key_owner(key):
        """
        General-purpose Chord key owner lookup — hashes `key` as-is (no prefix).

        Unlike /ring/lookup/<task_id>, this endpoint does NOT add a 'task:'
        prefix.  Use it from the DHT Store tab to find which node owns any
        arbitrary DHT key (plain strings, filenames, task:xxx, job:xxx, etc.)
        """
        key_id = sha1_id(key)
        primary = node.find_successor(key_id)
        chain = task_service.replication.get_successor_chain(primary)
        return jsonify({
            "ok": True,
            "data": {
                "key": key,
                "key_id": key_id,
                "primary": primary,
                "replicas": chain[1:],
            },
        })

    @app.get("/nodes/self")
    def node_self():
        return jsonify({"ok": True, "data": task_service.get_node_state()})

    @app.get("/nodes/query")
    def node_query():
        address = request.args.get("address")
        if not address:
            return jsonify({"ok": False, "error": {"code": "VALIDATION_ERROR", "message": "missing query param: address"}}), 422
        try:
            state = task_service.get_node_state(address=address)
            return jsonify({"ok": True, "data": state})
        except Exception as e:
            return jsonify({"ok": False, "error": {"code": "UPSTREAM_ERROR", "message": str(e)}}), 502

    # Internal replication API

    @app.post("/internal/tasks/replica/<path:task_key>")
    def put_task_replica(task_key):
        payload = request.get_json() or {}
        try:
            result = task_service.store_replica_local(task_key, payload)
            return jsonify({"ok": True, "data": result})
        except TaskValidationError as e:
            return jsonify({"ok": False, "error": {"code": "VALIDATION_ERROR", "message": str(e)}}), 422

    @app.get("/internal/tasks/replica/<path:task_key>")
    def get_task_replica(task_key):
        task = task_service.get_replica_local(task_key)
        if task is None:
            return jsonify({"ok": False, "error": {"code": "TASK_NOT_FOUND", "message": f"task not found: {task_key}"}}), 404
        return jsonify({"ok": True, "task": task})

    @app.delete("/internal/tasks/replica/<path:task_key>")
    def delete_task_replica(task_key):
        deleted = task_service.delete_replica_local(task_key)
        return jsonify({"ok": True, "deleted": deleted, "task_key": task_key})

    # ------------------------------------------------------------------
    # Phase 2 — Frontend / Conversational Agent endpoints
    #
    # These are additive and DO NOT touch the existing /tasks/* routes.
    # Storage for /createTask and /getStatus is stubbed in this phase;
    # actual DHT writes land in Phase 3.
    # ------------------------------------------------------------------

    _VALID_TASK_TYPES = {"SCRIPT", "BINARY"}

    @app.post("/workers/heartbeat")
    def workers_heartbeat():
        body = request.get_json(silent=True) or {}
        worker_id = body.get("worker_id")
        if not worker_id:
            return jsonify({"ok": False, "error": "worker_id required"}), 422
        worker_registry.heartbeat(worker_id)
        return jsonify({"ok": True})

    @app.get("/workers/live")
    def workers_live():
        return jsonify({"live_workers": worker_registry.live_workers()})

    @app.get("/debug/worker-metrics")
    def debug_worker_metrics():
        return jsonify(worker_registry.metrics_snapshot())

    @app.get("/api/dht/contents")
    def api_dht_contents():
        """
        Aggregate the two distributed hash tables across the ring:
          HT1 = result:<task_id> records (task details)
          HT2 = worker:<worker_id> queues (assigned task_ids)
        Returns per-node key counts + per-key primary/replicas.
        """
        # Walk ring (mirror of /api/ring's walk) — collect each node's data_keys
        nodes_seen: Dict[int, Dict] = {}
        try:
            self_state = {
                "node_id": node.node_id,
                "address": node.address,
                "successor": node.successor,
                "data_keys": list(node.data_store.keys()),
            }
            nodes_seen[node.node_id] = self_state
            cur = node.successor
            guard = 0
            while cur and cur["id"] not in nodes_seen and guard < 64:
                guard += 1
                try:
                    r = _requests.get(f"http://{cur['address']}/chord/state", timeout=1.0)
                    if not r.ok:
                        break
                    s = r.json()
                    nodes_seen[s["node_id"]] = s
                    cur = s.get("successor")
                except Exception:
                    break
        except Exception as e:
            logger.warning("[dht/contents] ring walk failed: %s", e)

        # Build successor list ordered by node_id walking the ring
        ordered = sorted(nodes_seen.values(), key=lambda n: n["node_id"])
        node_count = len(ordered)

        # Unique keys + which node has each
        all_keys: Dict[str, List[int]] = {}  # key -> list of node_ids that store it
        for n in ordered:
            for k in (n.get("data_keys") or []):
                all_keys.setdefault(k, []).append(n["node_id"])

        def replicas_for(primary_id: int) -> List[int]:
            if node_count <= 1:
                return []
            ids = [n["node_id"] for n in ordered]
            try:
                idx = ids.index(primary_id)
            except ValueError:
                return []
            return [ids[(idx + 1) % node_count], ids[(idx + 2) % node_count]][:max(0, node_count - 1)]

        ht1_rows: List[Dict] = []
        ht2_rows: List[Dict] = []
        for key in sorted(all_keys.keys()):
            key_id = sha1_id(key)
            try:
                primary = node.find_successor(key_id)
                primary_id = primary.get("id")
            except Exception:
                primary_id = None
            reps = replicas_for(primary_id) if primary_id is not None else []
            try:
                value = node.get(key)
            except Exception:
                value = None
            if key.startswith("result:"):
                v = value if isinstance(value, dict) else {}
                def _to_epoch_ms(x):
                    if isinstance(x, (int, float)):
                        return int(x * 1000) if x < 1e12 else int(x)
                    if isinstance(x, str) and x:
                        try:
                            from datetime import datetime as _dt
                            s = x.replace("Z", "+00:00")
                            return int(_dt.fromisoformat(s).timestamp() * 1000)
                        except Exception:
                            return None
                    return None
                created_ms = _to_epoch_ms(v.get("created_at"))
                updated_ms = _to_epoch_ms(v.get("updated_at"))
                duration_s = None
                if created_ms is not None and updated_ms is not None:
                    duration_s = round((updated_ms - created_ms) / 1000.0, 3)
                ht1_rows.append({
                    "key": key,
                    "task_id": key[len("result:"):],
                    "task_type": v.get("task_type"),
                    "status": v.get("status"),
                    "worker_id": v.get("worker_id"),
                    "result": v.get("result"),
                    "created_at": created_ms,
                    "updated_at": updated_ms,
                    "duration_s": duration_s,
                    "owner": primary_id,
                    "replicas": reps,
                })
            elif key.startswith("worker:"):
                if isinstance(value, dict):
                    tasks = list(value.get("tasks") or [])
                elif isinstance(value, list):
                    tasks = value
                else:
                    tasks = []
                ht2_rows.append({
                    "key": key,
                    "worker_id": key[len("worker:"):],
                    "task_count": len(tasks),
                    "task_ids": tasks,
                    "owner": primary_id,
                    "replicas": reps,
                })

        # Self-heal: any task_id stuck in HT2 whose HT1 status is SUCCESS
        # should not be there. The 3 s delayed cleanup can be missed when
        # the server is restarted mid-sleep, so we reconcile on read.
        success_ids = {r["task_id"] for r in ht1_rows if r.get("status") == "SUCCESS"}
        if success_ids:
            for row in ht2_rows:
                stuck = [t for t in (row.get("task_ids") or []) if t in success_ids]
                if not stuck:
                    continue
                wid = row.get("worker_id")
                for tid in stuck:
                    try:
                        worker_assignment_service.remove(wid, tid)
                    except Exception:
                        pass
                # update the in-memory row so the response reflects cleanup
                row["task_ids"] = [t for t in row["task_ids"] if t not in success_ids]
                row["task_count"] = len(row["task_ids"])

        per_node = []
        for n in ordered:
            ht1 = sum(1 for k in (n.get("data_keys") or []) if k.startswith("result:"))
            ht2 = sum(1 for k in (n.get("data_keys") or []) if k.startswith("worker:"))
            per_node.append({
                "node_id": n["node_id"],
                "address": n["address"],
                "ht1": ht1,
                "ht2": ht2,
                "total": len(n.get("data_keys") or []),
            })

        return jsonify({
            "nodes": per_node,
            "ht1": ht1_rows,
            "ht2": ht2_rows,
            "this_node": node.node_id,
        })

    # Workers spawned via the dashboard — pid tracked here so we can kill them.
    _worker_pids: Dict[str, int] = {}

    @app.post("/api/workers/spawn")
    def api_spawn_worker():
        body = request.get_json(silent=True) or {}
        requested = (body.get("worker_id") or "").strip()
        if requested:
            wid = requested
        else:
            existing = set(worker_registry.live_workers()) | set(_worker_pids.keys())
            n = 1
            while f"w{n}" in existing:
                n += 1
            wid = f"w{n}"
        if wid in _worker_pids:
            return jsonify({"ok": False, "error": f"worker {wid} already running"}), 409
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "chord.task_runner",
                 "--worker-id", wid,
                 "--frontend-url", f"http://{node.address}"],
                cwd=project_root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _worker_pids[wid] = proc.pid
            logger.info(f"[API] Spawned worker {wid} pid={proc.pid}")
            try: _obs_event("worker_spawned", f"worker {wid} (pid {proc.pid})", worker_id=wid, pid=proc.pid)
            except Exception: pass
            # Wait briefly for the new worker to heartbeat in, then rebalance
            # any PENDING tasks so it doesn't sit idle while existing workers
            # have a backlog.
            def _rebalance_after_spawn(target_wid=wid):
                for _ in range(20):  # up to ~6 s
                    time.sleep(0.3)
                    if target_wid in worker_registry.live_workers():
                        break
                try:
                    _do_rebalance()
                except Exception:
                    logger.exception("[spawn] rebalance failed")
            threading.Thread(target=_rebalance_after_spawn, daemon=True).start()
            return jsonify({"ok": True, "worker_id": wid, "pid": proc.pid})
        except Exception as e:
            logger.error(f"[API] Failed to spawn worker {wid}: {e}", exc_info=True)
            return jsonify({"ok": False, "error": str(e)}), 500

    def _do_rebalance() -> Dict[str, int]:
        """
        Re-distribute PENDING tasks across live workers so newly-added
        workers actually pick up work instead of sitting idle.

        Strategy: collect every PENDING HT1 record, group by current
        worker_id. Compute the average load. For each over-loaded worker,
        move its excess tasks to the most under-loaded live workers.
        """
        live = worker_registry.live_workers() or []
        if len(live) < 2:
            return {"moved": 0, "reason": "need at least 2 live workers"}

        # Gather all PENDING result:* records owned by this node's accessible view
        pending: List[tuple] = []  # (task_id, current_worker, full_record)
        for k in list(node.data_store.keys()):
            if not k.startswith("result:"):
                continue
            v = node.data_store.get(k)
            if isinstance(v, dict) and v.get("status") == "PENDING":
                pending.append((k[len("result:"):], v.get("worker_id"), v))
        if not pending:
            return {"moved": 0, "reason": "no PENDING tasks"}

        # Current load per live worker (only counting tasks we know about)
        load = {w: 0 for w in live}
        for (_tid, wid, _rec) in pending:
            if wid in load:
                load[wid] += 1

        target = max(1, len(pending) // len(live))  # ceil-ish
        moved = 0
        for (tid, src_wid, rec) in pending:
            # Skip if the current worker is already under-loaded
            if src_wid not in load:
                src_wid = None  # treat as orphaned
            if src_wid is not None and load[src_wid] <= target:
                continue
            # Pick the live worker with the lowest current load
            dst_wid = min(load.keys(), key=lambda w: load[w])
            if src_wid is not None and load[dst_wid] >= load[src_wid]:
                continue  # not actually a balancing move
            # Move it: update HT1 worker_id; update HT2 entries.
            new_rec = dict(rec)
            new_rec["worker_id"] = dst_wid
            # "intelligence" is one of the validator's allowed values; the
            # rebalancer is effectively the placement scorer reacting to a
            # new worker joining, so it fits semantically.
            new_rec["assigned_by"] = "intelligence"
            try:
                node.put(f"result:{tid}", new_rec)
                if src_wid:
                    worker_assignment_service.remove(src_wid, tid)
                worker_assignment_service.append(dst_wid, tid)
                load[dst_wid] += 1
                if src_wid in load:
                    load[src_wid] -= 1
                moved += 1
            except Exception:
                logger.exception("[rebalance] failed to move %s", tid)
        if moved:
            logger.info("[rebalance] moved %d PENDING tasks across %d workers",
                        moved, len(live))
            try: _obs_event("rebalance", f"moved {moved} pending tasks across {len(live)} workers",
                            moved=moved, workers=len(live))
            except Exception: pass
        return {"moved": moved, "live_workers": live, "load": load}

    @app.post("/api/workers/rebalance")
    def api_rebalance_workers():
        return jsonify({"ok": True, **_do_rebalance()})

    # ------------------------------------------------------------------
    # Built-in observability time series
    # ------------------------------------------------------------------
    from collections import deque as _deque
    _obs_buffer: "_deque[dict]" = _deque(maxlen=300)  # 10 min @ 2 s sampling

    def _sample_obs():
        try:
            counts = {"live": 0, "idle": 0, "busy": 0, "dead": 0}
            workers = []
            now = time.time()
            for w in worker_registry._workers.keys():  # all known workers
                last_ts = worker_registry._workers.get(w)
                age = now - last_ts if last_ts else None
                is_live = (last_ts is not None
                           and age is not None
                           and age <= worker_registry._timeout)
                # Determine busy/idle from PENDING tasks attributed to this worker.
                pending = sum(
                    1 for k, v in node.data_store.items()
                    if k.startswith("result:") and isinstance(v, dict)
                    and v.get("worker_id") == w and v.get("status") == "PENDING"
                )
                status = "dead" if not is_live else ("busy" if pending > 0 else "idle")
                counts[status] += 1
                if is_live:
                    counts["live"] += 1
                workers.append({"worker_id": w, "status": status, "pending": pending})

            # HT1 status counts
            ht1_pending = ht1_success = ht1_failure = 0
            for k, v in node.data_store.items():
                if not k.startswith("result:") or not isinstance(v, dict):
                    continue
                st = v.get("status")
                if st == "PENDING":
                    ht1_pending += 1
                elif st == "SUCCESS":
                    ht1_success += 1
                elif st == "FAILURE":
                    ht1_failure += 1

            _obs_buffer.append({
                "ts": now,
                "workers": counts,
                "queue": ht1_pending,
                "success": ht1_success,
                "failure": ht1_failure,
            })
            # Publish to Prometheus too so Grafana can chart them.
            try:
                from chord import metrics_registry as _M
                nid = str(node.node_id)
                _M.WORKERS_LIVE.labels(node_id=nid).set(counts["live"])
                _M.WORKERS_IDLE.labels(node_id=nid).set(counts["idle"])
                _M.WORKERS_BUSY.labels(node_id=nid).set(counts["busy"])
                _M.WORKERS_DEAD.labels(node_id=nid).set(counts["dead"])
                _M.TASKS_PENDING.labels(node_id=nid).set(ht1_pending)
                _M.TASKS_SUCCESS.labels(node_id=nid).set(ht1_success)
                _M.TASKS_FAILURE.labels(node_id=nid).set(ht1_failure)
            except Exception:
                pass
        except Exception:
            logger.exception("[observability] sample failed")

    def _obs_loop():
        while True:
            _sample_obs()
            time.sleep(2.0)
    threading.Thread(target=_obs_loop, daemon=True, name="obs-sampler").start()

    # Discrete event log — each entry: {ts, kind, message, data}.
    _obs_events: "_deque[dict]" = _deque(maxlen=200)

    def _obs_event(kind: str, message: str, **data) -> None:
        _obs_events.append({
            "ts": time.time(),
            "kind": kind,
            "message": message,
            "data": data or {},
        })

    # Hand it to the rest of the module so other handlers can record events.
    app.config["obs_event"] = _obs_event
    _obs_event("system_start", f"node {node.node_id} bound to {node.address}")

    @app.get("/api/observability/events")
    def api_obs_events():
        return jsonify({"events": list(_obs_events)})

    @app.get("/api/observability/trace/<task_id>")
    def api_obs_trace(task_id):
        """
        Build a per-task trace from the HT1 record and any related events.
        Spans:
          submit  : created_at → first_pending_seen
          execute : first_pending_seen → updated_at  (worker run window)
        """
        try:
            rec = node.get(f"result:{task_id}")
        except Exception:
            rec = None
        if not isinstance(rec, dict):
            return jsonify({"ok": False, "error": "task not found"}), 404
        related = [e for e in _obs_events if e.get("data", {}).get("task_id") == task_id]
        return jsonify({
            "ok": True,
            "task_id": task_id,
            "record": {
                "task_type": rec.get("task_type"),
                "status": rec.get("status"),
                "worker_id": rec.get("worker_id"),
                "assigned_by": rec.get("assigned_by"),
                "created_at": rec.get("created_at"),
                "updated_at": rec.get("updated_at"),
                "result": rec.get("result"),
            },
            "events": related,
        })

    @app.get("/api/observability/timeseries")
    def api_obs_timeseries():
        # Return the buffer + a few derived rates for the client.
        series = list(_obs_buffer)
        # Compute throughput: tasks/min completed in the last minute.
        if len(series) >= 2:
            head, tail = series[0], series[-1]
            window = max(1.0, tail["ts"] - head["ts"])
            tput = (tail["success"] - head["success"]) / window * 60.0  # per min
            failrate = (tail["failure"] - head["failure"]) / window * 60.0
        else:
            tput = failrate = 0.0
        return jsonify({
            "samples": series,
            "throughput_per_min": round(tput, 2),
            "failures_per_min": round(failrate, 2),
            "current": series[-1] if series else None,
        })

    @app.post("/api/demo/run")
    def api_demo_run():
        """
        Fire-and-forget: submit N demo tasks (rotating sleep 3..7s) in a
        background thread. Each task script prints the Welcome line.
        """
        body = request.get_json(silent=True) or {}
        try:
            n = max(1, min(int(body.get("count") or 10), 200))
        except Exception:
            n = 10
        sleeps = [3, 4, 5, 6, 7]
        welcome = "Welcome to Rakesh Ranjan's Distributed Class"
        ts = int(time.time())

        def background():
            for idx in range(1, n + 1):
                sleep_s = sleeps[(idx - 1) % len(sleeps)]
                tid = f"demo-{ts}-{idx:03d}"
                script = (
                    f'echo "[{tid}] starting (sleep {sleep_s}s)"; '
                    f'sleep {sleep_s}; '
                    f'echo "{welcome}"; '
                    f'echo "[{tid}] done"'
                )
                try:
                    _requests.post(
                        f"http://{node.address}/createTask",
                        json={"task_id": tid,
                              "task_details": {"task_type": "SCRIPT", "path": "", "script": script}},
                        timeout=5,
                    )
                except Exception:
                    pass
                time.sleep(0.05)
        threading.Thread(target=background, daemon=True).start()
        try: _obs_event("demo_run", f"launched {n} demo tasks", count=n)
        except Exception: pass
        return jsonify({"ok": True, "submitting": n, "ts": ts})

    @app.post("/api/workers/<wid>/kill")
    def api_kill_worker(wid):
        # Try the dashboard-tracked PID first; if absent, fall back to pgrep
        # so we can also kill workers launched from the terminal.
        pid = _worker_pids.get(wid)
        if not pid:
            try:
                out = subprocess.check_output(
                    ["pgrep", "-f", f"chord.task_runner.*--worker-id {wid}"],
                    text=True,
                ).strip()
                pids = [int(x) for x in out.splitlines() if x.strip()]
                if pids:
                    pid = pids[0]
            except subprocess.CalledProcessError:
                pid = None
            except FileNotFoundError:
                # pgrep not available — give up
                pid = None
        if not pid:
            return jsonify({"ok": False, "error": f"no process found for worker {wid}"}), 404
        try:
            os.kill(pid, 9)
            _worker_pids.pop(wid, None)
            try: _obs_event("worker_killed", f"worker {wid} stopped (pid {pid})", worker_id=wid, pid=pid)
            except Exception: pass
            return jsonify({"ok": True, "pid": pid})
        except ProcessLookupError:
            _worker_pids.pop(wid, None)
            return jsonify({"ok": True, "note": "already dead"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/workers/status")
    def workers_status():
        """
        Phase 5-ish dashboard endpoint.

        For every worker ever heartbeated, classify as:
          - busy: alive AND has at least one PENDING task in HT1
          - idle: alive AND no PENDING tasks
          - dead: not heartbeated within HEARTBEAT_TIMEOUT_S

        Returns counts + per-worker rows. Reads HT2 + HT1 per worker; this
        is O(workers × tasks) per call, fine for the dashboard's poll rate.
        """
        all_workers = worker_registry.all_workers()
        live_set = set(worker_registry.live_workers())

        rows = []
        counts = {"live": 0, "idle": 0, "busy": 0, "dead": 0}

        for wid, _ts, age_s in all_workers:
            try:
                tids = worker_assignment_service.get(wid)
            except Exception:
                tids = []
            pending = 0
            for tid in tids:
                try:
                    rec = result_service.get(tid)
                    if rec and rec.get("status") == "PENDING":
                        pending += 1
                except Exception:
                    pass

            if wid in live_set:
                status = "idle" if pending == 0 else "busy"
                counts["live"] += 1
                counts[status] += 1
            else:
                status = "dead"
                counts["dead"] += 1

            rows.append({
                "worker_id": wid,
                "status": status,
                "task_count": len(tids),
                "pending": pending,
                "last_seen_seconds_ago": round(age_s, 1),
            })

        return jsonify({"counts": counts, "workers": rows})

    @app.post("/createTask")
    def create_task_v2():
        """
        Phase 3 atomic-acceptance protocol.

        1. Idempotency check on result:<task_id>
        2. Determine worker (provided or round-robin)
        3. Write HT1 (result:<task_id>) with timeout + retries
        4. Append HT2 (worker:<worker_id>) with timeout + retries
           - on failure: rollback HT1 (best-effort) and 503
        5. Return 201 on success

        Test hook: PHASE3_FORCE_FAIL=ht1|ht2 injects a failure at the
        named step so rollback paths can be exercised end-to-end.
        """
        body = request.get_json(silent=True) or {}
        task_id = body.get("task_id")
        task_details = body.get("task_details") or {}
        provided_worker = body.get("worker_id")

        # ---- validation ----
        if not task_id:
            return jsonify({"message": "Task rejected", "reason": "task_id required"}), 422
        if not isinstance(task_details, dict):
            return jsonify({"message": "Task rejected", "reason": "task_details must be an object"}), 422
        task_type = task_details.get("task_type")
        if task_type not in _VALID_TASK_TYPES:
            return jsonify({
                "message": "Task rejected",
                "reason": f"task_details.task_type must be one of {sorted(_VALID_TASK_TYPES)}",
            }), 422

        # ---- Step 0: idempotency ----
        try:
            existing = result_service.get(task_id)
        except Exception as exc:
            logger.warning("[createTask] idempotency check failed: %s", exc)
            existing = None
        if existing is not None:
            return jsonify({
                "message": "Task already exists",
                "task_id": task_id,
                "status": existing["status"],
                "worker_id": existing["worker_id"],
                "assigned_by": existing["assigned_by"],
            }), 200

        # ---- Step 1: determine worker ----
        if provided_worker:
            worker_id = provided_worker
            assigned_by = "user"
        else:
            worker_id = worker_registry.round_robin_assign()
            if worker_id is None:
                return jsonify({
                    "message": "Task rejected",
                    "reason": "no live workers available for auto-assignment",
                }), 503
            assigned_by = "frontend"

        # ---- Step 2: build the result record ----
        try:
            record = build_result_record(
                task_id=task_id,
                task_type=task_type,
                path=task_details.get("path", ""),
                script=task_details.get("script", ""),
                worker_id=worker_id,
                assigned_by=assigned_by,
                status="PENDING",
                result=None,
            )
        except ResultValidationError as exc:
            return jsonify({"message": "Task rejected", "reason": str(exc)}), 422

        # Test hook: ?PHASE3_FORCE_FAIL=ht1|ht2 (read once per request)
        _inject = (os.environ.get("PHASE3_FORCE_FAIL") or "").lower()

        def _ht1_op():
            if _inject == "ht1":
                raise RuntimeError("injected HT1 failure")
            return result_service.put(task_id, record)

        def _ht2_op():
            if _inject == "ht2":
                raise RuntimeError("injected HT2 failure")
            return worker_assignment_service.append(worker_id, task_id)

        # ---- Step 3: HT1 write ----
        try:
            with_timeout_and_retry(_ht1_op, op_name=f"HT1.put({task_id})")
        except Exception as exc:
            logger.error("[createTask] HT1 write failed for %s: %s", task_id, exc)
            return jsonify({
                "message": "Task rejected",
                "reason": "Failed to persist task record",
            }), 503

        # ---- Step 4: HT2 append, with rollback on failure ----
        try:
            with_timeout_and_retry(
                _ht2_op, op_name=f"HT2.append({worker_id},{task_id})",
            )
        except Exception as exc:
            # ROLLBACK: remove the HT1 record so the system stays consistent.
            # Best-effort: a failed rollback leaves a "ghost" HT1 record;
            # log loudly and accept it (per spec, janitor in a future phase).
            logger.error(
                "[createTask] HT2 append failed for %s/%s: %s — rolling back HT1",
                task_id, worker_id, exc,
            )
            try:
                result_service.delete(task_id)
                logger.info("[createTask] rollback HT1.delete(%s) succeeded", task_id)
            except Exception as rb_exc:
                logger.error(
                    "[createTask] rollback HT1.delete(%s) FAILED: %s — ghost record left behind",
                    task_id, rb_exc,
                )
            return jsonify({
                "message": "Task rejected",
                "reason": "Failed to persist worker assignment",
            }), 503

        # ---- Step 5: success ----
        try:
            app.config.get("obs_event") and app.config["obs_event"](
                "task_created",
                f"task {task_id} → worker {worker_id} ({assigned_by})",
                task_id=task_id, worker_id=worker_id, assigned_by=assigned_by,
            )
        except Exception:
            pass
        return jsonify({
            "message": "Task accepted",
            "task_id": task_id,
            "worker_id": worker_id,
            "assigned_by": assigned_by,
        }), 201

    @app.get("/getStatus/<task_id>")
    def get_status_v2(task_id):
        """Phase 3: real HT1 read with replica fallback (in result_service.get)."""
        try:
            record = result_service.get(task_id)
        except Exception as exc:
            logger.exception("[getStatus] read failed for %s", task_id)
            return jsonify({"error": "lookup failed", "details": str(exc)}), 500
        if record is None:
            return jsonify({"error": "task not found", "task_id": task_id}), 404
        return jsonify({
            "task_id": task_id,
            "status": record["status"],
            "result": record["result"],
            "worker_id": record["worker_id"],
            "assigned_by": record["assigned_by"],
        }), 200

    # ------------------------------------------------------------------
    # Phase 3 — internal HT1/HT2 RPCs
    #
    # These power cross-node replication and the locked HT2 append. They
    # operate directly on this node's local store; routing is the
    # caller's responsibility (the service classes resolve the primary
    # via Chord first, then call these endpoints).
    # ------------------------------------------------------------------

    @app.post("/internal/results/replica/<task_id>")
    def result_replica_put(task_id):
        record = request.get_json()
        node.put(f"result:{task_id}", record)
        return jsonify({"ok": True, "stored_at": node.node_id})

    @app.get("/internal/results/replica/<task_id>")
    def result_replica_get(task_id):
        value = node.get(f"result:{task_id}")
        if value is None:
            return jsonify({"error": "not found"}), 404
        return jsonify({"record": value})

    @app.delete("/internal/results/replica/<task_id>")
    def result_replica_delete(task_id):
        deleted = node.delete(f"result:{task_id}")
        return jsonify({"ok": deleted})

    @app.post("/internal/workers/replica/<worker_id>")
    def worker_replica_put(worker_id):
        record = request.get_json()
        node.put(f"worker:{worker_id}", record)
        return jsonify({"ok": True, "stored_at": node.node_id})

    @app.get("/internal/workers/replica/<worker_id>")
    def worker_replica_get(worker_id):
        value = node.get(f"worker:{worker_id}")
        if value is None:
            return jsonify({"error": "not found"}), 404
        return jsonify({"record": value})

    @app.delete("/internal/workers/replica/<worker_id>")
    def worker_replica_delete(worker_id):
        deleted = node.delete(f"worker:{worker_id}")
        return jsonify({"ok": deleted})

    @app.post("/internal/workers/append/<worker_id>")
    def workers_append(worker_id):
        """
        Locked, primary-side append-to-list RPC. The lock is acquired
        inside append_local() (per-key, in worker_assignment._per_key_locks)
        and held for the entire read-modify-write+replicate cycle.
        """
        body = request.get_json(silent=True) or {}
        task_id = body.get("task_id")
        if not task_id:
            return jsonify({"ok": False, "error": "task_id required"}), 422
        try:
            result = worker_assignment_service.append_local(worker_id, task_id)
            return jsonify({"ok": True, **result})
        except Exception as exc:
            logger.exception("[workers/append] failed for %s/%s", worker_id, task_id)
            return jsonify({"ok": False, "error": str(exc)}), 500

    # ------------------------------------------------------------------
    # Phase 3 — debug endpoints (testing aids; safe to remove later)
    # ------------------------------------------------------------------

    @app.get("/debug/worker-tasks/<worker_id>")
    def debug_worker_tasks(worker_id):
        return jsonify({
            "worker_id": worker_id,
            "tasks": worker_assignment_service.get(worker_id),
        })

    @app.get("/debug/result-record/<task_id>")
    def debug_result_record(task_id):
        record = result_service.get(task_id)
        if record is None:
            return jsonify({"error": "not found", "task_id": task_id}), 404
        return jsonify(record)

    # ------------------------------------------------------------------
    # Phase 4 — worker → frontend completion endpoint
    #
    # Workers POST here when they finish executing a task.  The server
    # reads the existing HT1 record, patches status/result/updated_at,
    # and writes back via result_service.put() (k=3 quorum replication).
    # ------------------------------------------------------------------

    @app.post("/internal/results/<task_id>/complete")
    def result_complete(task_id):
        from datetime import datetime, timezone
        body = request.get_json(silent=True) or {}
        status = body.get("status")
        result_payload = body.get("result")
        if status not in {"SUCCESS", "FAILURE"}:
            return jsonify({
                "ok": False,
                "error": "status must be 'SUCCESS' or 'FAILURE'",
            }), 422

        existing = result_service.get(task_id)
        if existing is None:
            return jsonify({
                "ok": False, "error": "task not found", "task_id": task_id,
            }), 404

        updated = dict(existing)
        updated["status"] = status
        updated["result"] = result_payload
        updated["updated_at"] = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )

        try:
            result_service.put(task_id, updated)
        except Exception as exc:
            logger.exception("[results/complete] write failed for %s", task_id)
            return jsonify({"ok": False, "error": str(exc)}), 500

        # Only remove the task from HT2 on SUCCESS. Failed tasks stay in the
        # worker queue so the dashboard keeps showing them. Delay 3 s on
        # success so the assignment is visible before it disappears.
        worker_id = (existing or {}).get("worker_id")
        if worker_id and status == "SUCCESS":
            def _delayed_ht2_cleanup(wid=worker_id, tid=task_id):
                try:
                    time.sleep(3.0)
                    worker_assignment_service.remove(wid, tid)
                except Exception:
                    logger.exception(
                        "[results/complete] HT2 cleanup failed for %s/%s",
                        wid, tid,
                    )
            threading.Thread(target=_delayed_ht2_cleanup, daemon=True).start()

        # Update success-rate stats for placement scoring.
        try:
            worker_registry.record_completion(worker_id, status == "SUCCESS")
        except Exception:
            pass

        try: _obs_event(
            "task_succeeded" if status == "SUCCESS" else "task_failed",
            f"task {task_id} → {status} on {worker_id or '?'}",
            task_id=task_id, worker_id=worker_id, status=status,
        )
        except Exception: pass
        # Observe task duration so Grafana can plot p50/p95/p99 latency.
        try:
            from chord import metrics_registry as _M
            dur_ms = (result_payload or {}).get("duration_ms")
            if isinstance(dur_ms, (int, float)) and dur_ms >= 0:
                _M.TASK_DURATION.labels(
                    node_id=str(node.node_id), status=status,
                ).observe(dur_ms / 1000.0)
        except Exception:
            pass
        return jsonify({
            "ok": True, "task_id": task_id, "status": status,
        }), 200

    @app.post("/agent/chat")
    def agent_chat():
        body = request.get_json(silent=True) or {}
        message = body.get("message")
        history = body.get("history") or []
        session_id = body.get("session_id")

        if not message:
            return jsonify({"ok": False, "error": "message required"}), 422
        if not isinstance(history, list):
            return jsonify({"ok": False, "error": "history must be an array"}), 422

        try:
            agent = _get_conversation_agent()
            result = agent.chat(history=history, message=message, session_id=session_id)
            return jsonify(result)
        except RuntimeError as exc:
            # Anthropic key missing or similar config error
            return jsonify({"ok": False, "error": str(exc)}), 503
        except Exception as exc:
            logger.exception("[agent/chat] failed")
            return jsonify({"ok": False, "error": str(exc)}), 500

    return app


# ---------------------------------------------------------------------------
# File store helper
# ---------------------------------------------------------------------------

_FILE_SIZES = {
    "pdf": (50_000, 5_000_000), "csv": (1_000, 500_000),
    "bin": (500_000, 100_000_000), "yaml": (200, 10_000),
    "gz":  (10_000, 200_000_000), "zip": (5_000, 500_000_000),
    "pptx":(100_000, 20_000_000), "png": (10_000, 10_000_000),
    "jpg": (50_000, 8_000_000),   "mp4": (1_000_000, 2_000_000_000),
    "tar": (10_000, 500_000_000), "md":  (500, 50_000),
    "sql": (1_000, 10_000_000),   "json":(200, 5_000_000),
    "txt": (500, 1_000_000),      "sh":  (100, 50_000),
    "parquet":(50_000, 500_000_000),
}

def _fmt_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

def _ensure_file(node: ChordNode, filename: str) -> dict:
    """Return the file entry from the local store, creating it if absent."""
    key     = f"file:{filename}"
    content = node.get(key)
    if content is None:
        ext   = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        lo, hi = _FILE_SIZES.get(ext, (1_000, 10_000_000))
        size  = random.randint(lo, hi)
        content = {
            "filename":    filename,
            "file_type":   file_type(filename),
            "size_bytes":  size,
            "size_human":  _fmt_size(size),
            "created_at":  time.time(),
            "serve_count": 0,
            "stored_at":   node.node_id,
        }
        node.put(key, content)
    return content


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect_ring_metrics(node: ChordNode, transport) -> list[dict]:
    seen = set()
    result = []
    try:
        result.append(node.metrics())
        seen.add(node.node_id)
    except Exception:
        pass
    for finger in node.fingers:
        if finger.node_id is None or finger.node_id in seen:
            continue
        seen.add(finger.node_id)
        try:
            result.append(transport.get_metrics(finger.node_address))
        except Exception:
            pass
    return result


def _address_for(node: ChordNode, transport, target_node_id: int) -> str:
    """Resolve a node_id to its address via find_successor."""
    if target_node_id == node.node_id:
        return node.address
    for finger in node.fingers:
        if finger.node_id == target_node_id:
            return finger.node_address
    # Fall through to Chord routing
    responsible = node.find_successor(target_node_id)
    return responsible["address"]


def _store_job(node: ChordNode, transport, job: dict,
               target_address: str, target_node_id: int) -> str:
    """Store a job on the target node; returns the key used."""
    from chord.agent import make_job_key_for
    key = make_job_key_for(target_node_id)
    job_copy = dict(job)

    if target_node_id == node.node_id:
        node.put(key, job_copy)
    else:
        transport.put(target_address, key, job_copy)
    return key


# ---------------------------------------------------------------------------
# FailureWatcherThread — detects predecessor failure, triggers RecoveryAgent
# ---------------------------------------------------------------------------

class FailureWatcherThread(threading.Thread):
    """
    Watches the predecessor pointer. When it goes from Some → None (cleared by
    check_predecessor), collects orphaned jobs from the data store that were
    claimed by the failed node and runs the RecoveryAgent.
    """

    def __init__(self, node: ChordNode, agent, interval: float = 3.0):
        super().__init__(daemon=True, name=f"chord-failure-watcher-{node.node_id}")
        self.node = node
        self.agent = agent
        self.interval = interval
        self._stop = threading.Event()
        self._last_predecessor_id = None

    def run(self):
        logger.info(f"[FailureWatcher {self.node.node_id}] Started")
        while not self._stop.is_set():
            try:
                self._check()
            except Exception as e:
                logger.warning(f"[FailureWatcher] Error: {e}")
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()

    def _check(self):
        pred = self.node.predecessor
        current_pred_id = pred["id"] if pred else None

        # Predecessor disappeared — it just died
        if self._last_predecessor_id is not None and current_pred_id is None:
            failed_id = self._last_predecessor_id
            logger.info(f"[FailureWatcher] Predecessor {failed_id} died — starting recovery")
            activity.log(activity.NODE_FAIL,
                         f"Node {failed_id} failure detected — recovery starting",
                         {"failed_node_id": failed_id,
                          "detected_by": self.node.node_id})
            self._recover(failed_id)

        self._last_predecessor_id = current_pred_id

    def _recover(self, failed_node_id: int):
        transport = self.node._transport

        # Gather surviving nodes first (needed for both recovery paths)
        ring_metrics = _collect_ring_metrics(self.node, transport)
        surviving = [m for m in ring_metrics if m["node_id"] != failed_node_id]

        if not surviving:
            logger.warning("[FailureWatcher] No surviving nodes for recovery")
            return

        # ── Path 1: replica promotion (mid-execution recovery) ──────────────
        # Jobs that were running on the failed node are gone from its memory.
        # If the ReplicationAgent created replicas, they live on THIS node
        # (or other survivors) with replica_of set. Promote them to PENDING
        # so the local worker picks them up.
        promoted = []
        with self.node._lock:
            for k, v in list(self.node.data_store.items()):
                if not (k.startswith("job:") and isinstance(v, dict)):
                    continue
                # A replica whose primary was on the failed node
                primary_key = v.get("replica_of")
                if primary_key and v.get("status") in ACTIVE_STATUSES:
                    # Reset to PENDING so the local worker re-executes it
                    v["status"] = PENDING
                    v["claimed_by"] = None
                    v["started_at"] = None
                    v["replica_of"] = None  # promoted to primary
                    self.node.data_store[k] = v
                    promoted.append(v)
                    logger.info(
                        f"[FailureWatcher] Promoted replica {k} → PENDING "
                        f"(was replica of {primary_key})"
                    )

        if promoted:
            logger.info(
                f"[FailureWatcher] Promoted {len(promoted)} replicas after "
                f"node {failed_node_id} died mid-execution"
            )

        # ── Path 2: orphaned active jobs that were handed off to this node ──
        # These are jobs stored locally whose claimed_by address belongs to
        # the failed node (matched by address, not node_id, since claimed_by
        # stores "host:port").
        failed_addresses = {
            nd.get("address", "") for nd in ring_metrics
            # ring_metrics excludes the dead node, so match by node_id from
            # the last known state stored in finger table
        }
        # Build failed node's address from finger table
        failed_addr = None
        for f in self.node.fingers:
            if f.node_id == failed_node_id:
                failed_addr = f.node_address
                break

        with self.node._lock:
            orphaned = [
                v for k, v in self.node.data_store.items()
                if k.startswith("job:") and isinstance(v, dict)
                and v.get("status") in ACTIVE_STATUSES
                and not v.get("replica_of")  # skip replicas (handled above)
                and failed_addr is not None
                and v.get("claimed_by") == failed_addr
            ]

        if not orphaned and not promoted:
            logger.info(f"[FailureWatcher] No orphaned jobs from node {failed_node_id}")
            return

        if orphaned:
            result = self.agent.plan_recovery(failed_node_id, orphaned, surviving)
            assignments = result.get("assignments", {})
            logger.info(
                f"[FailureWatcher] Recovery plan: {len(assignments)} assignments. "
                f"Reason: {result.get('reasoning', '')}"
            )

            from chord.job import make_job
            for job in orphaned:
                target_node_id = assignments.get(job["job_id"])
                if target_node_id is None:
                    continue
                target_address = _address_for(self.node, transport, target_node_id)
                recovery_job = make_job(job["type"], job.get("payload", {}), job["job_id"])
                try:
                    _store_job(self.node, transport, recovery_job, target_address, target_node_id)
                    logger.info(
                        f"[FailureWatcher] Recovered job {job['job_id']} → node {target_node_id}"
                    )
                    activity.log(activity.JOB_RECOVER,
                                 f"Job {job['job_id'][:12]}… recovered → Node {target_node_id}",
                                 {"job_id": job["job_id"], "to_node": target_node_id,
                                  "job_type": job.get("type")})
                except Exception as e:
                    logger.error(
                        f"[FailureWatcher] Failed to recover job {job['job_id']}: {e}"
                    )


# ---------------------------------------------------------------------------
# Background maintenance thread
# ---------------------------------------------------------------------------

class MaintenanceThread(threading.Thread):
    def __init__(self, node: ChordNode, interval: float = 2.0):
        super().__init__(daemon=True, name=f"chord-maintenance-{node.node_id}")
        self.node = node
        self.interval = interval
        self._stop_event = threading.Event()

    def run(self):
        logger.info(f"[Maintenance] Started for node {self.node.node_id}")
        nid = str(self.node.node_id)
        while not self._stop_event.is_set():
            try:
                prev_pred = self.node.predecessor
                self.node.stabilize()
                STABILIZE_RUNS.labels(node_id=nid).inc()
                self.node.fix_fingers()
                FINGER_FIX_RUNS.labels(node_id=nid).inc()
                self.node.check_predecessor()
                if prev_pred and self.node.predecessor is None:
                    PREDECESSOR_FAILURES.labels(node_id=nid).inc()
            except Exception as e:
                logger.warning(f"[Maintenance] Error: {e}")
            self._stop_event.wait(self.interval)

    def stop(self):
        self._stop_event.set()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def start_node(host: str, port: int, known_address: str = None,
               node_id: int = None, maintenance_interval: float = 2.0,
               enable_worker: bool = False, worker_interval: float = 1.0,
               worker_threads: int = 4, agent_key: str = None,
               agent_loop_interval: float = 5.0,
               enable_dummy_client: bool = False,
               dummy_interval_min: float = 20.0,
               dummy_interval_max: float = 30.0,
               grpc_port: Optional[int] = None):
    import os
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    import logging as _logging
    _logging.basicConfig(
        level=getattr(_logging, log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    address = f"{host}:{port}"
    transport = HttpTransport()

    node = ChordNode(address=address, node_id=node_id)
    node.set_transport(transport)

    # Build agent
    from chord.agent import OrchestratorAgent
    agent = OrchestratorAgent(api_key=agent_key)

    app = create_app(node)
    app.config["agent"] = agent
    app.config["agent_key"] = agent_key  # used by Phase 2 ConversationAgent

    node.join(known_address)

    # Maintenance
    maint = MaintenanceThread(node, interval=maintenance_interval)
    maint.start()

    # Worker (optional)
    if enable_worker:
        from chord.worker import WorkerThread
        worker = WorkerThread(node, interval=worker_interval, max_workers=worker_threads)
        worker.start()
        logger.info(f"Worker started on node {node.node_id}")

    # Agent loop
    from chord.agent_loop import AgentLoop
    loop = AgentLoop(node, agent, interval=agent_loop_interval)
    loop.start()

    # Failure watcher
    watcher = FailureWatcherThread(node, agent, interval=maintenance_interval)
    watcher.start()

    # Dummy client (optional)
    if enable_dummy_client:
        from chord.dummy_client import DummyClient
        dc = DummyClient(address, dummy_interval_min, dummy_interval_max)
        dc.start()
        logger.info(
            f"DummyClient started — requests every "
            f"{dummy_interval_min}–{dummy_interval_max}s"
        )

    grpc_server = None
    if grpc_port is not None:
        from api.grpc_server import start_grpc_server
        grpc_server = start_grpc_server(node=node, transport=transport, grpc_port=grpc_port)

    logger.info(f"Starting Chord node {node.node_id} on {address}")
    try:
        # Use waitress in production — it handles concurrent inter-node RPCs,
        # health checks, and UI polls without the GIL-related stalls of the
        # Flask dev server that caused nodes to appear dead under load.
        try:
            from waitress import serve as _waitress_serve
            logger.info("[Server] Using waitress WSGI server (threads=8)")
            _waitress_serve(
                app, host=host, port=port,
                threads=8,
                connection_limit=200,
                channel_timeout=30,
            )
        except ImportError:
            logger.warning(
                "[Server] waitress not installed — falling back to Flask dev server. "
                "Run: pip install waitress"
            )
            app.run(host=host, port=port, threaded=True)
    finally:
        maint.stop()
        if grpc_server is not None:
            grpc_server.stop(grace=1)
