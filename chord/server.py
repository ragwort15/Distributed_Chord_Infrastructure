"""
Flask HTTP server exposing all Chord RPC endpoints and the public data/job API.
"""

import os
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

logger = logging.getLogger(__name__)

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


# In-memory request log — last 100 entries, shared across all threads
_request_log: List[Dict] = []
_request_lock = threading.Lock()


def create_app(node: ChordNode) -> Flask:
    app = Flask(__name__)
    app.config["node"] = node
    task_service = TaskService(node=node, transport=node._transport)

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

    @app.post("/put/<key>")
    def routed_put(key):
        value = request.get_json()
        key_id = sha1_id(key)
        responsible = node.find_successor(key_id)
        if responsible["id"] == node.node_id:
            node.put(key, value)
            return jsonify({"ok": True, "key": key, "stored_at": node.node_id})
        transport = node._transport
        try:
            transport.put(responsible["address"], key, value)
            return jsonify({"ok": True, "key": key, "stored_at": responsible["id"]})
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    @app.get("/get/<key>")
    def routed_get(key):
        key_id = sha1_id(key)
        responsible = node.find_successor(key_id)
        if responsible["id"] == node.node_id:
            value = node.get(key)
            if value is None:
                return jsonify({"error": "not found"}), 404
            return jsonify(value)
        transport = node._transport
        try:
            value = transport.get(responsible["address"], key)
            if value is None:
                return jsonify({"error": "not found"}), 404
            return jsonify(value)
        except Exception as e:
            return jsonify({"error": str(e)}), 502

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
        """Retrieve a job by ID from whichever node holds it."""
        key = job_key(job_id)
        key_id = sha1_id(key)
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
            return jsonify({"error": str(e)}), 502

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

    @app.get("/")
    def dashboard():
        return send_from_directory(_STATIC_DIR, "index.html")

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
    # Dashboard API — agent decision log
    # ------------------------------------------------------------------

    @app.get("/api/logs")
    def api_logs():
        log_path = pathlib.Path(os.environ.get("AGENT_LOG_PATH", "agent_decisions.jsonl"))
        if not log_path.exists():
            return jsonify({"entries": []})
        lines = log_path.read_text().strip().splitlines()
        entries = []
        for line in lines[-40:]:
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
        return jsonify({"entries": entries})

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
            def _spawn():
                try:
                    env = os.environ.copy()
                    env['AGENT_STRATEGY'] = 'heuristic'
                    # Get the project root directory (parent of chord/)
                    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    subprocess.Popen([
                        'python3', 'run_node.py',
                        '--host', '127.0.0.1',
                        '--port', str(next_port),
                        '--join', join_addr,
                        '--worker',
                        '--log', 'INFO'
                    ], env=env, cwd=project_root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    logger.info(f"[API] Spawned new node on port {next_port} from {project_root}")
                except Exception as e:
                    logger.error(f"[API] Failed to spawn node: {e}", exc_info=True)
            
            threading.Thread(target=_spawn, daemon=True).start()
            
            return jsonify({
                "ok": True,
                "port": next_port,
                "address": f"127.0.0.1:{next_port}",
                "message": f"New node spawning on port {next_port}"
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
        FILE_REQUESTS.labels(node_id=nid_str, file_type=ftype).inc()
        FILE_REQUEST_HOPS.labels(node_id=nid_str).observe(hops)
        FILE_REQUEST_DURATION.labels(node_id=nid_str).observe(time.time() - t0)

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
        }
        with _request_lock:
            _request_log.append(entry)
            if len(_request_log) > 100:
                _request_log.pop(0)

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

        return jsonify({
            "ts":             now,
            "req_per_min":    len(recent_60s),
            "avg_hops":       round(avg_hops, 2),
            "hop_dist":       hop_dist,
            "node_loads":     node_loads,
            "total_requests": len(all_reqs),
            "jobs_completed": jobs_completed,
            "jobs_failed":    jobs_failed,
            "ring_size":      len(seen),
        })

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
        app.run(host=host, port=port, threaded=True)
    finally:
        maint.stop()
        if grpc_server is not None:
            grpc_server.stop(grace=1)
