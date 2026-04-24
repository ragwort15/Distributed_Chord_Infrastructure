"""Job data model for the distributed job execution platform."""

import time
import uuid

PENDING = "pending"
CLAIMED = "claimed"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

ACTIVE_STATUSES = {PENDING, CLAIMED, RUNNING}


def make_job(job_type: str, payload: dict, job_id: str = None) -> dict:
    return {
        "job_id": job_id or str(uuid.uuid4()),
        "type": job_type,
        "payload": payload,
        "status": PENDING,
        "created_at": time.time(),
        "claimed_by": None,
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
    }


def job_key(job_id: str) -> str:
    return f"job:{job_id}"
