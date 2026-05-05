"""
Phase 4: standalone task-execution worker (separate process).

Long-running process that polls HT2 (worker:<my_worker_id>) for assigned
task_ids, fetches each task from HT1 (result:<task_id>), executes it as a
subprocess (SCRIPT or BINARY), and writes the result back to HT1.

Run as:
    python -m chord.task_runner --worker-id w-1 --frontend-url http://127.0.0.1:5001

Architecture:
  - Main thread runs the polling loop. One task at a time per worker
    (per Phase 4 spec).
  - Daemon thread sends heartbeats every HEARTBEAT_INTERVAL_SECONDS so
    the worker stays in /workers/live even during long-running tasks.
  - Crashes between subprocess-completion and result-write are recovered
    on next start: tasks still PENDING in HT1 get re-executed.

Distinct from chord/worker.py (Phase 1 in-process job thread); the two
modules have nothing in common conceptually — see PHASE 4 plan.
"""

import argparse
import logging
import signal
import threading
import time
from typing import List, Optional

import requests

from chord.executor import run_task
from chord.worker_config import (
    POLL_INTERVAL_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    EXECUTION_TIMEOUT_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    RESULT_REPORT_RETRIES,
)

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        worker_id: str,
        frontend_url: str,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
        execution_timeout: int = EXECUTION_TIMEOUT_SECONDS,
    ):
        self.worker_id = worker_id
        self.base_url = frontend_url.rstrip("/")
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.execution_timeout = execution_timeout

        # In-memory only. Repopulated from HT2 on every restart via
        # _recover_pending_tasks(). HT1 status is the source of truth.
        self.seen_task_ids: set[str] = set()

        # Phase 6: tasks we've started but not yet reported to /complete.
        # Drives the pending_tasks count we send in heartbeats so the
        # frontend's placement scorer can avoid loaded workers. Guarded by
        # _inflight_lock because the heartbeat thread reads it.
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None

    # -- lifecycle -----------------------------------------------------------

    def run(self):
        logger.info("[Worker %s] starting; frontend=%s", self.worker_id, self.base_url)

        # Send an immediate heartbeat so we appear in /workers/live right away
        self._send_heartbeat()

        # Background heartbeat thread
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name=f"hb-{self.worker_id}", daemon=True,
        )
        self._heartbeat_thread.start()

        # Recovery: re-execute tasks still PENDING from a prior crash.
        # Adds every assigned task_id to seen_task_ids regardless of whether
        # we re-execute, so the polling loop won't double-process.
        try:
            self._recover_pending_tasks()
        except Exception:
            logger.exception("[Worker %s] recovery raised; continuing into polling",
                             self.worker_id)

        try:
            self._polling_loop()
        finally:
            logger.info("[Worker %s] shutdown", self.worker_id)

    def stop(self, *_):
        logger.info("[Worker %s] stop signal received", self.worker_id)
        self._stop_event.set()

    # -- recovery + polling --------------------------------------------------

    def _recover_pending_tasks(self):
        assigned = self._fetch_assigned()
        logger.info("[Worker %s] recovery: %d task(s) in HT2",
                    self.worker_id, len(assigned))
        for tid in assigned:
            self.seen_task_ids.add(tid)        # claim BEFORE checking status
            record = self._fetch_record(tid)
            if record is None:
                logger.warning(
                    "[Worker %s] HT2 has %s but HT1 record missing — skipping",
                    self.worker_id, tid,
                )
                continue
            status = record.get("status")
            if status == "PENDING":
                logger.info(
                    "[Worker %s] re-executing PENDING task %s after restart",
                    self.worker_id, tid,
                )
                self._execute_and_report(tid, record)
            else:
                logger.debug(
                    "[Worker %s] skipping %s — already %s",
                    self.worker_id, tid, status,
                )

    def _polling_loop(self):
        while not self._stop_event.is_set():
            try:
                assigned = self._fetch_assigned()
                for tid in assigned:
                    if self._stop_event.is_set():
                        break
                    if tid in self.seen_task_ids:
                        continue
                    self.seen_task_ids.add(tid)   # claim BEFORE executing
                    record = self._fetch_record(tid)
                    if record is None:
                        logger.warning(
                            "[Worker %s] HT2 has %s but HT1 record missing — skipping",
                            self.worker_id, tid,
                        )
                        continue
                    self._execute_and_report(tid, record)
            except Exception:
                logger.exception("[Worker %s] polling tick failed", self.worker_id)
            self._stop_event.wait(self.poll_interval)

    # -- execution -----------------------------------------------------------

    def _execute_and_report(self, task_id: str, record: dict):
        task_type = record.get("task_type")
        path = record.get("path") or ""
        script = record.get("script") or ""
        logger.info("[Worker %s] executing %s (%s)", self.worker_id, task_id, task_type)

        # Phase 6: count this task as in-flight so heartbeats reflect load.
        with self._inflight_lock:
            self._inflight.add(task_id)
        try:
            try:
                result = run_task(
                    task_type=task_type, path=path, script=script,
                    timeout_s=self.execution_timeout,
                )
            except Exception as exc:
                logger.exception("[Worker %s] execution raised for %s",
                                 self.worker_id, task_id)
                self._report(task_id, status="FAILURE", result_payload={
                    "exit_code": -1, "stdout": "",
                    "stderr": f"executor error: {exc}",
                    "duration_ms": 0, "timed_out": False,
                })
                return

            status = "SUCCESS" if (result.exit_code == 0 and not result.timed_out) else "FAILURE"
            logger.info(
                "[Worker %s] task %s finished status=%s exit=%s duration=%dms timed_out=%s",
                self.worker_id, task_id, status,
                result.exit_code, result.duration_ms, result.timed_out,
            )
            self._report(task_id, status=status, result_payload=result.to_dict())
        finally:
            with self._inflight_lock:
                self._inflight.discard(task_id)

    # -- HTTP helpers --------------------------------------------------------

    def _fetch_assigned(self) -> List[str]:
        try:
            r = requests.get(
                f"{self.base_url}/debug/worker-tasks/{self.worker_id}",
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            r.raise_for_status()
            return r.json().get("tasks") or []
        except Exception as exc:
            logger.warning("[Worker %s] _fetch_assigned failed: %s",
                           self.worker_id, exc)
            return []

    def _fetch_record(self, task_id: str) -> Optional[dict]:
        try:
            r = requests.get(
                f"{self.base_url}/debug/result-record/{task_id}",
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            logger.warning("[Worker %s] _fetch_record(%s) failed: %s",
                           self.worker_id, task_id, exc)
            return None

    def _report(self, task_id: str, status: str, result_payload: dict):
        """
        POST the completion to the frontend. Local retry-with-backoff —
        we don't import Phase 3's retry helper because it lives in the
        chord package and we want this worker to remain a thin client.
        """
        last_exc = None
        backoff = 0.1
        attempts = 1 + RESULT_REPORT_RETRIES
        for attempt in range(attempts):
            try:
                r = requests.post(
                    f"{self.base_url}/internal/results/{task_id}/complete",
                    json={"status": status, "result": result_payload},
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
                r.raise_for_status()
                return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "[Worker %s] _report(%s) attempt %d/%d failed: %s",
                    self.worker_id, task_id, attempt + 1, attempts, exc,
                )
                if attempt < attempts - 1:
                    time.sleep(backoff)
                    backoff *= 2
        logger.error(
            "[Worker %s] _report(%s) FAILED after %d attempts: %s — "
            "task remains PENDING in HT1, will be retried on next worker restart",
            self.worker_id, task_id, attempts, last_exc,
        )

    def _send_heartbeat(self):
        # Phase 6: include current load + send-time so the frontend's
        # placement scorer can compute load + latency.
        with self._inflight_lock:
            pending = len(self._inflight)
        try:
            requests.post(
                f"{self.base_url}/workers/heartbeat",
                json={
                    "worker_id": self.worker_id,
                    "pending_tasks": pending,
                    "timestamp": time.time(),
                },
                timeout=HTTP_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("[Worker %s] heartbeat failed: %s",
                           self.worker_id, exc)

    def _heartbeat_loop(self):
        while not self._stop_event.is_set():
            self._send_heartbeat()
            self._stop_event.wait(self.heartbeat_interval)


def main():
    parser = argparse.ArgumentParser(
        description="Phase 4 task-execution worker (polls HT2, executes via subprocess, "
                    "writes result to HT1)",
    )
    parser.add_argument("--worker-id", required=True,
                        help="Unique worker ID (e.g. worker-1)")
    parser.add_argument("--frontend-url", default="http://127.0.0.1:5001",
                        help="Base URL of any Chord node (default: %(default)s)")
    parser.add_argument("--poll-interval", type=float, default=POLL_INTERVAL_SECONDS,
                        help="Seconds between HT2 polls (default: %(default)s)")
    parser.add_argument("--heartbeat-interval", type=float, default=HEARTBEAT_INTERVAL_SECONDS,
                        help="Seconds between heartbeats (default: %(default)s)")
    parser.add_argument("--execution-timeout", type=int, default=EXECUTION_TIMEOUT_SECONDS,
                        help="Per-task wall-clock timeout in seconds (default: %(default)s)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    w = Worker(
        worker_id=args.worker_id,
        frontend_url=args.frontend_url,
        poll_interval=args.poll_interval,
        heartbeat_interval=args.heartbeat_interval,
        execution_timeout=args.execution_timeout,
    )

    signal.signal(signal.SIGINT, w.stop)
    signal.signal(signal.SIGTERM, w.stop)

    w.run()


if __name__ == "__main__":
    main()
