"""
chord/activity.py — shared in-memory ring activity log.

Imported by both server.py and worker.py so all meaningful ring events
(job lifecycle, node joins/failures, agent decisions) flow into one
unified feed that the dashboard can display in real time.

Design notes
------------
- Thread-safe deque capped at MAX_ENTRIES.
- Zero external dependencies (no DB, no file I/O).
- Server aggregates entries from all ring nodes via /chord/activity_local.
"""

import time
import threading
from collections import deque
from typing import List, Dict

_log: deque = deque(maxlen=300)
_lock = threading.Lock()

# Event types (used as CSS class hooks in the frontend too)
JOB_SUBMIT  = "job_submit"
JOB_CLAIM   = "job_claim"
JOB_DONE    = "job_done"
JOB_FAILED  = "job_failed"
JOB_RETRY   = "job_retry"
JOB_RECOVER = "job_recover"
NODE_JOIN   = "node_join"
NODE_FAIL   = "node_fail"
AGENT       = "agent"
RING        = "ring"


def log(event_type: str, msg: str, details: dict = None) -> None:
    """Append an activity entry.  Non-blocking; never raises."""
    try:
        entry: Dict = {
            "ts":      time.time(),
            "type":    event_type,
            "msg":     msg,
            "details": details or {},
        }
        with _lock:
            _log.append(entry)
    except Exception:
        pass  # activity log must never crash the caller


def get_entries(limit: int = 150) -> List[Dict]:
    """Return up to *limit* most-recent entries (newest last)."""
    with _lock:
        entries = list(_log)
    return entries[-limit:]
