from chord.node import ChordNode
from storage.replication import ReplicationManager


class FakeTransport:
    def __init__(self):
        self.states = {
            "n1": {"successor": {"id": 2, "address": "n2"}},
            "n2": {"successor": {"id": 3, "address": "n3"}},
            "n3": {"successor": {"id": 1, "address": "n1"}},
        }
        self.replica_store = {}

    def get_state(self, address: str) -> dict:
        return self.states[address]

    def put_task_replica(self, address: str, task_key: str, task_record: dict):
        self.replica_store[(address, task_key)] = task_record

    def get_task_replica(self, address: str, task_key: str):
        return self.replica_store.get((address, task_key))

    def delete_task_replica(self, address: str, task_key: str):
        self.replica_store.pop((address, task_key), None)


def test_successor_chain_and_replication_write():
    node = ChordNode(address="n1", node_id=1)
    transport = FakeTransport()
    manager = ReplicationManager(node=node, transport=transport, replication_k=3)

    primary = {"id": 1, "address": "n1"}
    chain = manager.get_successor_chain(primary)

    assert [entry["address"] for entry in chain] == ["n1", "n2", "n3"]

    record = {"kind": "task", "task_id": "t1"}
    result = manager.replicate_write("task:t1", record, primary)

    assert result["replication_state"] == "COMPLETE"
    assert ("n2", "task:t1") in transport.replica_store
    assert ("n3", "task:t1") in transport.replica_store


def test_replica_read_fallback_and_delete():
    node = ChordNode(address="n1", node_id=1)
    transport = FakeTransport()
    manager = ReplicationManager(node=node, transport=transport, replication_k=3)
    primary = {"id": 1, "address": "n1"}

    record = {"kind": "task", "task_id": "t2"}
    manager.replicate_write("task:t2", record, primary)

    found = manager.read_from_replicas("task:t2", primary)
    assert found == record

    deleted = manager.delete_replicas("task:t2", primary)
    assert deleted["replication_state"] == "COMPLETE"
    assert ("n2", "task:t2") not in transport.replica_store
    assert ("n3", "task:t2") not in transport.replica_store
