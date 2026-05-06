"""
Phase 7: Intelligent Failure Recovery.

RecoveryManager runs as a daemon thread and continuously scans for three
categories of failure:

  1. Worker crash      — worker stopped sending heartbeats; its PENDING/RUNNING
                         tasks are orphaned.
  2. Task timeout      — worker marked the task FAILURE with timed_out=True.
  3. Stuck task        — task is RUNNING but its assigned worker is now dead.

For each detected failure it calls choose_recovery_path() (pure, no I/O) and
then executes one of:

  RETRY_SAME       — re-queue on the same worker (or best available if dead)
  RETRY_DIFFERENT  — pick the highest-scored live worker (Phase 6 scoring)
  WAIT_AND_RETRY   — schedule a retry RECOVERY_WAIT_SECONDS in the future
  GIVE_UP          — mark FAILURE permanently

All HT1 writes merge new fields into the existing record dict so that fields
written by workers (e.g. result, updated_at) are preserved.
"""

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from chord.worker_config import (
    MAX_RECOVERY_ATTEMPTS,
    RECOVERY_CHECK_INTERVAL_SECONDS,
    RECOVERY_WAIT_SECONDS,
)

logger = logging.getLogger(__name__)

# Recovery path constants
RETRY_SAME      = "RETRY_SAME"
RETRY_DIFFERENT = "RETRY_DIFFERENT"
WAIT_AND_RETRY  = "WAIT_AND_RETRY"
GIVE_UP         = "GIVE_UP"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Pure decision function — no I/O, fully unit-testable
# ---------------------------------------------------------------------------

def choose_recovery_path(
    record: dict,
    failure_type: str,
    has_live_workers: bool,
) -> str:
    """
    Choose a recovery path given the task record, failure type, and whether
    any live workers are currently available.

    Args:
        record:           HT1 record dict (may lack Phase 7 keys for old records).
        failure_type:     "worker_crash" | "task_timeout" | "task_failure"
        has_live_workers: True if score_all_workers() returned at least one entry.

    Returns:
        One of RETRY_SAME, RETRY_DIFFERENT, WAIT_AND_RETRY, GIVE_UP.
    """
    attempt_count    = record.get("attempt_count", 0)
    max_attempts     = record.get("max_attempts", MAX_RECOVERY_ATTEMPTS)
    retry_on_failure = record.get("retry_on_failure", False)

    # Hard stop: exceeded max retries
    if attempt_count >= max_attempts:
        return GIVE_UP

    if failure_type == "task_timeout":
        return RETRY_DIFFERENT if has_live_workers else WAIT_AND_RETRY

    if failure_type == "worker_crash":
        return RETRY_DIFFERENT if has_live_workers else WAIT_AND_RETRY

    if failure_type == "task_failure":
        # Non-zero exit — only retry if user marked it safe to do so
        if retry_on_failure:
            return RETRY_DIFFERENT if has_live_workers else WAIT_AND_RETRY
        return GIVE_UP

    return GIVE_UP


# ---------------------------------------------------------------------------
# RecoveryManager
# ---------------------------------------------------------------------------

class RecoveryManager:
    """
    Daemon thread that periodically scans HT1/HT2 for failures and applies
    the recovery decision from choose_recovery_path().
    """

    def __init__(self, result_service, worker_assignment_service, worker_registry):
        self._result_svc  = result_service
        self._assign_svc  = worker_assignment_service
        self._registry    = worker_registry
        self._thread: Optional[threading.Thread] = None
        self._stop        = threading.Event()
        # Guard against the same task being recovered concurrently
        self._in_progress: set = set()
        self._in_progress_lock = threading.Lock()
        # Event logging for recovery timeline
        self._events = deque(maxlen=100)  # Keep last 100 events
        self._events_lock = threading.Lock()

    # ---- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._recovery_loop,
            name="recovery-manager",
            daemon=True,
        )
        self._thread.start()
        logger.info("[RecoveryManager] started (interval=%ss)", RECOVERY_CHECK_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._stop.set()

    def trigger(self) -> None:
        """Run one scan cycle immediately (used by /debug/trigger-recovery)."""
        try:
            self._scan()
        except Exception as exc:
            logger.warning("[RecoveryManager] trigger scan error: %s", exc)

    def get_events(self, since: float = 0.0) -> list:
        """
        Return events since the given timestamp (in seconds).
        
        Args:
            since: Unix timestamp; return events >= this time.
        
        Returns:
            List of event dicts: {ts, type, task_id, worker_id, message}
        """
        with self._events_lock:
            return [e for e in self._events if e["ts"] >= since]

    def _emit_event(self, event_type: str, task_id: str, worker_id: str = "", message: str = "") -> None:
        """Emit a recovery event for the timeline."""
        event = {
            "ts": time.time(),
            "type": event_type,
            "task_id": task_id,
            "worker_id": worker_id,
            "message": message,
        }
        with self._events_lock:
            self._events.append(event)

    # ---- main loop --------------------------------------------------------

    def _recovery_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan()
            except Exception as exc:
                logger.warning("[RecoveryManager] loop error: %s", exc)
            self._stop.wait(RECOVERY_CHECK_INTERVAL_SECONDS)

    def _scan(self) -> None:
        self._detect_worker_crashes()
        self._detect_timed_out_tasks()
        self._detect_failed_tasks()
        self._detect_stuck_tasks()
        self._retry_waiting_tasks()

    # ---- detection methods ------------------------------------------------

    def _detect_worker_crashes(self) -> None:
        """
        For every worker tracked in HT2 that is no longer live, recover its
        PENDING or RUNNING tasks.
        """
        live = set(self._registry.live_workers())
        # all_workers() returns [(worker_id, last_ts, age_s), ...]
        all_seen = {w for w, _ts, _age in self._registry.all_workers()}
        dead = all_seen - live
        for worker_id in dead:
            task_ids = self._assign_svc.get(worker_id)
            for task_id in task_ids:
                record = self._safe_get(task_id)
                if record is None:
                    continue
                if record.get("status") in ("PENDING", "RUNNING"):
                    self._emit_event(
                        "worker_crash",
                        task_id,
                        worker_id=worker_id,
                        message=f"Worker {worker_id} crashed"
                    )
                    self._attempt_recovery(record, "worker_crash")

    def _detect_timed_out_tasks(self) -> None:
        """
        Find tasks marked FAILURE with timed_out=True that haven't been
        recovered yet (recovery_status is still None).
        """
        for record in self._all_ht1_records():
            if (
                record.get("status") == "FAILURE"
                and record.get("timed_out")
                and record.get("recovery_status") is None
                and not record.get("recovery_history")  # not yet touched by recovery
            ):
                self._attempt_recovery(record, "task_timeout")

    def _detect_failed_tasks(self) -> None:
        """
        Find tasks with status FAILURE that completed normally (not timed out)
        and have not yet been permanently given up.
        Applies GIVE_UP or RETRY_DIFFERENT depending on retry_on_failure.
        """
        for record in self._all_ht1_records():
            history = record.get("recovery_history") or []
            if (
                record.get("status") == "FAILURE"
                and not record.get("timed_out")
                and record.get("recovery_status") is None
                and GIVE_UP not in history          # not already permanently failed
            ):
                self._attempt_recovery(record, "task_failure")

    def _detect_stuck_tasks(self) -> None:
        """
        A task is 'stuck' if its status is RUNNING but its assigned worker
        is no longer live.
        """
        for record in self._all_ht1_records():
            if record.get("status") == "RUNNING":
                worker_id = record.get("worker_id", "")
                if worker_id and not self._registry.is_live(worker_id):
                    self._attempt_recovery(record, "worker_crash")

    def _retry_waiting_tasks(self) -> None:
        """
        Re-attempt tasks that were put into WAIT_AND_RETRY and whose
        retry_at timestamp has now passed.
        """
        now = time.time()
        for record in self._all_ht1_records():
            if (
                record.get("recovery_status") == "WAITING_FOR_RETRY"
                and record.get("retry_at") is not None
                and record["retry_at"] <= now
            ):
                # Clear waiting state before re-attempting so we don't loop
                task_id = record["task_id"]
                updated = dict(record)
                updated["recovery_status"] = None
                updated["retry_at"] = None
                updated["updated_at"] = _utc_now_iso()
                self._safe_put(task_id, updated)
                self._attempt_recovery(updated, record.get("last_failure_reason", "worker_crash"))

    # ---- recovery execution -----------------------------------------------

    def _attempt_recovery(self, record: dict, failure_type: str) -> None:
        task_id = record.get("task_id")
        if not task_id:
            return

        # Deduplicate: skip if already being recovered in another cycle
        with self._in_progress_lock:
            if task_id in self._in_progress:
                return
            self._in_progress.add(task_id)

        try:
            # Always read a fresh copy to avoid stale state
            fresh = self._safe_get(task_id)
            if fresh is None:
                return

            # Skip if already in a terminal/waiting state from a concurrent recovery
            if fresh.get("recovery_status") == "WAITING_FOR_RETRY":
                return
            if fresh.get("status") in ("SUCCESS",):
                return

            scored = self._registry.score_all_workers()
            has_workers = len(scored) > 0
            path = choose_recovery_path(fresh, failure_type, has_workers)

            logger.info(
                "[RecoveryManager] task=%s failure=%s attempt=%d/%d → %s",
                task_id,
                failure_type,
                fresh.get("attempt_count", 0),
                fresh.get("max_attempts", MAX_RECOVERY_ATTEMPTS),
                path,
            )

            self._execute(fresh, path, failure_type, scored)
        except Exception as exc:
            logger.warning("[RecoveryManager] recovery error for %s: %s", task_id, exc)
        finally:
            with self._in_progress_lock:
                self._in_progress.discard(task_id)

    def _execute(
        self,
        record: dict,
        path: str,
        failure_type: str,
        scored_workers: list,
    ) -> None:
        task_id = record["task_id"]
        updated = dict(record)
        updated["last_failure_reason"] = failure_type
        history = list(updated.get("recovery_history") or [])
        updated["updated_at"] = _utc_now_iso()

        if path == GIVE_UP:
            attempt = updated.get("attempt_count", 0)
            updated["status"] = "FAILURE"
            updated["result"] = f"Permanently failed after {attempt} attempt(s)"
            history.append("GIVE_UP")
            updated["recovery_history"] = history
            self._safe_put(task_id, updated)
            self._emit_event("give_up", task_id, message=f"Gave up after {attempt} attempt(s)")

        elif path == WAIT_AND_RETRY:
            retry_at = time.time() + RECOVERY_WAIT_SECONDS
            updated["recovery_status"] = "WAITING_FOR_RETRY"
            updated["retry_at"] = retry_at
            history.append(f"WAIT_AND_RETRY (retry at {retry_at:.0f})")
            updated["recovery_history"] = history
            self._safe_put(task_id, updated)
            self._emit_event("wait_retry", task_id, message=f"Waiting {RECOVERY_WAIT_SECONDS}s before retry")

        else:
            # RETRY_SAME or RETRY_DIFFERENT
            current_worker = updated.get("worker_id", "")
            if path == RETRY_SAME:
                if current_worker and self._registry.is_live(current_worker):
                    new_worker = current_worker
                else:
                    new_worker = scored_workers[0][0] if scored_workers else None
            else:  # RETRY_DIFFERENT — prefer any live worker other than the one that failed
                others = [w for w, _score in scored_workers if w != current_worker]
                new_worker = others[0] if others else (
                    scored_workers[0][0] if scored_workers else None
                )

            if new_worker is None:
                # No worker available after all — fall back to wait
                retry_at = time.time() + RECOVERY_WAIT_SECONDS
                updated["recovery_status"] = "WAITING_FOR_RETRY"
                updated["retry_at"] = retry_at
                history.append(f"WAIT_AND_RETRY (no workers; retry at {retry_at:.0f})")
                updated["recovery_history"] = history
                self._safe_put(task_id, updated)
                self._emit_event("wait_retry", task_id, message="No workers available, waiting for retry")
                return

            updated["attempt_count"] = updated.get("attempt_count", 0) + 1
            updated["worker_id"]    = new_worker
            updated["status"]       = "PENDING"
            updated["recovery_status"] = None
            updated["retry_at"]     = None
            updated["timed_out"]    = False
            history.append(f"{path} on {new_worker}")
            updated["recovery_history"] = history
            self._safe_put(task_id, updated)

            # Re-assign in HT2 so the new worker picks up the task
            try:
                self._assign_svc.append(new_worker, task_id)
                self._emit_event(
                    "retry",
                    task_id,
                    worker_id=new_worker,
                    message=f"{path} on worker {new_worker}"
                )
            except Exception as exc:
                logger.warning(
                    "[RecoveryManager] HT2 append failed for %s → %s: %s",
                    task_id, new_worker, exc,
                )
                self._emit_event(
                    "error",
                    task_id,
                    worker_id=new_worker,
                    message=f"Failed to assign to {new_worker}: {exc}"
                )

    # ---- helpers ----------------------------------------------------------

    def _safe_get(self, task_id: str) -> Optional[dict]:
        try:
            return self._result_svc.get(task_id)
        except Exception as exc:
            logger.warning("[RecoveryManager] HT1 read failed for %s: %s", task_id, exc)
            return None

    def _safe_put(self, task_id: str, record: dict) -> None:
        try:
            self._result_svc.put(task_id, record)
        except Exception as exc:
            logger.warning("[RecoveryManager] HT1 write failed for %s: %s", task_id, exc)

    def _all_ht1_records(self) -> list:
        """
        Best-effort scan of all result: records visible from the local node.
        Uses the node's data_store directly — only sees records homed here
        (primary or replica), which is sufficient for the recovery scan.
        """
        try:
            node = self._result_svc.node
            with node._lock:
                return [
                    v for k, v in node.data_store.items()
                    if isinstance(k, str)
                    and k.startswith("result:")
                    and isinstance(v, dict)
                    and v.get("kind") == "result_details"
                ]
        except Exception as exc:
            logger.warning("[RecoveryManager] data_store scan failed: %s", exc)
            return []
