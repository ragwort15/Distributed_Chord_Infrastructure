import copy
import uuid
from typing import Optional, List
from datetime import datetime, timezone

TASK_SCHEMA_VERSION = 1
TASK_KIND = "task"

TASK_STATUSES = {
    "REGISTERED",
    "QUEUED",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "DELETED",
}


class TaskValidationError(ValueError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def generate_task_id() -> str:
    return uuid.uuid4().hex


def build_task_key(task_id: str) -> str:
    if not isinstance(task_id, str) or not task_id.strip():
        raise TaskValidationError("task_id must be a non-empty string")
    return f"task:{task_id.strip()}"


def is_task_record(value: Optional[dict]) -> bool:
    return isinstance(value, dict) and value.get("kind") == TASK_KIND


def _validate_required_task_fields(task_input: dict):
    if not isinstance(task_input, dict):
        raise TaskValidationError("task payload must be a JSON object")

    for field in ("job_id", "type", "payload"):
        if field not in task_input:
            raise TaskValidationError(f"missing required field: {field}")

    if not isinstance(task_input["job_id"], str) or not task_input["job_id"].strip():
        raise TaskValidationError("job_id must be a non-empty string")

    if not isinstance(task_input["type"], str) or not task_input["type"].strip():
        raise TaskValidationError("type must be a non-empty string")


def create_task_record(
    task_input: dict,
    owner_node: dict,
    replication_factor: int,
    task_id: Optional[str] = None,
) -> dict:
    _validate_required_task_fields(task_input)

    final_task_id = task_id or task_input.get("task_id") or generate_task_id()
    if not isinstance(final_task_id, str) or not final_task_id.strip():
        raise TaskValidationError("task_id must be a non-empty string")

    now = utc_now_iso()
    status = task_input.get("status", "REGISTERED")
    if status not in TASK_STATUSES:
        raise TaskValidationError(f"unsupported status: {status}")

    priority = task_input.get("priority", 0)
    if not isinstance(priority, int):
        raise TaskValidationError("priority must be an integer")

    record = {
        "schema_version": TASK_SCHEMA_VERSION,
        "kind": TASK_KIND,
        "task_id": final_task_id.strip(),
        "job_id": task_input["job_id"].strip(),
        "type": task_input["type"].strip(),
        "payload": copy.deepcopy(task_input["payload"]),
        "priority": priority,
        "status": status,
        "result": task_input.get("result"),
        "error": task_input.get("error"),
        "created_at": now,
        "updated_at": now,
        "version": 1,
        "deleted": False,
        "placement": {
            "primary_node": owner_node,
            "replication_factor": max(1, int(replication_factor)),
            "replica_nodes": [],
        },
    }

    validate_task_record(record)
    return record


def validate_task_record(record: dict):
    if not is_task_record(record):
        raise TaskValidationError("record is not a task object")

    if record.get("schema_version") != TASK_SCHEMA_VERSION:
        raise TaskValidationError("unsupported schema version")

    if record.get("status") not in TASK_STATUSES:
        raise TaskValidationError("invalid task status")

    placement = record.get("placement")
    if not isinstance(placement, dict):
        raise TaskValidationError("placement must be an object")

    if not isinstance(placement.get("primary_node"), dict):
        raise TaskValidationError("placement.primary_node must be an object")

    replica_nodes = placement.get("replica_nodes", [])
    if not isinstance(replica_nodes, list):
        raise TaskValidationError("placement.replica_nodes must be a list")


def set_replica_nodes(record: dict, replica_nodes: List[dict]) -> dict:
    updated = copy.deepcopy(record)
    updated["placement"]["replica_nodes"] = replica_nodes
    updated["updated_at"] = utc_now_iso()
    updated["version"] = int(updated.get("version", 1)) + 1
    validate_task_record(updated)
    return updated


def mark_task_deleted(record: dict) -> dict:
    updated = copy.deepcopy(record)
    updated["status"] = "DELETED"
    updated["deleted"] = True
    updated["updated_at"] = utc_now_iso()
    updated["version"] = int(updated.get("version", 1)) + 1
    validate_task_record(updated)
    return updated
