import logging

logger = logging.getLogger(__name__)


class ReplicationManager:
    def __init__(self, node, transport, replication_k: int = 3):
        self.node = node
        self.transport = transport
        self.replication_k = max(1, int(replication_k))

    def get_successor_chain(self, primary_node: dict, k: int | None = None) -> list[dict]:
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

    def replicate_write(self, task_key: str, task_record: dict, primary_node: dict) -> dict:
        chain = self.get_successor_chain(primary_node)
        replicas_written: list[dict] = []
        failed: list[dict] = []

        for replica in chain[1:]:
            try:
                self.transport.put_task_replica(replica["address"], task_key, task_record)
                replicas_written.append(replica)
            except Exception as error:
                failed.append({"node": replica, "error": str(error)})

        return {
            "primary": primary_node,
            "replica_targets": chain[1:],
            "replicas_written": replicas_written,
            "failed": failed,
            "replication_state": "COMPLETE" if not failed else "DEGRADED",
        }

    def delete_replicas(self, task_key: str, primary_node: dict) -> dict:
        chain = self.get_successor_chain(primary_node)
        deleted: list[dict] = []
        failed: list[dict] = []

        for replica in chain[1:]:
            try:
                self.transport.delete_task_replica(replica["address"], task_key)
                deleted.append(replica)
            except Exception as error:
                failed.append({"node": replica, "error": str(error)})

        return {
            "primary": primary_node,
            "replica_targets": chain[1:],
            "deleted": deleted,
            "failed": failed,
            "replication_state": "COMPLETE" if not failed else "DEGRADED",
        }

    def read_from_replicas(self, task_key: str, primary_node: dict) -> dict | None:
        chain = self.get_successor_chain(primary_node)
        for replica in chain[1:]:
            try:
                value = self.transport.get_task_replica(replica["address"], task_key)
            except Exception:
                continue
            if value is not None:
                return value
        return None
