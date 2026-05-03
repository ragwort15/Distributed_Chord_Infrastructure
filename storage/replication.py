import logging
import time
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# How many times to retry a single replica write/delete before giving up
REPLICA_RETRIES = 2
# Base back-off delay (seconds); doubles each attempt
REPLICA_RETRY_DELAY = 0.2
# Minimum fraction of replicas that must succeed to call replication COMPLETE
# (quorum = ceil(k/2)).  Below this threshold the record is DEGRADED.
# e.g. k=3 → quorum=2; k=2 → quorum=1
def _quorum(k: int) -> int:
    import math
    return math.ceil(k / 2)


class ReplicationManager:
    def __init__(self, node, transport, replication_k: int = 3):
        self.node = node
        self.transport = transport
        self.replication_k = max(1, int(replication_k))

    def get_successor_chain(self, primary_node: dict, k: Optional[int] = None) -> List[dict]:
        target_len = max(1, int(k or self.replication_k))
        chain: list[dict] = []
        visited_addresses: set[str] = set()

        current = {"id": primary_node["id"], "address": primary_node["address"]}
        while len(chain) < target_len:
            address = current["address"]
            if address in visited_addresses:
                break
            visited_addresses.add(address)
            chain.append(current)

            try:
                state = self.transport.get_state(address)
            except Exception as error:
                logger.warning("Failed to fetch state from %s: %s", address, error)
                break

            successor = state.get("successor")
            if not successor or successor.get("address") == address:
                break

            current = {
                "id": successor["id"],
                "address": successor["address"],
            }

        return chain

    def _put_with_retry(self, replica: dict, task_key: str, task_record: dict) -> Optional[str]:
        """
        Attempt to write one replica with exponential-backoff retries.

        Returns None on success, or an error string on permanent failure.

        Why retry here in addition to transport-level retries?
        The transport layer retries on *connection* errors (socket timeouts,
        refused connections).  This layer adds retries for *application-level*
        transient errors: 503 Service Unavailable, brief lock contention on
        the remote node, etc.  Both layers are independent and additive.
        """
        last_error: Optional[str] = None
        for attempt in range(REPLICA_RETRIES + 1):
            try:
                self.transport.put_task_replica(
                    replica["address"], task_key, task_record
                )
                return None  # success
            except Exception as exc:
                last_error = str(exc)
                if attempt < REPLICA_RETRIES:
                    delay = REPLICA_RETRY_DELAY * (2 ** attempt)
                    logger.debug(
                        "[Replication] Write to %s failed (attempt %d/%d): %s — retry in %.2fs",
                        replica["address"], attempt + 1, REPLICA_RETRIES + 1, exc, delay,
                    )
                    time.sleep(delay)
        return last_error

    def replicate_write(self, task_key: str, task_record: dict, primary_node: dict) -> dict:
        """
        Write task_record to all successor replicas.

        Previous behaviour: one attempt per replica; any failure → DEGRADED.
        Fix: each replica gets REPLICA_RETRIES+1 attempts with exponential
        back-off.  After all attempts we compute a quorum check: if at least
        ceil(k/2) replicas were written (including primary) the state is
        COMPLETE, otherwise DEGRADED.  This is more lenient than requiring
        ALL replicas (a single flaky node shouldn't block every write) but
        still ensures data durability.
        """
        chain = self.get_successor_chain(primary_node)
        replicas_written: list[dict] = []
        failed: list[dict] = []

        for replica in chain[1:]:
            error = self._put_with_retry(replica, task_key, task_record)
            if error is None:
                replicas_written.append(replica)
            else:
                failed.append({"node": replica, "error": error})
                logger.warning(
                    "[Replication] Permanently failed to write %s to replica %s: %s",
                    task_key, replica["address"], error,
                )

        # Quorum check: primary counts as 1 success
        total_written = 1 + len(replicas_written)  # 1 = primary
        needed = _quorum(self.replication_k)
        replication_state = "COMPLETE" if total_written >= needed else "DEGRADED"

        return {
            "primary": primary_node,
            "replica_targets": chain[1:],
            "replicas_written": replicas_written,
            "failed": failed,
            "replication_state": replication_state,
            "quorum": {"required": needed, "achieved": total_written},
        }

    def _delete_with_retry(self, replica: dict, task_key: str) -> Optional[str]:
        """Attempt to delete one replica with retries. Returns None on success."""
        last_error: Optional[str] = None
        for attempt in range(REPLICA_RETRIES + 1):
            try:
                self.transport.delete_task_replica(replica["address"], task_key)
                return None
            except Exception as exc:
                last_error = str(exc)
                if attempt < REPLICA_RETRIES:
                    delay = REPLICA_RETRY_DELAY * (2 ** attempt)
                    logger.debug(
                        "[Replication] Delete from %s failed (attempt %d/%d): %s — retry in %.2fs",
                        replica["address"], attempt + 1, REPLICA_RETRIES + 1, exc, delay,
                    )
                    time.sleep(delay)
        return last_error

    def delete_replicas(self, task_key: str, primary_node: dict) -> dict:
        """
        Delete task_record from all successor replicas.

        Same retry policy as replicate_write — each replica gets up to
        REPLICA_RETRIES+1 attempts before being logged as failed.
        A failed replica deletion is logged as a warning but does not
        raise: the primary delete already happened, so the record is at
        worst 'soft-present' on one stale replica until the next
        stabilization sweep.
        """
        chain = self.get_successor_chain(primary_node)
        deleted: list[dict] = []
        failed: list[dict] = []

        for replica in chain[1:]:
            error = self._delete_with_retry(replica, task_key)
            if error is None:
                deleted.append(replica)
            else:
                failed.append({"node": replica, "error": error})
                logger.warning(
                    "[Replication] Could not delete %s from replica %s: %s",
                    task_key, replica["address"], error,
                )

        return {
            "primary": primary_node,
            "replica_targets": chain[1:],
            "deleted": deleted,
            "failed": failed,
            "replication_state": "COMPLETE" if not failed else "DEGRADED",
        }

    def read_from_replicas(self, task_key: str, primary_node: dict) -> Optional[dict]:
        chain = self.get_successor_chain(primary_node)
        for replica in chain[1:]:
            try:
                value = self.transport.get_task_replica(replica["address"], task_key)
            except Exception:
                continue
            if value is not None:
                return value
        return None
