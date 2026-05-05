"""
Phase 2: Live worker registry + (originally) round-robin auto-assignment.

Phase 6: Extended with per-worker metrics (pending_tasks, latency) and
per-worker stats (total/successful/failed) so /createTask can do
intelligent placement instead of round-robin.

Storage of the actual task records (HT1) and worker->task list (HT2) is
Phase 3 work and is NOT done here.
"""

import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from chord.worker_config import (
    PLACEMENT_WEIGHT_LOAD,
    PLACEMENT_WEIGHT_LATENCY,
    PLACEMENT_WEIGHT_AVAILABILITY,
    LATENCY_NORMALIZATION_MS,
)


DEFAULT_HEARTBEAT_TIMEOUT_S = 10.0


# ---------------------------------------------------------------------------
# Phase 6 — per-worker state
# ---------------------------------------------------------------------------

@dataclass
class WorkerMetrics:
    """Snapshot from a worker's most recent heartbeat."""
    worker_id: str
    pending_tasks: int = 0
    latency_ms: float = 0.0
    last_heartbeat_ts: float = 0.0  # server-side wall clock when received


@dataclass
class WorkerStats:
    """Cumulative completion counters for a worker. In-memory; resets on
    server restart (Phase 8c moves to DHT)."""
    worker_id: str
    total_executed: int = 0
    successful: int = 0
    failed: int = 0

    @property
    def success_rate(self) -> float:
        if self.total_executed == 0:
            return 1.0  # optimistic — no failures yet
        return self.successful / self.total_executed

    def add_result(self, is_success: bool) -> None:
        self.total_executed += 1
        if is_success:
            self.successful += 1
        else:
            self.failed += 1


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class WorkerRegistry:
    def __init__(self, heartbeat_timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S):
        # Phase 2: timestamps drive live/dead classification
        self._workers: dict = {}          # worker_id -> last_heartbeat_ts
        # Phase 6: richer per-worker views
        self._metrics: Dict[str, WorkerMetrics] = {}
        self._stats: Dict[str, WorkerStats] = {}
        self._lock = threading.Lock()
        self._rr_index = -1
        self._timeout = heartbeat_timeout_s

    # ---- Phase 2 API (kept for back-compat) ---------------------------

    def heartbeat(self, worker_id: str,
                  pending_tasks: int = 0,
                  timestamp: Optional[float] = None) -> None:
        """
        Record a heartbeat.

        Phase 6: ``pending_tasks`` and ``timestamp`` feed the placement
        scorer. Both are optional — defaults yield load=0, latency=0 — so
        clients that haven't been upgraded keep working.
        """
        if not worker_id:
            raise ValueError("worker_id required")
        now = time.time()
        latency_ms = 0.0 if timestamp is None else max(0.0, (now - float(timestamp)) * 1000.0)
        with self._lock:
            self._workers[worker_id] = now
            self._metrics[worker_id] = WorkerMetrics(
                worker_id=worker_id,
                pending_tasks=int(pending_tasks or 0),
                latency_ms=latency_ms,
                last_heartbeat_ts=now,
            )
            self._stats.setdefault(worker_id, WorkerStats(worker_id=worker_id))

    def remove(self, worker_id: str) -> bool:
        with self._lock:
            self._metrics.pop(worker_id, None)
            return self._workers.pop(worker_id, None) is not None

    def live_workers(self) -> List[str]:
        with self._lock:
            return self._live_locked()

    def round_robin_assign(self) -> Optional[str]:
        """Phase 2 round-robin. Kept for backward-compat / fallback paths."""
        with self._lock:
            live = self._live_locked()
            if not live:
                return None
            self._rr_index = (self._rr_index + 1) % len(live)
            return live[self._rr_index]

    def _live_locked(self) -> List[str]:
        # timeout <= 0 disables expiration
        if self._timeout <= 0:
            return sorted(self._workers.keys())
        now = time.time()
        return sorted(
            w for w, t in self._workers.items()
            if now - t <= self._timeout
        )

    def dead_workers(self) -> List[tuple]:
        """Workers ever seen but no longer live. Returns [(worker_id, age_s)]."""
        with self._lock:
            if self._timeout <= 0:
                return []
            now = time.time()
            return sorted(
                (w, now - t) for w, t in self._workers.items()
                if now - t > self._timeout
            )

    def all_workers(self) -> List[tuple]:
        """All workers ever seen. Returns [(worker_id, last_ts, age_s)] sorted by id."""
        with self._lock:
            now = time.time()
            return sorted((w, t, now - t) for w, t in self._workers.items())

    # ---- Phase 6 API --------------------------------------------------

    def record_completion(self, worker_id: str, is_success: bool) -> None:
        """
        Called from /internal/results/<id>/complete after a worker reports
        a finished task. Updates the worker's running success rate.
        Single caller (server-side hook) → no double-counting.
        """
        if not worker_id:
            return
        with self._lock:
            stats = self._stats.setdefault(worker_id, WorkerStats(worker_id=worker_id))
            stats.add_result(bool(is_success))

    def get_metrics(self, worker_id: str) -> Optional[WorkerMetrics]:
        with self._lock:
            return self._metrics.get(worker_id)

    def get_stats(self, worker_id: str) -> WorkerStats:
        with self._lock:
            return self._stats.get(worker_id) or WorkerStats(worker_id=worker_id)

    def _score_locked(self, worker_id: str) -> float:
        """Compute placement score for one worker. Caller holds self._lock."""
        m = self._metrics.get(worker_id) or WorkerMetrics(worker_id=worker_id)
        s = self._stats.get(worker_id) or WorkerStats(worker_id=worker_id)
        load_score    = -float(m.pending_tasks)
        latency_score = -(m.latency_ms / LATENCY_NORMALIZATION_MS)
        availability  = s.success_rate
        return (
            PLACEMENT_WEIGHT_LOAD          * load_score
            + PLACEMENT_WEIGHT_LATENCY     * latency_score
            + PLACEMENT_WEIGHT_AVAILABILITY * availability
        )

    def score_all_workers(self) -> List[Tuple[str, float]]:
        """
        Score every LIVE worker. Returns [(worker_id, score), ...] sorted
        descending. workers[0] = the one /createTask should pick.
        """
        with self._lock:
            scored = [(wid, self._score_locked(wid)) for wid in self._live_locked()]
            return sorted(scored, key=lambda x: x[1], reverse=True)

    def metrics_snapshot(self) -> dict:
        """
        Whole-registry snapshot for /debug/worker-metrics. Live workers are
        sorted by score (descending); dead workers follow with score=None.
        """
        with self._lock:
            live_ids = set(self._live_locked())
            now = time.time()
            live_rows: List[dict] = []
            for wid in live_ids:
                m = self._metrics.get(wid) or WorkerMetrics(worker_id=wid)
                s = self._stats.get(wid) or WorkerStats(worker_id=wid)
                live_rows.append({
                    "worker_id": wid,
                    "is_live": True,
                    "pending_tasks": m.pending_tasks,
                    "latency_ms": round(m.latency_ms, 2),
                    "success_rate": round(s.success_rate, 4),
                    "total_executed": s.total_executed,
                    "successful": s.successful,
                    "failed": s.failed,
                    "score": round(self._score_locked(wid), 4),
                })
            live_rows.sort(key=lambda r: r["score"], reverse=True)

            dead_rows: List[dict] = []
            for wid, ts in self._workers.items():
                if wid in live_ids:
                    continue
                s = self._stats.get(wid) or WorkerStats(worker_id=wid)
                dead_rows.append({
                    "worker_id": wid,
                    "is_live": False,
                    "last_seen_seconds_ago": round(now - ts, 1),
                    "success_rate": round(s.success_rate, 4),
                    "total_executed": s.total_executed,
                    "successful": s.successful,
                    "failed": s.failed,
                    "score": None,
                })

            return {
                "weights": {
                    "load": PLACEMENT_WEIGHT_LOAD,
                    "latency": PLACEMENT_WEIGHT_LATENCY,
                    "availability": PLACEMENT_WEIGHT_AVAILABILITY,
                    "latency_normalization_ms": LATENCY_NORMALIZATION_MS,
                },
                "workers": live_rows + dead_rows,
            }
