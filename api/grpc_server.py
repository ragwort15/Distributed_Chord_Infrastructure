import json
import logging
from concurrent import futures

import grpc

from api import task_service_pb2, task_service_pb2_grpc
from storage.task_service import (
    TaskConflictError,
    TaskNotFoundError,
    TaskService,
    TaskValidationError,
)

logger = logging.getLogger(__name__)


def _json_dumps(value) -> str:
    if value is None:
        return ""
    return json.dumps(value)


def _json_loads(value: str):
    if not value:
        return None
    return json.loads(value)


from typing import Optional

def _node_ref_from_dict(node_ref: Optional[dict]):
    if not node_ref:
        return None
    return task_service_pb2.NodeRef(
        id=int(node_ref.get("id", 0)),
        address=node_ref.get("address", ""),
    )


def _task_record_to_proto(record: dict) -> task_service_pb2.TaskRecord:
    placement = record.get("placement", {})
    primary = _node_ref_from_dict(placement.get("primary_node"))
    replica_nodes = [
        _node_ref_from_dict(replica)
        for replica in placement.get("replica_nodes", [])
        if replica
    ]
    return task_service_pb2.TaskRecord(
        schema_version=str(record.get("schema_version", "")),
        task_id=record.get("task_id", ""),
        job_id=record.get("job_id", ""),
        type=record.get("type", ""),
        payload_json=_json_dumps(record.get("payload")),
        priority=int(record.get("priority", 0)),
        status=record.get("status", ""),
        result_json=_json_dumps(record.get("result")),
        error_json=_json_dumps(record.get("error")),
        created_at=record.get("created_at", ""),
        updated_at=record.get("updated_at", ""),
        version=int(record.get("version", 0)),
        deleted=bool(record.get("deleted", False)),
        primary_node=primary,
        replica_nodes=replica_nodes,
        replication_factor=int(placement.get("replication_factor", 1)),
    )


def _task_record_from_proto(proto: task_service_pb2.TaskRecord) -> dict:
    return {
        "schema_version": int(proto.schema_version) if proto.schema_version else 1,
        "kind": "task",
        "task_id": proto.task_id,
        "job_id": proto.job_id,
        "type": proto.type,
        "payload": _json_loads(proto.payload_json),
        "priority": proto.priority,
        "status": proto.status,
        "result": _json_loads(proto.result_json),
        "error": _json_loads(proto.error_json),
        "created_at": proto.created_at,
        "updated_at": proto.updated_at,
        "version": proto.version,
        "deleted": proto.deleted,
        "placement": {
            "primary_node": {
                "id": proto.primary_node.id,
                "address": proto.primary_node.address,
            },
            "replication_factor": proto.replication_factor,
            "replica_nodes": [
                {"id": node.id, "address": node.address}
                for node in proto.replica_nodes
            ],
        },
    }


class TaskServiceGrpcServicer(task_service_pb2_grpc.TaskServiceServicer):
    def __init__(self, task_service: TaskService):
        self.task_service = task_service

    def RegisterTask(self, request, context):
        try:
            payload = {
                "task_id": request.task_id or None,
                "job_id": request.job_id,
                "type": request.type,
                "payload": _json_loads(request.payload_json) or {},
                "priority": request.priority,
            }
            result = self.task_service.register_task(payload)
            return task_service_pb2.RegisterTaskResponse(
                task=_task_record_to_proto(result["task"]),
                task_key=result["task_key"],
                replication_state=result["storage"].get("replication_state", ""),
            )
        except json.JSONDecodeError as error:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"invalid payload_json: {error}")
            return task_service_pb2.RegisterTaskResponse()
        except TaskValidationError as error:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(str(error))
            return task_service_pb2.RegisterTaskResponse()
        except TaskConflictError as error:
            context.set_code(grpc.StatusCode.ALREADY_EXISTS)
            context.set_details(str(error))
            return task_service_pb2.RegisterTaskResponse()

    def GetTask(self, request, context):
        try:
            task = self.task_service.get_task(
                request.task_id,
                allow_replica_read=request.allow_replica_read,
            )
            if task is None:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                context.set_details(f"task not found: {request.task_id}")
                return task_service_pb2.GetTaskResponse()
            return task_service_pb2.GetTaskResponse(task=_task_record_to_proto(task))
        except TaskValidationError as error:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(str(error))
            return task_service_pb2.GetTaskResponse()

    def DeregisterTask(self, request, context):
        try:
            result = self.task_service.deregister_task(
                request.task_id,
                hard_delete=request.hard_delete,
            )
            return task_service_pb2.DeregisterTaskResponse(
                task_id=result.get("task_id", request.task_id),
                task_key=result.get("task_key", ""),
                hard_delete=result.get("hard_delete", request.hard_delete),
                replication_state=result.get("storage", {}).get("replication_state", ""),
            )
        except TaskNotFoundError as error:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(str(error))
            return task_service_pb2.DeregisterTaskResponse()
        except TaskValidationError as error:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(str(error))
            return task_service_pb2.DeregisterTaskResponse()

    def QueryTasks(self, request, context):
        tasks = self.task_service.query_local_tasks(
            job_id=request.job_id or None,
            status=request.status or None,
            include_deleted=request.include_deleted,
            limit=request.limit if request.limit > 0 else 100,
        )
        return task_service_pb2.QueryTasksResponse(
            tasks=[_task_record_to_proto(task) for task in tasks]
        )

    def LookupTaskOwner(self, request, context):
        try:
            result = self.task_service.lookup_owner(request.task_id)
            return task_service_pb2.LookupTaskOwnerResponse(
                task_id=result["task_id"],
                task_key=result["task_key"],
                primary=_node_ref_from_dict(result["primary"]),
                replicas=[
                    _node_ref_from_dict(replica)
                    for replica in result["replicas"]
                ],
            )
        except TaskValidationError as error:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(str(error))
            return task_service_pb2.LookupTaskOwnerResponse()

    def GetNodeInfo(self, request, context):
        try:
            state = self.task_service.get_node_state(address=request.address or None)
            response = task_service_pb2.GetNodeInfoResponse(
                node_id=int(state.get("node_id", 0)),
                address=state.get("address", ""),
                data_keys=list(state.get("data_keys", [])),
            )
            if state.get("successor"):
                response.successor.CopyFrom(_node_ref_from_dict(state["successor"]))
            if state.get("predecessor"):
                response.predecessor.CopyFrom(_node_ref_from_dict(state["predecessor"]))
            return response
        except Exception as error:
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(error))
            return task_service_pb2.GetNodeInfoResponse()


class InternalReplicationGrpcServicer(task_service_pb2_grpc.InternalReplicationServiceServicer):
    def __init__(self, task_service: TaskService):
        self.task_service = task_service

    def ReplicateTask(self, request, context):
        try:
            task_record = _task_record_from_proto(request.task)
            self.task_service.store_replica_local(request.task_key, task_record)
            return task_service_pb2.ReplicateTaskResponse(ok=True, task_key=request.task_key)
        except TaskValidationError as error:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(str(error))
            return task_service_pb2.ReplicateTaskResponse(ok=False, task_key=request.task_key)
        except json.JSONDecodeError as error:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"invalid nested JSON field: {error}")
            return task_service_pb2.ReplicateTaskResponse(ok=False, task_key=request.task_key)


def start_grpc_server(node, transport, grpc_port: int, max_workers: int = 10) -> grpc.Server:
    task_service = TaskService(node=node, transport=transport)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))

    task_service_pb2_grpc.add_TaskServiceServicer_to_server(
        TaskServiceGrpcServicer(task_service),
        server,
    )
    task_service_pb2_grpc.add_InternalReplicationServiceServicer_to_server(
        InternalReplicationGrpcServicer(task_service),
        server,
    )

    bind_address = f"0.0.0.0:{grpc_port}"
    server.add_insecure_port(bind_address)
    server.start()

    logger.info("gRPC TaskService started on %s", bind_address)
    return server
