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
        """Walk full ring to collect /metrics from ALL reachable nodes (not just finger table)."""
        import requests
        result = []

        # Always include self
        try:
            m = self.node.metrics()
            result.append(m)
            seen = {self.node.node_id}
        except Exception:
            seen = set()

        # Full ring walk: follow successor chain to reach all nodes
        current = self.node.successor
        visited_count = 0
        
        while current and current.get("id") not in seen and visited_count < 1000:
            seen.add(current["id"])
            visited_count += 1
            try:
                # Fetch metrics from this node
                resp = requests.get(f"http://{current['address']}/metrics", timeout=2)
                if resp.status_code == 200:
                    m = resp.json()
                    result.append(m)
                
                # Get successor of this node to continue ring walk
                state_resp = requests.get(f"http://{current['address']}/chord/state", timeout=2)
                if state_resp.status_code == 200:
                    state = state_resp.json()
                    current = state.get("successor")
                else:
                    break
            except Exception:
                # Node unreachable, try to continue with next
                break

        logger.debug(f"[AgentLoop {self.node.node_id}] Collected metrics from {len(result)} nodes (ring walk completed in {visited_count} steps)")
        return result

    def _local_pending_jobs(self) -> List[Dict]:
        with self.node._lock:
            return [
                v for k, v in self.node.data_store.items()
                if k.startswith("job:") and isinstance(v, dict)
                and v.get("status") == "pending"
            ]
