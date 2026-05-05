"""
Phase 2: Live worker registry + pure round-robin auto-assignment.

This module is intentionally independent of the Chord/DHT layer.  It tracks
which workers have heartbeated recently and hands out worker_ids in O(1)
via a rotating index.

Storage of the actual task records (HT1) and worker->task list (HT2) is
Phase 3 work and is NOT done here.
"""

import threading
import time
from typing import List, Optional


DEFAULT_HEARTBEAT_TIMEOUT_S = 10.0


class WorkerRegistry:
    def __init__(self, heartbeat_timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S):
        self._workers: dict = {}          # worker_id -> last_heartbeat_ts
        self._lock = threading.Lock()
        self._rr_index = -1
        self._timeout = heartbeat_timeout_s

    def heartbeat(self, worker_id: str) -> None:
        if not worker_id:
            raise ValueError("worker_id required")
        with self._lock:
            self._workers[worker_id] = time.time()

    def remove(self, worker_id: str) -> bool:
        with self._lock:
            return self._workers.pop(worker_id, None) is not None

    def live_workers(self) -> List[str]:
        with self._lock:
            return self._live_locked()

    def round_robin_assign(self) -> Optional[str]:
        with self._lock:
            live = self._live_locked()
            if not live:
                return None
            self._rr_index = (self._rr_index + 1) % len(live)
            return live[self._rr_index]

    def _live_locked(self) -> List[str]:
        # timeout <= 0 disables expiration: every heartbeated worker stays live
        if self._timeout <= 0:
            return sorted(self._workers.keys())
        now = time.time()
        return sorted(
            w for w, t in self._workers.items()
            if now - t <= self._timeout
        )
