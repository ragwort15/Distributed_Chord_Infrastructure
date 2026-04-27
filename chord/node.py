"""
Implements:
  - SHA-1 consistent hashing
  - Finger table construction & lookup
  - Successor/predecessor management
  - Stabilization protocol
  - Node join/graceful leave
"""

import hashlib
import threading
import time
import logging

logger = logging.getLogger(__name__)

# Chord key space: 2^M identifiers
M = 8  # Use 8-bit ring for easy local testing; set to 160 for production SHA-1


def sha1_id(key: str) -> int:
    h = hashlib.sha1(key.encode()).hexdigest()
    return int(h, 16) % (2 ** M)


def in_range(x: int, a: int, b: int, inclusive_b: bool = False) -> bool:
    if a == b:
        return True  # entire ring
    if a < b:
        if inclusive_b:
            return a < x <= b
        return a < x < b
    else:  # wraparound
        if inclusive_b:
            return x > a or x <= b
        return x > a or x < b


class FingerEntry:
    def __init__(self, start: int):
        self.start = start          # (n + 2^i) mod 2^M
        self.node_id: int = None    # ID of the successor node for this finger
        self.node_address: str = None  # "host:port" of that node


class ChordNode:
    """
    Represents a single node in the Chord ring.
    """

    def __init__(self, address: str, node_id: int = None):
        self.address = address
        self.node_id = node_id if node_id is not None else sha1_id(address)
        self.predecessor: dict = None
        self.fingers: list[FingerEntry] = self._init_fingers()
        self.data_store: dict = {}
        self._lock = threading.RLock()
        self._transport = None  # injected after construction

        # Job execution counters (updated by WorkerThread)
        self.jobs_completed: int = 0
        self.jobs_failed: int = 0

        # Point successor to self initially (single-node ring)
        self.fingers[0].node_id = self.node_id
        self.fingers[0].node_address = self.address

        logger.info(f"[Node {self.node_id}] Initialized at {self.address}")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def successor(self) -> dict:
        f = self.fingers[0]
        return {"id": f.node_id, "address": f.node_address}

    @successor.setter
    def successor(self, value: dict):
        self.fingers[0].node_id = value["id"]
        self.fingers[0].node_address = value["address"]

    def set_transport(self, transport):
        self._transport = transport

    # Finger table initialization

    def _init_fingers(self) -> list:
        fingers = []
        for i in range(M):
            start = (self.node_id + 2 ** i) % (2 ** M)
            fe = FingerEntry(start)
            fe.node_id = self.node_id
            fe.node_address = self.address
            fingers.append(fe)
        return fingers

    # Core Chord lookup

    def find_successor(self, key_id: int) -> dict:
        if in_range(key_id, self.node_id, self.successor["id"], inclusive_b=True):
            return self.successor

        # Find the closest preceding node and delegate
        n_prime = self._closest_preceding_node(key_id)
        if n_prime["id"] == self.node_id:
            return self.successor  # avoid infinite loop in 1-node ring

        # Remote call
        try:
            return self._transport.find_successor(n_prime["address"], key_id)
        except Exception as e:
            logger.warning(f"[Node {self.node_id}] find_successor RPC failed: {e}")
            return self.successor  # fallback

    def _closest_preceding_node(self, key_id: int) -> dict:
        """
        Walk finger table from M-1 down to 0.
        Return the furthest node that precedes key_id on the ring.
        """
        for i in range(M - 1, -1, -1):
            f = self.fingers[i]
            if f.node_id is not None and in_range(f.node_id, self.node_id, key_id):
                return {"id": f.node_id, "address": f.node_address}
        return {"id": self.node_id, "address": self.address}

    # Join protocol

    def join(self, known_address: str = None):
        """
        Join the Chord ring.
        """
        with self._lock:
            if known_address is None:
                self.predecessor = None
                self.successor = {"id": self.node_id, "address": self.address}
                logger.info(f"[Node {self.node_id}] Bootstrapped as first node")
            else:
                self.predecessor = None
                succ = self._transport.find_successor(known_address, self.node_id)
                self.successor = succ
                logger.info(
                    f"[Node {self.node_id}] Joined ring via {known_address}, "
                    f"successor={succ['id']}"
                )

    # Stabilization (run periodically)

    def stabilize(self):
        with self._lock:
            try:
                succ = self.successor
                if succ["id"] == self.node_id:
                    # Successor points to self but we have a predecessor —
                    # bootstrap by using predecessor as successor so stabilize
                    # can discover the real ring topology on the next cycle.
                    if self.predecessor and self.predecessor["id"] != self.node_id:
                        self.successor = self.predecessor
                    return

                # Ask successor for its predecessor
                x = self._transport.get_predecessor(succ["address"])

                if x and in_range(x["id"], self.node_id, succ["id"]):
                    self.successor = x
                    logger.debug(
                        f"[Node {self.node_id}] Stabilize: updated successor to {x['id']}"
                    )

                # Notify our (possibly new) successor about us
                self._transport.notify(
                    self.successor["address"],
                    {"id": self.node_id, "address": self.address}
                )
            except Exception as e:
                logger.warning(f"[Node {self.node_id}] Stabilize failed: {e}")

    def notify(self, candidate: dict):
        """
        A node thinks it might be our predecessor.
        """
        with self._lock:
            if (self.predecessor is None or
                    in_range(candidate["id"], self.predecessor["id"], self.node_id)):
                self.predecessor = candidate
                logger.debug(
                    f"[Node {self.node_id}] Accepted predecessor {candidate['id']}"
                )

    def fix_fingers(self):
        """
        Refresh one finger table entry per call (rotate through all M fingers).
        """
        with self._lock:
            # Pick a random finger to fix (or cycle through)
            import random
            i = random.randint(1, M - 1)
            self.fingers[i].node_id = None 
            target = self.fingers[i].start
            result = self.find_successor(target)
            self.fingers[i].node_id = result["id"]
            self.fingers[i].node_address = result["address"]

    def check_predecessor(self):
        """
        If predecessor has failed, clear it so we can accept a new one.
        """
        with self._lock:
            if self.predecessor is None:
                return
            try:
                self._transport.ping(self.predecessor["address"])
            except Exception:
                logger.info(
                    f"[Node {self.node_id}] Predecessor {self.predecessor['id']} "
                    f"is unreachable — clearing"
                )
                self.predecessor = None

    # Graceful leave

    def leave(self):
        """
        Gracefully depart from the ring:
          1. Transfer our data store to our successor.
          2. Inform successor of our predecessor.
          3. Inform predecessor of our successor.
        """
        with self._lock:
            succ = self.successor
            pred = self.predecessor

            if succ["id"] == self.node_id:
                logger.info(f"[Node {self.node_id}] Last node leaving — ring dissolved")
                return

            # Transfer data
            if self.data_store:
                try:
                    self._transport.bulk_put(succ["address"], self.data_store)
                    logger.info(
                        f"[Node {self.node_id}] Transferred {len(self.data_store)} "
                        f"keys to successor {succ['id']}"
                    )
                except Exception as e:
                    logger.error(f"[Node {self.node_id}] Data transfer failed: {e}")

            # Update successor's predecessor pointer
            if pred:
                try:
                    self._transport.update_predecessor(succ["address"], pred)
                except Exception as e:
                    logger.warning(f"[Node {self.node_id}] Could not update successor's pred: {e}")

            # Update predecessor's successor pointer
            if pred:
                try:
                    self._transport.update_successor(pred["address"], succ)
                except Exception as e:
                    logger.warning(f"[Node {self.node_id}] Could not update predecessor's succ: {e}")

            logger.info(f"[Node {self.node_id}] Left ring gracefully")

    # Data store (tasks / metadata)

    def put(self, key: str, value: dict) -> bool:
        """Store a key locally (called after routing confirms we're responsible)."""
        with self._lock:
            self.data_store[key] = value
            logger.debug(f"[Node {self.node_id}] Stored key={key}")
            return True

    def get(self, key: str) -> dict | None:
        """Retrieve a key from local store."""
        with self._lock:
            return self.data_store.get(key)

    def delete(self, key: str) -> bool:
        with self._lock:
            return self.data_store.pop(key, None) is not None

    def bulk_put(self, items: dict):
        """Accept a batch of keys (used during node join/leave handoff)."""
        with self._lock:
            self.data_store.update(items)
            logger.info(f"[Node {self.node_id}] Bulk received {len(items)} keys")

    # Agent metrics

    def metrics(self) -> dict:
        """Lightweight snapshot used by the agent loop and /metrics endpoint."""
        with self._lock:
            queue_depth = sum(
                1 for v in self.data_store.values()
                if isinstance(v, dict) and v.get("status") in ("pending", "claimed", "running")
            )
            return {
                "node_id": self.node_id,
                "address": self.address,
                "queue_depth": queue_depth,
                "jobs_completed": self.jobs_completed,
                "jobs_failed": self.jobs_failed,
            }

    # Debug / introspection

    def state(self) -> dict:
        """Return full node state (for REST /state endpoint)."""
        with self._lock:
            return {
                "node_id": self.node_id,
                "address": self.address,
                "successor": self.successor,
                "predecessor": self.predecessor,
                "fingers": [
                    {
                        "index": i,
                        "start": f.start,
                        "node_id": f.node_id,
                        "node_address": f.node_address,
                    }
                    for i, f in enumerate(self.fingers)
                ],
                "data_keys": list(self.data_store.keys()),
            }
