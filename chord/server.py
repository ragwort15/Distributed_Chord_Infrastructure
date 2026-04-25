"""
Flask HTTP server exposing all Chord RPC endpoints and the public data API.
"""

import threading
import logging
from flask import Flask, request, jsonify
from chord.node import ChordNode, sha1_id
from chord.transport import HttpTransport
from storage.task_service import (
    TaskConflictError,
    TaskNotFoundError,
    TaskService,
    TaskValidationError,
)

logger = logging.getLogger(__name__)


def create_app(node: ChordNode) -> Flask:
    app = Flask(__name__)
    app.config["node"] = node
    task_service = TaskService(node=node, transport=node._transport)

    # Chord internal RPC endpoints

    @app.get("/chord/find_successor")
    def find_successor():
        key_id = int(request.args.get("id"))
        result = node.find_successor(key_id)
        return jsonify(result)

    @app.get("/chord/predecessor")
    def get_predecessor():
        pred = node.predecessor
        if pred is None:
            return jsonify({"id": None, "address": None})
        return jsonify(pred)

    @app.post("/chord/notify")
    def notify():
        candidate = request.get_json()
        node.notify(candidate)
        return jsonify({"ok": True})

    @app.post("/chord/update_predecessor")
    def update_predecessor():
        new_pred = request.get_json()
        node.predecessor = new_pred
        return jsonify({"ok": True})

    @app.post("/chord/update_successor")
    def update_successor():
        new_succ = request.get_json()
        node.successor = new_succ
        return jsonify({"ok": True})

    @app.post("/chord/bulk_put")
    def bulk_put():
        items = request.get_json()
        node.bulk_put(items)
        return jsonify({"ok": True, "count": len(items)})

    @app.get("/chord/ping")
    def ping():
        return jsonify({"id": node.node_id, "address": node.address})

    @app.get("/chord/state")
    def state():
        return jsonify(node.state())

    # Local data store endpoints (direct, no routing)

    @app.post("/data/<key>")
    def data_put(key):
        value = request.get_json()
        node.put(key, value)
        return jsonify({"ok": True, "key": key, "stored_at": node.node_id})

    @app.get("/data/<key>")
    def data_get(key):
        value = node.get(key)
        if value is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(value)

    @app.delete("/data/<key>")
    def data_delete(key):
        deleted = node.delete(key)
        return jsonify({"ok": deleted})

    # Routed data API — automatically sends to responsible node

    @app.post("/put/<key>")
    def routed_put(key):
        """Hash the key and route the PUT to the responsible node."""
        value = request.get_json()
        key_id = sha1_id(key)
        responsible = node.find_successor(key_id)

        if responsible["id"] == node.node_id:
            node.put(key, value)
            return jsonify({"ok": True, "key": key, "stored_at": node.node_id})

        # Forward to responsible node
        transport = node._transport
        try:
            transport.put(responsible["address"], key, value)
            return jsonify({"ok": True, "key": key, "stored_at": responsible["id"]})
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    @app.get("/get/<key>")
    def routed_get(key):
        """Hash the key and route the GET to the responsible node."""
        key_id = sha1_id(key)
        responsible = node.find_successor(key_id)

        if responsible["id"] == node.node_id:
            value = node.get(key)
            if value is None:
                return jsonify({"error": "not found"}), 404
            return jsonify(value)

        transport = node._transport
        try:
            value = transport.get(responsible["address"], key)
            if value is None:
                return jsonify({"error": "not found"}), 404
            return jsonify(value)
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    # Task service API

    @app.post("/tasks")
    def register_task():
        payload = request.get_json() or {}
        try:
            result = task_service.register_task(payload)
            return jsonify({"ok": True, "data": result}), 201
        except TaskValidationError as e:
            return jsonify({"ok": False, "error": {"code": "VALIDATION_ERROR", "message": str(e)}}), 422
        except TaskConflictError as e:
            return jsonify({"ok": False, "error": {"code": "TASK_CONFLICT", "message": str(e)}}), 409

    @app.get("/tasks/<task_id>")
    def get_task(task_id):
        allow_replica_read = request.args.get("allow_replica_read", "true").lower() != "false"
        try:
            task = task_service.get_task(task_id, allow_replica_read=allow_replica_read)
            if task is None:
                return jsonify({"ok": False, "error": {"code": "TASK_NOT_FOUND", "message": f"task not found: {task_id}"}}), 404
            return jsonify({"ok": True, "data": {"task": task}})
        except TaskValidationError as e:
            return jsonify({"ok": False, "error": {"code": "VALIDATION_ERROR", "message": str(e)}}), 422

    @app.delete("/tasks/<task_id>")
    def deregister_task(task_id):
        hard_delete = request.args.get("hard", "false").lower() == "true"
        try:
            result = task_service.deregister_task(task_id, hard_delete=hard_delete)
            return jsonify({"ok": True, "data": result})
        except TaskNotFoundError as e:
            return jsonify({"ok": False, "error": {"code": "TASK_NOT_FOUND", "message": str(e)}}), 404
        except TaskValidationError as e:
            return jsonify({"ok": False, "error": {"code": "VALIDATION_ERROR", "message": str(e)}}), 422

    @app.get("/tasks")
    def query_tasks():
        job_id = request.args.get("job_id")
        status = request.args.get("status")
        include_deleted = request.args.get("include_deleted", "false").lower() == "true"
        limit = int(request.args.get("limit", "100"))

        tasks = task_service.query_local_tasks(
            job_id=job_id,
            status=status,
            include_deleted=include_deleted,
            limit=limit,
        )
        return jsonify({"ok": True, "data": {"tasks": tasks, "count": len(tasks)}})

    @app.get("/ring/lookup/<task_id>")
    def lookup_task(task_id):
        try:
            result = task_service.lookup_owner(task_id)
            return jsonify({"ok": True, "data": result})
        except TaskValidationError as e:
            return jsonify({"ok": False, "error": {"code": "VALIDATION_ERROR", "message": str(e)}}), 422

    @app.get("/nodes/self")
    def node_self():
        return jsonify({"ok": True, "data": task_service.get_node_state()})

    @app.get("/nodes/query")
    def node_query():
        address = request.args.get("address")
        if not address:
            return jsonify({"ok": False, "error": {"code": "VALIDATION_ERROR", "message": "missing query param: address"}}), 422
        try:
            state = task_service.get_node_state(address=address)
            return jsonify({"ok": True, "data": state})
        except Exception as e:
            return jsonify({"ok": False, "error": {"code": "UPSTREAM_ERROR", "message": str(e)}}), 502

    # Internal replication API

    @app.post("/internal/tasks/replica/<path:task_key>")
    def put_task_replica(task_key):
        payload = request.get_json() or {}
        try:
            result = task_service.store_replica_local(task_key, payload)
            return jsonify({"ok": True, "data": result})
        except TaskValidationError as e:
            return jsonify({"ok": False, "error": {"code": "VALIDATION_ERROR", "message": str(e)}}), 422

    @app.get("/internal/tasks/replica/<path:task_key>")
    def get_task_replica(task_key):
        task = task_service.get_replica_local(task_key)
        if task is None:
            return jsonify({"ok": False, "error": {"code": "TASK_NOT_FOUND", "message": f"task not found: {task_key}"}}), 404
        return jsonify({"ok": True, "task": task})

    @app.delete("/internal/tasks/replica/<path:task_key>")
    def delete_task_replica(task_key):
        deleted = task_service.delete_replica_local(task_key)
        return jsonify({"ok": True, "deleted": deleted, "task_key": task_key})

    return app


# Background maintenance thread

class MaintenanceThread(threading.Thread):
    """
    Runs stabilize(), fix_fingers(), and check_predecessor()
    in a loop at a configurable interval.
    """

    def __init__(self, node: ChordNode, interval: float = 2.0):
        super().__init__(daemon=True, name=f"chord-maintenance-{node.node_id}")
        self.node = node
        self.interval = interval
        self._stop_event = threading.Event()

    def run(self):
        logger.info(f"[Maintenance] Started for node {self.node.node_id}")
        while not self._stop_event.is_set():
            try:
                self.node.stabilize()
                self.node.fix_fingers()
                self.node.check_predecessor()
            except Exception as e:
                logger.warning(f"[Maintenance] Error: {e}")
            self._stop_event.wait(self.interval)

    def stop(self):
        self._stop_event.set()


# Entrypoint helper

def start_node(host: str, port: int, known_address: str = None,
               node_id: int = None, maintenance_interval: float = 2.0,
               grpc_port: int | None = None):
    """
    Convenience function: create node, wire transport, join ring, start server.
    """
    address = f"{host}:{port}"
    transport = HttpTransport()

    node = ChordNode(address=address, node_id=node_id)
    node.set_transport(transport)

    app = create_app(node)

    # Join must happen AFTER transport is set but BEFORE server starts
    node.join(known_address)

    maint = MaintenanceThread(node, interval=maintenance_interval)
    maint.start()

    grpc_server = None
    if grpc_port is not None:
        from api.grpc_server import start_grpc_server
        grpc_server = start_grpc_server(node=node, transport=transport, grpc_port=grpc_port)

    import os
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    logging.basicConfig(level=getattr(logging, log_level))

    logger.info(f"Starting Chord node {node.node_id} on {address}")
    try:
        app.run(host=host, port=port, threaded=True)
    finally:
        maint.stop()
        if grpc_server is not None:
            grpc_server.stop(grace=1)
