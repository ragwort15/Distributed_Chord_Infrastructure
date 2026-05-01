"""
HTTP/REST transport layer for Chord inter-node RPCs.

All node-to-node calls go through this class, keeping the core
ChordNode logic completely decoupled from networking.
"""

import time
import requests
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Timeout for all inter-node HTTP calls (seconds)
RPC_TIMEOUT = 3
# Retries for transient failures (network blip, node briefly busy)
RPC_RETRIES = 1
RPC_RETRY_DELAY = 0.3


def _get(url, **kwargs):
    for attempt in range(RPC_RETRIES + 1):
        try:
            return requests.get(url, **kwargs)
        except requests.RequestException:
            if attempt == RPC_RETRIES:
                raise
            time.sleep(RPC_RETRY_DELAY)


def _post(url, **kwargs):
    for attempt in range(RPC_RETRIES + 1):
        try:
            return requests.post(url, **kwargs)
        except requests.RequestException:
            if attempt == RPC_RETRIES:
                raise
            time.sleep(RPC_RETRY_DELAY)


class HttpTransport:
    """
    Wraps every inter-node RPC as a simple HTTP call with one retry
    on transient network failures.
    """

    # Core Chord RPCs

    def find_successor(self, address: str, key_id: int) -> dict:
        r = _get(
            f"http://{address}/chord/find_successor",
            params={"id": key_id},
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()

    def get_predecessor(self, address: str) -> Optional[dict]:
        r = _get(
            f"http://{address}/chord/predecessor",
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        return data if data.get("id") is not None else None

    def notify(self, address: str, candidate: dict):
        r = _post(
            f"http://{address}/chord/notify",
            json=candidate,
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()

    def ping(self, address: str) -> bool:
        r = _get(
            f"http://{address}/chord/ping",
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()
        return True

    # Leave / pointer update RPCs

    def update_predecessor(self, address: str, new_pred: dict):
        r = _post(
            f"http://{address}/chord/update_predecessor",
            json=new_pred,
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()

    def update_successor(self, address: str, new_succ: dict):
        r = _post(
            f"http://{address}/chord/update_successor",
            json=new_succ,
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()

    # Data transfer RPCs

    def put(self, address: str, key: str, value: dict) -> bool:
        r = _post(
            f"http://{address}/data/{key}",
            json=value,
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()
        return True

    def get(self, address: str, key: str) -> Optional[dict]:
        r = _get(
            f"http://{address}/data/{key}",
            timeout=RPC_TIMEOUT,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def delete(self, address: str, key: str) -> bool:
        r = requests.delete(
            f"http://{address}/data/{key}",
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        return bool(data.get("ok"))

    def bulk_put(self, address: str, items: dict):
        r = _post(
            f"http://{address}/chord/bulk_put",
            json=items,
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()

    def get_metrics(self, address: str) -> dict:
        r = _get(
            f"http://{address}/metrics",
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()

    def get_state(self, address: str) -> dict:
        r = _get(
            f"http://{address}/chord/state",
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()

    def put_task_replica(self, address: str, task_key: str, task_record: dict) -> bool:
        r = _post(
            f"http://{address}/internal/tasks/replica/{task_key}",
            json=task_record,
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()
        return True

    def get_task_replica(self, address: str, task_key: str) -> Optional[dict]:
        r = _get(
            f"http://{address}/internal/tasks/replica/{task_key}",
            timeout=RPC_TIMEOUT,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        return data.get("task")

    def delete_task_replica(self, address: str, task_key: str) -> bool:
        r = requests.delete(
            f"http://{address}/internal/tasks/replica/{task_key}",
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()
        return True
