"""
Worker thread: scans local DHT store for PENDING jobs, claims and executes them.
"""

import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor

from chord.job import PENDING, CLAIMED, RUNNING, DONE, FAILED, job_key

logger = logging.getLogger(__name__)


JOB_TIMEOUT = 120  # seconds before a RUNNING job is declared stuck and failed


class WorkerThread(threading.Thread):
    """
    Daemon thread that polls the node's local data_store for PENDING jobs,
    atomically claims them, then executes them in a thread pool.
    Also reaps jobs stuck in RUNNING state beyond JOB_TIMEOUT.
    """

    def __init__(self, node, interval: float = 1.0, max_workers: int = 4):
        super().__init__(daemon=True, name=f"chord-worker-{node.node_id}")
        self.node = node
        self.interval = interval
        self._stop_event = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=max_workers,
                                            thread_name_prefix=f"job-{node.node_id}")

    def run(self):
        logger.info(f"[Worker {self.node.node_id}] Started (interval={self.interval}s)")
        while not self._stop_event.is_set():
            try:
                self._scan_and_claim()
                self._reap_timed_out_jobs()
            except Exception as e:
                logger.warning(f"[Worker {self.node.node_id}] Scan error: {e}")
            self._stop_event.wait(self.interval)

    def _reap_timed_out_jobs(self):
        """Mark RUNNING jobs that exceeded JOB_TIMEOUT as FAILED."""
        now = time.time()
        with self.node._lock:
            for k, v in self.node.data_store.items():
                if not (k.startswith("job:") and isinstance(v, dict)):
                    continue
                if v.get("status") == RUNNING:
                    started = v.get("started_at") or now
                    if now - started > JOB_TIMEOUT:
                        v["status"] = FAILED
                        v["finished_at"] = now
                        v["error"] = f"timed out after {JOB_TIMEOUT}s"
                        self.node.data_store[k] = v
                        self.node.jobs_failed += 1
                        logger.warning(f"[Worker {self.node.node_id}] Job {k} timed out")

    def stop(self):
        self._stop_event.set()
        self._executor.shutdown(wait=False)

    def _scan_and_claim(self):
        with self.node._lock:
            pending = [
                (k, v) for k, v in self.node.data_store.items()
                if k.startswith("job:") and isinstance(v, dict) and v.get("status") == PENDING
            ]

        for key, job in pending:
            claimed = self._try_claim(key, job)
            if claimed:
                self._executor.submit(self._run_job, key, job)

    def _try_claim(self, key: str, job: dict) -> bool:
        """Atomically transition job from PENDING → CLAIMED."""
        with self.node._lock:
            current = self.node.data_store.get(key)
            if current is None or current.get("status") != PENDING:
                return False
            current["status"] = CLAIMED
            current["claimed_by"] = self.node.address
            current["started_at"] = time.time()
            self.node.data_store[key] = current
        logger.debug(f"[Worker {self.node.node_id}] Claimed {key}")
        return True

    def _run_job(self, key: str, job: dict):
        node = self.node
        with node._lock:
            entry = node.data_store.get(key)
            if entry:
                entry["status"] = RUNNING
                node.data_store[key] = entry

        try:
            result = _execute(job)
            with node._lock:
                entry = node.data_store.get(key)
                if entry:
                    entry["status"] = DONE
                    entry["finished_at"] = time.time()
                    entry["result"] = result
                    node.data_store[key] = entry
            node.jobs_completed += 1
            logger.info(f"[Worker {node.node_id}] Completed {key}")
            try:
                from chord.metrics_registry import JOBS_TOTAL
                JOBS_TOTAL.labels(node_id=str(node.node_id), status="done").inc()
            except Exception:
                pass
        except Exception as e:
            with node._lock:
                entry = node.data_store.get(key)
                if entry:
                    entry["status"] = FAILED
                    entry["finished_at"] = time.time()
                    entry["error"] = str(e)
                    node.data_store[key] = entry
            node.jobs_failed += 1
            logger.warning(f"[Worker {node.node_id}] Failed {key}: {e}")
            try:
                from chord.metrics_registry import JOBS_TOTAL
                JOBS_TOTAL.labels(node_id=str(node.node_id), status="failed").inc()
            except Exception:
                pass


def _execute(job: dict):
    """Execute a job by type. Raises on failure."""
    job_type = job.get("type", "")
    payload = job.get("payload", {})

    if job_type == "sleep":
        duration = float(payload.get("seconds", 1))
        time.sleep(duration)
        return {"slept": duration}

    elif job_type == "echo":
        message = payload.get("message", "")
        return {"echo": message}

    elif job_type == "compute":
        n = int(payload.get("n", 1000))
        total = sum(range(n))
        return {"sum": total, "n": n}

    else:
        raise ValueError(f"Unknown job type: {job_type!r}")
