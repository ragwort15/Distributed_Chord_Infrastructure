from unittest.mock import MagicMock

import pytest

from chord.node import ChordNode
from storage.schema import build_task_key, create_task_record
from storage.task_service import TaskConflictError, TaskNotFoundError, TaskService


def make_node(address: str, node_id: int) -> ChordNode:
    node = ChordNode(address=address, node_id=node_id)
    transport = MagicMock()
    transport.get_state.return_value = {
        "successor": {"id": node_id, "address": address}
    }
    node.set_transport(transport)
    node.join(None)
    return node


def test_register_and_get_task_local_store():
    node = make_node("127.0.0.1:5001", 10)
    service = TaskService(node=node, transport=node._transport, replication_k=3)

    created = service.register_task(
        {
            "task_id": "task-1",
            "job_id": "job-1",
            "type": "test.work",
            "payload": {"x": 1},
        }
    )

    assert created["task"]["task_id"] == "task-1"

    fetched = service.get_task("task-1")
    assert fetched is not None
    assert fetched["job_id"] == "job-1"


def test_register_duplicate_task_conflict():
    node = make_node("127.0.0.1:5001", 10)
    service = TaskService(node=node, transport=node._transport, replication_k=2)

    payload = {
        "task_id": "task-dup",
        "job_id": "job-1",
        "type": "demo",
        "payload": {"a": 1},
    }
    service.register_task(payload)

    with pytest.raises(TaskConflictError):
        service.register_task(payload)


def test_soft_deregister_marks_deleted():
    node = make_node("127.0.0.1:5001", 10)
    service = TaskService(node=node, transport=node._transport)

    service.register_task(
        {
            "task_id": "task-delete",
            "job_id": "job-1",
            "type": "demo",
            "payload": {"a": 1},
        }
    )

    result = service.deregister_task("task-delete", hard_delete=False)
    assert result["task"]["status"] == "DELETED"


def test_hard_deregister_removes_record():
    node = make_node("127.0.0.1:5001", 10)
    service = TaskService(node=node, transport=node._transport)

    service.register_task(
        {
            "task_id": "task-hard-delete",
            "job_id": "job-1",
            "type": "demo",
            "payload": {"a": 1},
        }
    )

    service.deregister_task("task-hard-delete", hard_delete=True)
    assert service.get_task("task-hard-delete", allow_replica_read=False) is None


def test_deregister_non_existent_raises():
    node = make_node("127.0.0.1:5001", 10)
    service = TaskService(node=node, transport=node._transport)

    with pytest.raises(TaskNotFoundError):
        service.deregister_task("missing")


def test_query_local_tasks_filters_job_and_status():
    node = make_node("127.0.0.1:5001", 10)
    service = TaskService(node=node, transport=node._transport)

    alpha = create_task_record(
        task_input={"task_id": "a", "job_id": "job-a", "type": "demo", "payload": {}},
        owner_node={"id": 10, "address": "127.0.0.1:5001"},
        replication_factor=1,
    )
    beta = create_task_record(
        task_input={"task_id": "b", "job_id": "job-b", "type": "demo", "payload": {}},
        owner_node={"id": 10, "address": "127.0.0.1:5001"},
        replication_factor=1,
    )
    beta["status"] = "RUNNING"

    node.put(build_task_key("a"), alpha)
    node.put(build_task_key("b"), beta)

    result = service.query_local_tasks(job_id="job-b", status="RUNNING")
    assert len(result) == 1
    assert result[0]["task_id"] == "b"
