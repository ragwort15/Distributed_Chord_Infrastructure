"""
Phase 3: ResultDetails record schema (HT1, separate keyspace).

Stored under ``result:<task_id>`` keys. Coexists with Phase 1's
``task:<task_id>`` records — the two schemas don't interfere.

Status vocabulary differs from Phase 1's intentionally:
  PENDING  - written by /createTask
  SUCCESS  - written by the worker after success (Phase 4)
  FAILURE  - written by the worker on failure (Phase 4)
"""

from datetime import datetime, timezone
from typing import Optional

RESULT_SCHEMA_VERSION = 1
RESULT_KIND = "result_details"

VALID_STATUSES = {"PENDING", "RUNNING", "SUCCESS", "FAILURE"}
VALID_TASK_TYPES = {"SCRIPT", "BINARY"}
VALID_ASSIGNED_BY = {"user", "frontend", "intelligence"}


class ResultValidationError(ValueError):
    pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_result_key(task_id: str) -> str:
    if not isinstance(task_id, str) or not task_id.strip():
        raise ResultValidationError("task_id must be a non-empty string")
    return f"result:{task_id.strip()}"


def is_result_record(value) -> bool:
    return isinstance(value, dict) and value.get("kind") == RESULT_KIND


def build_result_record(
    task_id: str,
    task_type: str,
    path: str,
    script: str,
    worker_id: str,
    assigned_by: str,
    status: str = "PENDING",
    result: Optional[str] = None,
    # Phase 7 — recovery tracking fields
    attempt_count: int = 0,
    max_attempts: int = 3,
    last_failure_reason: Optional[str] = None,
    retry_on_failure: bool = False,
    recovery_history: Optional[list] = None,
    recovery_status: Optional[str] = None,
    retry_at: Optional[float] = None,
    timed_out: bool = False,
) -> dict:
    if not task_id or not isinstance(task_id, str):
        raise ResultValidationError("task_id required")
    if task_type not in VALID_TASK_TYPES:
        raise ResultValidationError(
            f"task_type must be one of {sorted(VALID_TASK_TYPES)}"
        )
    if status not in VALID_STATUSES:
        raise ResultValidationError(
            f"status must be one of {sorted(VALID_STATUSES)}"
        )
    if assigned_by not in VALID_ASSIGNED_BY:
        raise ResultValidationError(
            f"assigned_by must be one of {sorted(VALID_ASSIGNED_BY)}"
        )
    if not worker_id or not isinstance(worker_id, str):
        raise ResultValidationError("worker_id required")

    now = _utc_now_iso()
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "task_id": task_id,
        "task_type": task_type,
        "path": path or "",
        "script": script or "",
        "status": status,
        "worker_id": worker_id,
        "assigned_by": assigned_by,
        "result": result,
        "created_at": now,
        "updated_at": now,
        # Phase 7 recovery fields
        "attempt_count": int(attempt_count),
        "max_attempts": int(max_attempts),
        "last_failure_reason": last_failure_reason,
        "retry_on_failure": bool(retry_on_failure),
        "recovery_history": list(recovery_history) if recovery_history is not None else [],
        "recovery_status": recovery_status,
        "retry_at": retry_at,
        "timed_out": bool(timed_out),
    }


def validate_result_record(record: dict) -> None:
    if not is_result_record(record):
        raise ResultValidationError("not a result_details record")
    if record.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise ResultValidationError("unsupported schema version")
    if record.get("status") not in VALID_STATUSES:
        raise ResultValidationError("invalid status")
    if record.get("task_type") not in VALID_TASK_TYPES:
        raise ResultValidationError("invalid task_type")
    if record.get("assigned_by") not in VALID_ASSIGNED_BY:
        raise ResultValidationError("invalid assigned_by")
    if not record.get("worker_id"):
        raise ResultValidationError("worker_id required")
