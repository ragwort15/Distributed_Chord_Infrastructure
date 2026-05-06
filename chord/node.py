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
from typing import List, Optional

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
        self.fingers: List[FingerEntry] = self._init_fingers()
        self.data_store: dict = {}
        self._lock = threading.RLock()
        self._transport = None  # injected after construction

        # Job execution counters (updated by WorkerThread)
        self.jobs_completed: int = 0
        self.jobs_failed: int = 0

        # Point successor to self initially (single-node ring)
        self.fingers[0].node_id = self.node_id
        self.fingers[0].node_address = self.address

        # ---- Persistence: snapshot data_store to disk so we can recover from
        # a full ring restart. CHORD_DATA_DIR env var overrides the default.
        import os, json
        data_dir = os.environ.get("CHORD_DATA_DIR") or os.path.join(
            os.path.expanduser("~"), ".chord-data"
        )
        try:
            os.makedirs(data_dir, exist_ok=True)
        except Exception:
            data_dir = None
        self._persist_path = (
            os.path.join(data_dir, f"node-{self.node_id}.json")
            if data_dir else None
        )
        self._load_from_disk()

        logger.info(f"[Node {self.node_id}] Initialized at {self.address}"
                    f" (persistence: {self._persist_path or 'disabled'},"
                    f" {len(self.data_store)} keys recovered)")

    def _load_from_disk(self) -> None:
        if not self._persist_path:
            return
        import os, json
        if not os.path.exists(self._persist_path):
            return
        try:
            with open(self._persist_path, "r") as f:
                self.data_store = json.load(f) or {}
        except Exception as exc:
            logger.warning(f"[Node {self.node_id}] failed to load snapshot: {exc}")

    def _save_to_disk(self) -> None:
        if not self._persist_path:
            return
        import json
        try:
            tmp = self._persist_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data_store, f)
            import os
            os.replace(tmp, self._persist_path)
        except Exception as exc:
            logger.warning(f"[Node {self.node_id}] snapshot write failed: {exc}")

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
        """
        Chord stabilization protocol.

        CRITICAL FIX: the original implementation held self._lock for the
        entire method, including all three RPC calls (get_predecessor, notify,
        and the fallback ping loop).  With RPC_TIMEOUT=3s and RPC_RETRIES=3,
        each call can block for up to 7 s, meaning the lock could be held for
        21+ seconds per cycle while maintenance runs every 2 s.

        Any thread that needs self._lock during that window — including Flask
        request handlers for /chord/state, /api/ring, and /nodes/self — is
        blocked for the full duration.  /api/ring uses a 1.5 s timeout, so
        the node appears unreachable to the ring walk, making it look "dead"
        in the dashboard.

        Fix: snapshot all needed data under the lock, release it, perform all
        RPCs without holding the lock, then reacquire briefly to apply updates.
        """
        # ── Step 1: snapshot state (lock held for microseconds only) ─────────
        with self._lock:
            succ_id   = self.fingers[0].node_id
            succ_addr = self.fingers[0].node_address
            my_id     = self.node_id
            my_addr   = self.address
            pred      = self.predecessor

        # ── Single-node ring: bootstrap via predecessor ───────────────────────
        if succ_id == my_id:
            if pred and pred["id"] != my_id:
                with self._lock:
                    self.successor = pred
            return

        # ── Step 2: all RPCs happen OUTSIDE the lock ──────────────────────────
        try:
            x = self._transport.get_predecessor(succ_addr)  # RPC — no lock held

            # ── Step 3: apply successor update under lock ─────────────────────
            with self._lock:
                current_succ = self.successor   # re-read in case another thread updated it
                if x and in_range(x["id"], my_id, current_succ["id"]):
                    self.successor = x
                    logger.debug(f"[Node {my_id}] Stabilize: updated successor to {x['id']}")
                notify_addr = self.successor["address"]

            # Notify our (possibly new) successor — RPC, no lock held
            self._transport.notify(notify_addr, {"id": my_id, "address": my_addr})

        except Exception as e:
            logger.warning(f"[Node {my_id}] Stabilize failed: {e}")
            # Successor may be dead — build finger candidate list under lock
            # then ping each candidate WITHOUT holding the lock.
            with self._lock:
                candidates = [
                    {"index": i, "id": self.fingers[i].node_id,
                     "address": self.fingers[i].node_address}
                    for i in range(M - 1, 0, -1)
                    if self.fingers[i].node_id is not None
                    and self.fingers[i].node_id != my_id
                ]
            for c in candidates:
                try:
                    self._transport.ping(c["address"])   # RPC — no lock held
                    with self._lock:
                        self.successor = {"id": c["id"], "address": c["address"]}
                    logger.info(
                        f"[Node {my_id}] Successor dead; fell back to "
                        f"finger {c['index']} (node {c['id']})"
                    )
                    break
                except Exception:
                    continue

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

        Bug fix: the old code set fingers[i].node_id = None before calling
        find_successor, creating a brief window where the finger was broken.
        During that window the /api/ring walk could miss the finger's node,
        making it appear dead in the UI. Fix: compute the new value first,
        then atomically swap it in.
        """
        import random
        with self._lock:
            i = random.randint(1, M - 1)
            target = self.fingers[i].start
        # find_successor does its own locking; call it outside our lock
        # to avoid holding the lock during a potentially slow RPC chain.
        try:
            result = self.find_successor(target)
        except Exception as e:
            logger.debug(f"[Node {self.node_id}] fix_fingers({i}) failed: {e}")
            return
        with self._lock:
            self.fingers[i].node_id = result["id"]
            self.fingers[i].node_address = result["address"]

    def check_predecessor(self):
        """
        If predecessor has failed, clear it so we can accept a new one.

        Bug fix: the old code held self._lock while making the HTTP ping call.
        If the ping needed retries (up to ~7 s) every other lock-holder was
        blocked for that entire duration, stalling stabilise() and request
        handlers.  Fix: snapshot the predecessor under the lock, release it,
        do the ping, then re-acquire the lock only to clear the pointer.

        We also require TWO consecutive failures before clearing, so a single
        transient network hiccup doesn't prematurely evict a healthy node.
        """
        with self._lock:
            pred = self.predecessor

        if pred is None:
            return

        try:
            self._transport.ping(pred["address"])
            # Successful ping — reset the consecutive-failure counter
            self._pred_fail_count = 0
        except Exception:
            self._pred_fail_count = getattr(self, "_pred_fail_count", 0) + 1
            if self._pred_fail_count < 2:
                logger.debug(
                    f"[Node {self.node_id}] Predecessor {pred['id']} ping failed "
                    f"(attempt {self._pred_fail_count}/2) — will retry next cycle"
                )
                return
            with self._lock:
                # Only clear if it's still the same predecessor (nothing changed)
                if self.predecessor and self.predecessor["id"] == pred["id"]:
                    logger.info(
                        f"[Node {self.node_id}] Predecessor {pred['id']} "
                        f"unreachable after 2 consecutive checks — clearing"
                    )
                    self.predecessor = None
            self._pred_fail_count = 0

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
            self._save_to_disk()
            logger.debug(f"[Node {self.node_id}] Stored key={key}")
            return True

    def get(self, key: str) -> Optional[dict]:
        """Retrieve a key from local store."""
        with self._lock:
            return self.data_store.get(key)

    def delete(self, key: str) -> bool:
        with self._lock:
            removed = self.data_store.pop(key, None) is not None
            if removed:
                self._save_to_disk()
            return removed

    def bulk_put(self, items: dict):
        """Accept a batch of keys (used during node join/leave handoff)."""
        with self._lock:
            self.data_store.update(items)
            self._save_to_disk()
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
