"""
Phase 3: HT1 service for ``result:<task_id>`` records.

Mirrors storage/task_service.py for the new keyspace. Replication is
k=3 quorum, matching Phase 1: primary write must succeed (raises on
failure to trigger the caller's retry); replica writes are best-effort
and report COMPLETE/DEGRADED via quorum (ceil(k/2)).
"""

import logging
import math
import time
from typing import Optional

from chord.node import sha1_id
from storage.replication import ReplicationManager
from storage.result_record import (
    build_result_key,
    is_result_record,
    validate_result_record,
)

logger = logging.getLogger(__name__)

PRIMARY_WRITE_RETRIES = 2
PRIMARY_WRITE_DELAY = 0.2


class ResultService:
    def __init__(self, node, transport, replication_k: int = 3):
        self.node = node
        self.transport = transport
        self.replication_k = max(1, int(replication_k))
        # Throwaway ReplicationManager just for get_successor_chain()
        self._chain = ReplicationManager(
            node=node, transport=transport, replication_k=replication_k
        )

    # public API

    def put(self, task_id: str, record: dict) -> dict:
        validate_result_record(record)
        key = build_result_key(task_id)
        primary = self._owner(key)
        self._put_on_node(primary, key, record)
        rep = self._replicate(task_id, record, primary)
        return {"key": key, "primary": primary, "replication": rep}

    def get(self, task_id: str) -> Optional[dict]:
        key = build_result_key(task_id)
        primary = self._owner(key)
        value = None
        try:
            value = self._get_on_node(primary, key)
        except Exception as exc:
            logger.warning(
                "[ResultService] primary read failed for %s on %s: %s — falling back to replicas",
                key, primary.get("address"), exc,
            )
        if value is None:
            value = self._read_from_replicas(task_id, primary)
        if value is None or not is_result_record(value):
            return None
        return value

    def delete(self, task_id: str) -> bool:
        key = build_result_key(task_id)
        primary = self._owner(key)
        ok = False
        try:
            ok = self._delete_on_node(primary, key)
        except Exception as exc:
            logger.warning(
                "[ResultService] primary delete failed for %s: %s", key, exc,
            )
        chain = self._chain.get_successor_chain(primary)
        for replica in chain[1:]:
            try:
                self.transport.delete_result_replica(replica["address"], task_id)
            except Exception as exc:
                logger.warning(
                    "[ResultService] replica delete failed for %s on %s: %s",
                    key, replica.get("address"), exc,
                )
        return ok

    # internals

    def _owner(self, key: str) -> dict:
        return self.node.find_successor(sha1_id(key))

    def _put_on_node(self, node_ref, key, value):
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
                    new_primary = self._owner(key)
                    logger.warning(
                        "[ResultService] primary put to %s failed (%d/%d): %s — re-resolving",
                        node_ref["address"], attempt + 1,
                        PRIMARY_WRITE_RETRIES + 1, exc,
                    )
                    node_ref = new_primary
                    time.sleep(PRIMARY_WRITE_DELAY * (2 ** attempt))
        raise last_exc

    def _get_on_node(self, node_ref, key):
        if node_ref["id"] == self.node.node_id:
            return self.node.get(key)
        return self.transport.get(node_ref["address"], key)

    def _delete_on_node(self, node_ref, key) -> bool:
        if node_ref["id"] == self.node.node_id:
            return self.node.delete(key)
        return self.transport.delete(node_ref["address"], key)

    def _replicate(self, task_id: str, record: dict, primary: dict) -> dict:
        chain = self._chain.get_successor_chain(primary)
        written: list = []
        failed: list = []
        for replica in chain[1:]:
            try:
                self.transport.put_result_replica(
                    replica["address"], task_id, record,
                )
                written.append(replica)
            except Exception as exc:
                failed.append({"node": replica, "error": str(exc)})
                logger.warning(
                    "[ResultService] replica write failed result:%s -> %s: %s",
                    task_id, replica.get("address"), exc,
                )
        needed = math.ceil(self.replication_k / 2)
        achieved = 1 + len(written)
        return {
            "replicas_written": written,
            "failed": failed,
            "replication_state": "COMPLETE" if achieved >= needed else "DEGRADED",
            "quorum": {"required": needed, "achieved": achieved},
        }

    def _read_from_replicas(self, task_id: str, primary: dict) -> Optional[dict]:
        chain = self._chain.get_successor_chain(primary)
        for replica in chain[1:]:
            try:
                v = self.transport.get_result_replica(replica["address"], task_id)
                if v is not None:
                    return v
            except Exception:
                continue
        return None
