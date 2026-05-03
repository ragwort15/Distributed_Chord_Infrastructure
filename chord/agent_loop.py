"""
AgentLoop: continuously polls ring node state and logs decisions.

Runs as a background daemon thread alongside the Chord node.
Every `interval` seconds it:
  1. Collects metrics from all reachable ring nodes
  2. Checks for any PENDING jobs on the local store and triggers placement advice
  3. Logs the ring snapshot to the decision log

This satisfies the "agent decision loop: continuously polls node state" requirement.
"""

import threading
import time
import logging
from typing import List, Dict

from chord.agent import OrchestratorAgent, _log_decision

logger = logging.getLogger(__name__)


class AgentLoop(threading.Thread):
    """
    Daemon thread that polls ring state and emits structured decision-log entries.
    The actual job routing happens in server.py (POST /jobs); this loop provides
    continuous observability and can trigger proactive rebalancing advice.
    """

    def __init__(self, node, agent: OrchestratorAgent, interval: float = 5.0):
        super().__init__(daemon=True, name=f"agent-loop-{node.node_id}")
        self.node = node
        self.agent = agent
        self.interval = interval
        self._stop = threading.Event()

    def run(self):
        logger.info(f"[AgentLoop {self.node.node_id}] Started (interval={self.interval}s)")
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.warning(f"[AgentLoop {self.node.node_id}] Tick error: {e}")
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()

    def _tick(self):
        snapshot = self._collect_ring_metrics()
        if not snapshot:
            return

        # Log ring snapshot (used for evaluation / comparison against baseline)
        _log_decision(
            agent="AgentLoop",
            tool="ring_snapshot",
            inputs={"node_id": self.node.node_id},
            output={
                "ring_size": len(snapshot),
                "nodes": [
                    {"node_id": m["node_id"], "queue_depth": m["queue_depth"],
                     "completed": m["jobs_completed"], "failed": m["jobs_failed"]}
                    for m in snapshot
                ],
            },
            strategy="monitor",
            latency_ms=0,
        )

        # Check for locally queued PENDING jobs — emit placement advice
        pending = self._local_pending_jobs()
        if pending and len(snapshot) > 1:
            for job in pending[:3]:  # cap to avoid thundering-herd of API calls
                try:
                    advice = self.agent.select_placement(job, snapshot)
                    logger.debug(
                        f"[AgentLoop] Placement advice for {job['job_id']}: "
                        f"node={advice['node_id']} reason={advice['reasoning']}"
                    )
                except Exception as e:
                    logger.debug(f"[AgentLoop] Placement advice skipped: {e}")

    def _collect_ring_metrics(self) -> List[Dict]:
        """Walk full ring to collect /metrics from ALL reachable nodes.

        Previous behaviour: `break` on the first unreachable node, so a
        single dead node anywhere in the successor chain silently dropped
        all nodes behind it from analytics/placement decisions.

        Fix: when a node is unreachable we still need to advance the walk.
        We fall back to the node's finger-table entry for the midpoint of
        the ring (finger[len//2]) so we can skip over the dead node and
        continue collecting from healthy peers.  If no finger is available
        we simply skip and stop (better than breaking the whole loop).

        Retry policy: each individual HTTP call goes through the transport
        layer's _get() which already has 3-attempt exponential back-off.
        Here we apply a lightweight 1-retry directly via requests so that
        the ring walk itself doesn't wait 0.7 s × N nodes per tick.
        """
        import requests as _req
        result = []
        unreachable: List[str] = []

        # Always include self
        try:
            m = self.node.metrics()
            result.append(m)
            seen = {self.node.node_id}
        except Exception:
            seen = set()

        def _fetch_with_retry(url: str, retries: int = 1, timeout: float = 2.0):
            """One quick retry so a single packet-drop doesn't drop a node."""
            for attempt in range(retries + 1):
                try:
                    r = _req.get(url, timeout=timeout)
                    if r.status_code == 200:
                        return r.json()
                    return None
                except Exception:
                    if attempt == retries:
                        return None
                    time.sleep(0.1)

        # Full ring walk: follow successor chain
        current = self.node.successor
        visited_count = 0

        while current and current.get("id") not in seen and visited_count < 1000:
            node_id = current.get("id")
            address = current.get("address")
            seen.add(node_id)
            visited_count += 1

            # --- Metrics (best-effort, skip on failure) ---
            metrics = _fetch_with_retry(f"http://{address}/metrics")
            if metrics:
                result.append(metrics)

            # --- Advance walk via state (skip dead node, don't break) ---
            state = _fetch_with_retry(f"http://{address}/chord/state")
            if state:
                current = state.get("successor")
            else:
                # Node is unreachable — log it and attempt to skip forward
                # using our own finger table so we can still reach nodes
                # that sit further along the ring.
                unreachable.append(address)
                logger.debug(
                    "[AgentLoop %s] Node %s unreachable during ring walk — "
                    "attempting finger-table skip", self.node.node_id, address
                )
                # Try to find a finger that is not in `seen` to continue
                skipped = False
                with self.node._lock:
                    fingers = list(self.node.finger_table)
                for finger in fingers:
                    if finger and finger.get("id") not in seen:
                        current = finger
                        skipped = True
                        break
                if not skipped:
                    # No viable finger — ring walk is exhausted
                    break

        if unreachable:
            logger.info(
                "[AgentLoop %s] Ring walk: %d/%d nodes reached; unreachable: %s",
                self.node.node_id, len(result), visited_count, unreachable,
            )
        else:
            logger.debug(
                "[AgentLoop %s] Ring walk: collected metrics from %d nodes in %d steps",
                self.node.node_id, len(result), visited_count,
            )
        return result

    def _local_pending_jobs(self) -> List[Dict]:
        with self.node._lock:
            return [
                v for k, v in self.node.data_store.items()
                if k.startswith("job:") and isinstance(v, dict)
                and v.get("status") == "pending"
            ]
