import logging
import time
from typing import List, Dict, Optional
from chord.node import sha1_id
from storage.replication import ReplicationManager
from storage.schema import (
    TaskValidationError,
    build_task_key,
    create_task_record,
    is_task_record,
    mark_task_deleted,
    set_replica_nodes,
    update_task_record,
    validate_task_record,
)

logger = logging.getLogger(__name__)

# Retries for primary-node writes (transport already retries internally,
# but if the *routing* itself pointed to the wrong node due to a ring
# change mid-flight, we re-resolve the owner and retry the whole operation)
PRIMARY_WRITE_RETRIES = 2
PRIMARY_WRITE_DELAY = 0.2


class TaskNotFoundError(Exception):
    pass


class TaskConflictError(Exception):
    pass


class TaskService:
    def __init__(self, node, transport, replication_k: int = 3):
        self.node = node
        self.transport = transport
        self.replication = ReplicationManager(node=node, transport=transport, replication_k=replication_k)

    def _owner_for_key(self, task_key: str) -> dict:
        key_id = sha1_id(task_key)
        return self.node.find_successor(key_id)

    def _put_on_node(self, node_ref: dict, key: str, value: dict):
        """
        Write a key-value pair to the given node.

        If the target is a remote node and the put fails (e.g. the node
        crashed after we resolved it as primary), re-resolve the owner and
        retry.  This handles the window where the ring is mid-stabilisation
        and the successor table has not yet updated.
        """
        if node_ref["id"] == self.node.node_id:
            self.node.put(key, value)
            return

        last_exc = None
        for attempt in range(PRIMARY_WRITE_RETRIES + 1):
            try:
                self.transport.put(node_ref["address"], key, value)
                return
            except Exception as exc:
                last_exc = exc
                if attempt < PRIMARY_WRITE_RETRIES:
                    # Re-resolve: ring may have stabilised to a new primary
                    key_id = sha1_id(key)
                    new_primary = self.node.find_successor(key_id)
                    logger.warning(
                        "[TaskService] Primary write to %s failed (attempt %d/%d): %s"
                        " — re-resolving primary (was %s, now %s)",
                        node_ref["address"], attempt + 1, PRIMARY_WRITE_RETRIES + 1,
                        exc, node_ref["id"], new_primary["id"],
                    )
                    node_ref = new_primary
                    time.sleep(PRIMARY_WRITE_DELAY * (2 ** attempt))
        raise last_exc

    def _get_on_node(self, node_ref: dict, key: str) -> Optional[dict]:
        if node_ref["id"] == self.node.node_id:
            return self.node.get(key)
        return self.transport.get(node_ref["address"], key)

    def _delete_on_node(self, node_ref: dict, key: str) -> bool:
        if node_ref["id"] == self.node.node_id:
            return self.node.delete(key)
        return self.transport.delete(node_ref["address"], key)

    def register_task(self, task_input: dict) -> dict:
        task_id = task_input.get("task_id")
        if task_id:
            existing = self.get_task(task_id, allow_replica_read=True)
            if existing and existing.get("status") != "DELETED":
                raise TaskConflictError(f"task already exists: {task_id}")

        candidate_id = task_id if task_id else task_input.get("task_id")
        if candidate_id:
            task_key = build_task_key(candidate_id)
        else:
            from storage.schema import generate_task_id
            task_key = build_task_key(generate_task_id())

        primary = self._owner_for_key(task_key)
        task_record = create_task_record(
            task_input=task_input,
            owner_node=primary,
            replication_factor=self.replication.replication_k,
            task_id=task_key.split("task:", 1)[1],
        )

        chain = self.replication.get_successor_chain(primary)
        task_record = set_replica_nodes(task_record, chain[1:])

        self._put_on_node(primary, task_key, task_record)
        replication_result = self.replication.replicate_write(task_key, task_record, primary)

        return {
            "task": task_record,
            "task_key": task_key,
            "storage": replication_result,
        }

    def get_task(self, task_id: str, allow_replica_read: bool = True) -> Optional[dict]:
        """
        Fetch a task record.

        Fallback chain:
          1. Try the primary node (Chord-routed owner of the key).
          2. If primary is unreachable OR returns None, fall back to
             reading from replica nodes (successor chain).

        Previously the fallback only triggered when the primary returned
        None (key not found).  It did NOT trigger when the primary threw
        a network exception — so a crashed primary made the task
        invisible even if replicas held a good copy.

        Fix: wrap the primary fetch in a try/except; on any exception
        treat it the same as a None return and consult replicas.
        """
        task_key = build_task_key(task_id)
        primary = self._owner_for_key(task_key)

        value = None
        try:
            value = self._get_on_node(primary, task_key)
        except Exception as exc:
            logger.warning(
                "[TaskService] Primary read failed for %s on %s: %s — "
                "falling back to replicas",
                task_key, primary.get("address"), exc,
            )

        if value is None and allow_replica_read:
            value = self.replication.read_from_replicas(task_key, primary)

        if value is None:
            return None

        if not is_task_record(value):
            return None

        validate_task_record(value)
        return value

    def deregister_task(self, task_id: str, hard_delete: bool = False) -> dict:
        task_key = build_task_key(task_id)
        primary = self._owner_for_key(task_key)

        existing = self.get_task(task_id, allow_replica_read=True)
        if existing is None:
            raise TaskNotFoundError(f"task not found: {task_id}")

        if hard_delete:
            self._delete_on_node(primary, task_key)
            replication_result = self.replication.delete_replicas(task_key, primary)
            return {
                "task_id": task_id,
                "task_key": task_key,
                "hard_delete": True,
                "storage": replication_result,
            }

        deleted_record = mark_task_deleted(existing)
        self._put_on_node(primary, task_key, deleted_record)
        replication_result = self.replication.replicate_write(task_key, deleted_record, primary)

        return {
            "task": deleted_record,
            "task_key": task_key,
            "hard_delete": False,
            "storage": replication_result,
        }

    def update_task(self, task_id: str, patch: dict) -> dict:
        """Apply a partial update (status, result, error, payload, priority) to a task."""
        task_key = build_task_key(task_id)
        primary = self._owner_for_key(task_key)

        existing = self.get_task(task_id, allow_replica_read=True)
        if existing is None:
            raise TaskNotFoundError(f"task not found: {task_id}")

        if existing.get("status") == "DELETED":
            raise TaskValidationError("cannot update a deleted task")

        updated_record = update_task_record(existing, patch)
        self._put_on_node(primary, task_key, updated_record)
        replication_result = self.replication.replicate_write(task_key, updated_record, primary)

        return {
            "task": updated_record,
            "task_key": task_key,
            "storage": replication_result,
        }

    def query_local_tasks(
        self,
        job_id: Optional[str] = None,
        status: Optional[str] = None,
        include_deleted: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        results: list[dict] = []
        for _, value in self.node.data_store.items():
            if not is_task_record(value):
                continue

            if not include_deleted and value.get("status") == "DELETED":
                continue

            if job_id and value.get("job_id") != job_id:
                continue

            if status and value.get("status") != status:
                continue

            results.append(value)
            if len(results) >= limit:
                break

        return results

    def lookup_owner(self, task_id: str) -> dict:
        task_key = build_task_key(task_id)
        primary = self._owner_for_key(task_key)
        chain = self.replication.get_successor_chain(primary)
        return {
            "task_id": task_id,
            "task_key": task_key,
            "primary": primary,
            "replicas": chain[1:],
        }

    def get_node_state(self, address: Optional[str] = None) -> dict:
        if not address or address == self.node.address:
            return self.node.state()
        return self.transport.get_state(address)

    def store_replica_local(self, task_key: str, task_record: dict) -> dict:
        validate_task_record(task_record)
        self.node.put(task_key, task_record)
        return {"ok": True, "task_key": task_key, "stored_at": self.node.node_id}

    def get_replica_local(self, task_key: str) -> Optional[dict]:
        value = self.node.get(task_key)
        if value is None:
            return None
        if not is_task_record(value):
            return None
        return value

    def delete_replica_local(self, task_key: str) -> bool:
        return self.node.delete(task_key)
