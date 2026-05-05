"""
Phase 3: HT2 — worker assignment records.

Stores ``worker_id -> List[task_id]`` under keys ``worker:<worker_id>``,
replicated k=3 across the same Chord ring as HT1.

The append() operation is read-modify-write, so it must be atomic per
worker_id. The lock lives on the *primary node's process* — frontends
just call a single RPC (POST /internal/workers/append/<id>) and the
primary holds _per_key_locks[key] for the entire
read+modify+write+replicate cycle. Two concurrent appends from two
different frontend nodes therefore serialise correctly.

TODO(future-phase): _per_key_locks grows unboundedly with the number
of distinct worker_ids ever seen. Fine at Phase 3 scale; later phases
should add a janitor or bounded LRU.
"""

import copy
import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import List, Optional

from chord.node import sha1_id
from storage.replication import ReplicationManager

logger = logging.getLogger(__name__)

WORKER_SCHEMA_VERSION = 1
WORKER_KIND = "worker_assignment"

PRIMARY_WRITE_RETRIES = 2
PRIMARY_WRITE_DELAY = 0.2

# --- per-key lock dict (primary-side) ----------------------------------------
_locks_meta = threading.Lock()
_per_key_locks: dict = {}


def _key_lock(key: str) -> threading.Lock:
    with _locks_meta:
        return _per_key_locks.setdefault(key, threading.Lock())


# --- record helpers ----------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_worker_key(worker_id: str) -> str:
    if not worker_id or not isinstance(worker_id, str):
        raise ValueError("worker_id required")
    return f"worker:{worker_id.strip()}"


def is_worker_record(value) -> bool:
    return isinstance(value, dict) and value.get("kind") == WORKER_KIND


def _new_record(worker_id: str, tasks: List[str]) -> dict:
    now = _utc_now_iso()
    return {
        "schema_version": WORKER_SCHEMA_VERSION,
        "kind": WORKER_KIND,
        "worker_id": worker_id,
        "tasks": list(tasks),
        "created_at": now,
        "updated_at": now,
        "version": 1,
    }


def _bump(record: dict, tasks: List[str]) -> dict:
    out = copy.deepcopy(record)
    out["tasks"] = list(tasks)
    out["updated_at"] = _utc_now_iso()
    out["version"] = int(out.get("version", 1)) + 1
    return out


# --- service -----------------------------------------------------------------

class WorkerAssignmentService:
    def __init__(self, node, transport, replication_k: int = 3):
        self.node = node
        self.transport = transport
        self.replication_k = max(1, int(replication_k))
        self._chain = ReplicationManager(
            node=node, transport=transport, replication_k=replication_k
        )

    # ---- public: routed always through the primary's locked RPC ----

    def append(self, worker_id: str, task_id: str) -> dict:
        """
        Append task_id to worker_id's list. Always routes through the
        *primary node's* RPC, even if this process IS the primary — the
        lock is held in exactly one place (append_local) regardless of
        where the call originates.
        """
        key = build_worker_key(worker_id)
        primary = self._owner(key)

        if primary["id"] == self.node.node_id:
            return self.append_local(worker_id, task_id, primary=primary)

        last_exc = None
        for attempt in range(PRIMARY_WRITE_RETRIES + 1):
            try:
                return self.transport.append_worker_assignment(
                    primary["address"], worker_id, task_id,
                )
            except Exception as exc:
                last_exc = exc
                if attempt < PRIMARY_WRITE_RETRIES:
                    primary = self._owner(key)
                    logger.warning(
                        "[WorkerAssignment] append RPC to %s failed (%d/%d): %s — re-resolving",
                        primary["address"], attempt + 1,
                        PRIMARY_WRITE_RETRIES + 1, exc,
                    )
                    time.sleep(PRIMARY_WRITE_DELAY * (2 ** attempt))
        raise last_exc

    def append_local(self, worker_id: str, task_id: str,
                     primary: Optional[dict] = None) -> dict:
        """
        Server-side handler. Holds _per_key_locks[key] for the full
        read-modify-write+replicate cycle. Idempotent on (worker_id, task_id).
        """
        key = build_worker_key(worker_id)
        if primary is None:
            primary = self._owner(key)

        with _key_lock(key):
            current = self.node.get(key)
            tasks = list(current["tasks"]) if is_worker_record(current) else []

            if task_id in tasks:
                logger.info(
                    "[WorkerAssignment] %s already has %s — idempotent no-op",
                    worker_id, task_id,
                )
                return {
                    "key": key,
                    "tasks": tasks,
                    "appended": False,
                    "replication": {"replication_state": "SKIPPED"},
                }

            tasks.append(task_id)
            new_record = (_bump(current, tasks)
                          if is_worker_record(current)
                          else _new_record(worker_id, tasks))

            self.node.put(key, new_record)
            rep = self._replicate(worker_id, new_record, primary)

        return {
            "key": key,
            "tasks": new_record["tasks"],
            "appended": True,
            "replication": rep,
        }

    def get(self, worker_id: str) -> List[str]:
        key = build_worker_key(worker_id)
        primary = self._owner(key)
        value = None
        try:
            if primary["id"] == self.node.node_id:
                value = self.node.get(key)
            else:
                value = self.transport.get(primary["address"], key)
        except Exception as exc:
            logger.warning(
                "[WorkerAssignment] primary read failed for %s: %s — falling back",
                key, exc,
            )
        if not is_worker_record(value):
            value = self._read_from_replicas(worker_id, primary)
        if not is_worker_record(value):
            return []
        return list(value.get("tasks") or [])

    def delete(self, worker_id: str) -> bool:
        key = build_worker_key(worker_id)
        primary = self._owner(key)
        with _key_lock(key):
            ok = False
            try:
                if primary["id"] == self.node.node_id:
                    ok = self.node.delete(key)
                else:
                    ok = self.transport.delete(primary["address"], key)
            except Exception as exc:
                logger.warning("[WorkerAssignment] primary delete failed: %s", exc)
            chain = self._chain.get_successor_chain(primary)
            for replica in chain[1:]:
                try:
                    self.transport.delete_worker_replica(
                        replica["address"], worker_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "[WorkerAssignment] replica delete failed on %s: %s",
                        replica.get("address"), exc,
                    )
        return ok

    # ---- internals ----

    def _owner(self, key: str) -> dict:
        return self.node.find_successor(sha1_id(key))

    def _replicate(self, worker_id: str, record: dict, primary: dict) -> dict:
        chain = self._chain.get_successor_chain(primary)
        written: list = []
        failed: list = []
        for replica in chain[1:]:
            try:
                self.transport.put_worker_replica(
                    replica["address"], worker_id, record,
                )
                written.append(replica)
            except Exception as exc:
                failed.append({"node": replica, "error": str(exc)})
                logger.warning(
                    "[WorkerAssignment] replica write failed worker:%s -> %s: %s",
                    worker_id, replica.get("address"), exc,
                )
        needed = math.ceil(self.replication_k / 2)
        achieved = 1 + len(written)
        return {
            "replicas_written": written,
            "failed": failed,
            "replication_state": "COMPLETE" if achieved >= needed else "DEGRADED",
            "quorum": {"required": needed, "achieved": achieved},
        }

    def _read_from_replicas(self, worker_id: str, primary: dict) -> Optional[dict]:
        chain = self._chain.get_successor_chain(primary)
        for replica in chain[1:]:
            try:
                v = self.transport.get_worker_replica(replica["address"], worker_id)
                if v is not None:
                    return v
            except Exception:
                continue
        return None
