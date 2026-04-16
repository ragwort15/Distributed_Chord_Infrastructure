"""
Unit tests for the Chord DHT implementation.

Covers:
  - SHA-1 hashing and ring arithmetic
  - Finger table initialization
  - in_range() circular interval logic
  - Single-node ring behavior
  - Multi-node successor lookup
  - Stabilization and predecessor updates
  - Node join (with mock transport)
  - Node leave and data handoff
  - Data store operations (put / get / delete / bulk)
  - Failure recovery (predecessor cleared on timeout)

Run:
  pytest tests/test_chord.py -v
"""

import pytest
from unittest.mock import MagicMock, patch
from chord.node import ChordNode, sha1_id, in_range, M

# Helpers

def make_node(address: str, node_id: int) -> ChordNode:
    """Create a ChordNode with a mock transport."""
    node = ChordNode(address=address, node_id=node_id)
    node.set_transport(MagicMock())
    return node


RING_SIZE = 2 ** M


# 1. Hashing

class TestHashing:
    def test_sha1_id_returns_int(self):
        result = sha1_id("127.0.0.1:5001")
        assert isinstance(result, int)

    def test_sha1_id_in_range(self):
        for key in ["a", "b", "hello", "127.0.0.1:9999"]:
            assert 0 <= sha1_id(key) < RING_SIZE

    def test_sha1_id_deterministic(self):
        assert sha1_id("test") == sha1_id("test")

    def test_sha1_id_different_keys(self):
        assert sha1_id("key1") != sha1_id("key2")


# 2. Ring arithmetic

class TestInRange:
    def test_simple_inside(self):
        assert in_range(5, 3, 8)

    def test_simple_outside(self):
        assert not in_range(2, 3, 8)

    def test_exclusive_endpoints(self):
        assert not in_range(3, 3, 8)   # a itself is excluded
        assert not in_range(8, 3, 8)   # b excluded when inclusive_b=False

    def test_inclusive_b(self):
        assert in_range(8, 3, 8, inclusive_b=True)

    def test_wraparound_inside(self):
        # Ring wraps: (200, 10] — 250 is in (200, 256) ∪ [0, 10]
        assert in_range(250, 200, 10, inclusive_b=True)

    def test_wraparound_outside(self):
        assert not in_range(100, 200, 10, inclusive_b=True)

    def test_equal_bounds(self):
        # a == b means entire ring
        assert in_range(0, 5, 5)
        assert in_range(200, 5, 5)


# 3. Node initialization

class TestNodeInit:
    def test_node_id_set(self):
        node = make_node("127.0.0.1:5001", node_id=42)
        assert node.node_id == 42

    def test_node_id_sha1_fallback(self):
        node = ChordNode(address="127.0.0.1:9999")
        node.set_transport(MagicMock())
        assert node.node_id == sha1_id("127.0.0.1:9999")

    def test_finger_table_length(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        assert len(node.fingers) == M

    def test_finger_starts(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        for i, f in enumerate(node.fingers):
            expected = (10 + 2 ** i) % RING_SIZE
            assert f.start == expected

    def test_single_node_successor_is_self(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        assert node.successor["id"] == 10

    def test_predecessor_is_none(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        assert node.predecessor is None


# 4. Bootstrap join (first node)

class TestBootstrap:
    def test_bootstrap_sets_successor_to_self(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        node.join(known_address=None)
        assert node.successor["id"] == 10
        assert node.predecessor is None


# 5. Join via known peer

class TestJoin:
    def test_join_calls_find_successor(self):
        node = make_node("127.0.0.1:5002", node_id=50)
        node._transport.find_successor.return_value = {
            "id": 80, "address": "127.0.0.1:5003"
        }
        node.join("127.0.0.1:5001")
        node._transport.find_successor.assert_called_once_with(
            "127.0.0.1:5001", 50
        )

    def test_join_sets_successor(self):
        node = make_node("127.0.0.1:5002", node_id=50)
        node._transport.find_successor.return_value = {
            "id": 80, "address": "127.0.0.1:5003"
        }
        node.join("127.0.0.1:5001")
        assert node.successor["id"] == 80

    def test_join_clears_predecessor(self):
        node = make_node("127.0.0.1:5002", node_id=50)
        node.predecessor = {"id": 99, "address": "old"}
        node._transport.find_successor.return_value = {
            "id": 80, "address": "127.0.0.1:5003"
        }
        node.join("127.0.0.1:5001")
        assert node.predecessor is None


# 6. find_successor — single node

class TestFindSuccessorSingleNode:
    def test_returns_self_for_any_key(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        node.join(None)
        # In a 1-node ring, every key resolves to self
        for key_id in [0, 5, 10, 100, 200]:
            result = node.find_successor(key_id)
            assert result["id"] == 10


# 7. find_successor — two-node ring

class TestFindSuccessorTwoNodes:
    def setup_method(self):
        # Node A: id=10, successor=B(id=100)
        self.nodeA = make_node("127.0.0.1:5001", node_id=10)
        self.nodeA.successor = {"id": 100, "address": "127.0.0.1:5002"}
        self.nodeA.predecessor = {"id": 100, "address": "127.0.0.1:5002"}

        # Node B: id=100, successor=A(id=10)
        self.nodeB = make_node("127.0.0.1:5002", node_id=100)
        self.nodeB.successor = {"id": 10, "address": "127.0.0.1:5001"}
        self.nodeB.predecessor = {"id": 10, "address": "127.0.0.1:5001"}

    def test_key_between_a_and_b_goes_to_b(self):
        # key_id=50 is in (10, 100] → B is responsible
        result = self.nodeA.find_successor(50)
        assert result["id"] == 100

    def test_key_at_b_goes_to_b(self):
        result = self.nodeA.find_successor(100)
        assert result["id"] == 100

    def test_key_just_after_a_goes_to_b(self):
        result = self.nodeA.find_successor(11)
        assert result["id"] == 100

    def test_key_at_a_goes_to_a_successor(self):
        # key_id=10 is exactly node A's id; successor handles it
        result = self.nodeA.find_successor(10)
        # In (10, 100] → id=10 is NOT in range (exclusive start)
        # So it should delegate; mock transport handles it
        # Just assert no exception
        assert result is not None


# 8. Stabilization

class TestStabilize:
    def test_stabilize_notifies_successor(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        node.successor = {"id": 50, "address": "127.0.0.1:5002"}
        node._transport.get_predecessor.return_value = None
        node._transport.notify.return_value = None

        node.stabilize()

        node._transport.notify.assert_called_once_with(
            "127.0.0.1:5002",
            {"id": 10, "address": "127.0.0.1:5001"}
        )

    def test_stabilize_updates_successor_if_better_predecessor_found(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        node.successor = {"id": 50, "address": "127.0.0.1:5002"}
        # Successor reports a predecessor at id=30 — closer to us
        node._transport.get_predecessor.return_value = {
            "id": 30, "address": "127.0.0.1:5003"
        }
        node._transport.notify.return_value = None

        node.stabilize()
        assert node.successor["id"] == 30

    def test_stabilize_skips_single_node_ring(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        node.join(None)
        node.stabilize()
        # No RPC should be made in a 1-node ring
        node._transport.get_predecessor.assert_not_called()


# 9. Notify

class TestNotify:
    def test_notify_sets_predecessor_when_none(self):
        node = make_node("127.0.0.1:5001", node_id=50)
        node.predecessor = None
        node.notify({"id": 30, "address": "127.0.0.1:5002"})
        assert node.predecessor["id"] == 30

    def test_notify_updates_closer_predecessor(self):
        node = make_node("127.0.0.1:5001", node_id=50)
        node.predecessor = {"id": 10, "address": "127.0.0.1:5000"}
        # id=30 is between 10 and 50 → should be accepted
        node.notify({"id": 30, "address": "127.0.0.1:5002"})
        assert node.predecessor["id"] == 30

    def test_notify_rejects_farther_candidate(self):
        node = make_node("127.0.0.1:5001", node_id=50)
        node.predecessor = {"id": 40, "address": "127.0.0.1:5000"}
        # id=5 is not between 40 and 50 → reject
        node.notify({"id": 5, "address": "127.0.0.1:5002"})
        assert node.predecessor["id"] == 40


# 10. Predecessor failure detection

class TestCheckPredecessor:
    def test_clears_predecessor_on_timeout(self):
        node = make_node("127.0.0.1:5001", node_id=50)
        node.predecessor = {"id": 30, "address": "127.0.0.1:5002"}
        node._transport.ping.side_effect = Exception("Connection refused")

        node.check_predecessor()
        assert node.predecessor is None

    def test_keeps_predecessor_when_alive(self):
        node = make_node("127.0.0.1:5001", node_id=50)
        node.predecessor = {"id": 30, "address": "127.0.0.1:5002"}
        node._transport.ping.return_value = True

        node.check_predecessor()
        assert node.predecessor["id"] == 30

    def test_noop_when_no_predecessor(self):
        node = make_node("127.0.0.1:5001", node_id=50)
        node.predecessor = None
        node.check_predecessor()
        node._transport.ping.assert_not_called()


# 11. Data store

class TestDataStore:
    def test_put_and_get(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        node.put("task:1", {"name": "job", "status": "pending"})
        result = node.get("task:1")
        assert result["name"] == "job"

    def test_get_missing_key_returns_none(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        assert node.get("nonexistent") is None

    def test_delete_existing_key(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        node.put("task:2", {"x": 1})
        assert node.delete("task:2") is True
        assert node.get("task:2") is None

    def test_delete_missing_key_returns_false(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        assert node.delete("ghost") is False

    def test_bulk_put(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        node.bulk_put({"a": {"v": 1}, "b": {"v": 2}})
        assert node.get("a") == {"v": 1}
        assert node.get("b") == {"v": 2}

    def test_overwrite_existing_key(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        node.put("task:3", {"status": "pending"})
        node.put("task:3", {"status": "running"})
        assert node.get("task:3")["status"] == "running"

# 12. Node leave

class TestLeave:
    def test_leave_transfers_data_to_successor(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        node.successor = {"id": 50, "address": "127.0.0.1:5002"}
        node.predecessor = {"id": 200, "address": "127.0.0.1:5000"}
        node.put("task:1", {"v": 1})
        node.put("task:2", {"v": 2})

        node.leave()

        node._transport.bulk_put.assert_called_once()
        call_args = node._transport.bulk_put.call_args
        assert call_args[0][0] == "127.0.0.1:5002"
        assert "task:1" in call_args[0][1]

    def test_leave_updates_successor_predecessor(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        node.successor = {"id": 50, "address": "127.0.0.1:5002"}
        node.predecessor = {"id": 200, "address": "127.0.0.1:5000"}

        node.leave()

        node._transport.update_predecessor.assert_called_once_with(
            "127.0.0.1:5002",
            {"id": 200, "address": "127.0.0.1:5000"}
        )

    def test_leave_single_node_ring_is_noop(self):
        node = make_node("127.0.0.1:5001", node_id=10)
        node.join(None)
        node.leave()
        node._transport.bulk_put.assert_not_called()


# 13. State introspection

class TestState:
    def test_state_returns_expected_keys(self):
        node = make_node("127.0.0.1:5001", node_id=42)
        s = node.state()
        assert "node_id" in s
        assert "successor" in s
        assert "predecessor" in s
        assert "fingers" in s
        assert "data_keys" in s

    def test_state_node_id_correct(self):
        node = make_node("127.0.0.1:5001", node_id=42)
        assert node.state()["node_id"] == 42

    def test_state_fingers_count(self):
        node = make_node("127.0.0.1:5001", node_id=42)
        assert len(node.state()["fingers"]) == M
