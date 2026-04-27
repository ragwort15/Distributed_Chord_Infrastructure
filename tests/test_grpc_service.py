import json
from unittest.mock import MagicMock

import grpc

from api import task_service_pb2
from api.grpc_server import InternalReplicationGrpcServicer, TaskServiceGrpcServicer
from chord.node import ChordNode
from storage.task_service import TaskService


class FakeContext:
    def __init__(self):
        self.code = None
        self.details = None

    def set_code(self, code):
        self.code = code

    def set_details(self, details):
        self.details = details


def make_service() -> TaskService:
    node = ChordNode(address="127.0.0.1:5001", node_id=10)
    transport = MagicMock()
    transport.get_state.return_value = {
        "successor": {"id": 10, "address": "127.0.0.1:5001"}
    }
    node.set_transport(transport)
    node.join(None)
    return TaskService(node=node, transport=transport)


def test_grpc_register_and_get_task():
    service = make_service()
    servicer = TaskServiceGrpcServicer(service)

    register_request = task_service_pb2.RegisterTaskRequest(
        task_id="grpc-task-1",
        job_id="job-1",
        type="demo.grpc",
        payload_json=json.dumps({"x": 42}),
        priority=7,
    )

    register_context = FakeContext()
    register_response = servicer.RegisterTask(register_request, register_context)

    assert register_context.code is None
    assert register_response.task.task_id == "grpc-task-1"
    assert register_response.task.priority == 7

    get_request = task_service_pb2.GetTaskRequest(
        task_id="grpc-task-1",
        allow_replica_read=True,
    )
    get_context = FakeContext()
    get_response = servicer.GetTask(get_request, get_context)

    assert get_context.code is None
    assert get_response.task.job_id == "job-1"
    assert json.loads(get_response.task.payload_json) == {"x": 42}


def test_grpc_get_task_not_found_sets_status():
    service = make_service()
    servicer = TaskServiceGrpcServicer(service)
    context = FakeContext()

    response = servicer.GetTask(
        task_service_pb2.GetTaskRequest(task_id="missing", allow_replica_read=True),
        context,
    )

    assert context.code == grpc.StatusCode.NOT_FOUND
    assert response.task.task_id == ""


def test_internal_replicate_task_rpc():
    service = make_service()
    servicer = InternalReplicationGrpcServicer(service)
    context = FakeContext()

    record = task_service_pb2.TaskRecord(
        schema_version="1",
        task_id="replica-task",
        job_id="job-r",
        type="demo",
        payload_json=json.dumps({"k": "v"}),
        priority=1,
        status="REGISTERED",
        created_at="2026-04-24T00:00:00Z",
        updated_at="2026-04-24T00:00:00Z",
        version=1,
        deleted=False,
        primary_node=task_service_pb2.NodeRef(id=10, address="127.0.0.1:5001"),
        replication_factor=1,
    )

    response = servicer.ReplicateTask(
        task_service_pb2.ReplicateTaskRequest(task_key="task:replica-task", task=record),
        context,
    )

    assert context.code is None
    assert response.ok is True
    stored = service.get_replica_local("task:replica-task")
    assert stored is not None
    assert stored["task_id"] == "replica-task"
