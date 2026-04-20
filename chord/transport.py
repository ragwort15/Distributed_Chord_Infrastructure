"""
HTTP/REST transport layer for Chord inter-node RPCs.

All node-to-node calls go through this class, keeping the core
ChordNode logic completely decoupled from networking.
"""

import requests
import logging

logger = logging.getLogger(__name__)

# Timeout for all inter-node HTTP calls (seconds)
RPC_TIMEOUT = 3


class HttpTransport:
    """
    Wraps every inter-node RPC as a simple HTTP call.
    """

    # Core Chord RPCs

    def find_successor(self, address: str, key_id: int) -> dict:
        r = requests.get(
            f"http://{address}/chord/find_successor",
            params={"id": key_id},
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()   # {"id": int, "address": str}

    def get_predecessor(self, address: str) -> dict | None:
        r = requests.get(
            f"http://{address}/chord/predecessor",
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        return data if data.get("id") is not None else None

    def notify(self, address: str, candidate: dict):
        r = requests.post(
            f"http://{address}/chord/notify",
            json=candidate,
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()

    def ping(self, address: str) -> bool:
        r = requests.get(
            f"http://{address}/chord/ping",
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()
        return True

    # Leave / pointer update RPCs

    def update_predecessor(self, address: str, new_pred: dict):
        r = requests.post(
            f"http://{address}/chord/update_predecessor",
            json=new_pred,
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()

    def update_successor(self, address: str, new_succ: dict):
        r = requests.post(
            f"http://{address}/chord/update_successor",
            json=new_succ,
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()

    # Data transfer RPCs

    def put(self, address: str, key: str, value: dict) -> bool:
        r = requests.post(
            f"http://{address}/data/{key}",
            json=value,
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()
        return True

    def get(self, address: str, key: str) -> dict | None:
        r = requests.get(
            f"http://{address}/data/{key}",
            timeout=RPC_TIMEOUT,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def bulk_put(self, address: str, items: dict):
        r = requests.post(
            f"http://{address}/chord/bulk_put",
            json=items,
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()
