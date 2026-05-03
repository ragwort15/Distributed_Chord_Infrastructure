"""
HTTP/REST transport layer for Chord inter-node RPCs.

All node-to-node calls go through this class, keeping the core
ChordNode logic completely decoupled from networking.

Retry policy (2025 update):
  - RPC_RETRIES = 3   (was 1 — too few for a single transient blip)
  - Exponential back-off: delay = RPC_RETRY_DELAY * 2**attempt
    e.g. 0.1 s → 0.2 s → 0.4 s before giving up (max ~0.7 s total wait)
  - All four HTTP verbs (GET, POST, DELETE, PATCH) share the same
    retry logic via `_request()`.  The old code called raw
    `requests.delete()` directly (no retry at all) — that is fixed.
"""

import time
import requests
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Timeout for all inter-node HTTP calls (seconds)
RPC_TIMEOUT = 3
# Number of retry attempts after first failure (3 = 4 total tries)
RPC_RETRIES = 3
# Base delay (seconds); each retry doubles it (exponential back-off)
RPC_RETRY_DELAY = 0.1


def _request(method: str, url: str, **kwargs):
    """
    Unified retry wrapper for all HTTP verbs.

    Why exponential back-off instead of a flat delay?
    A flat 0.3 s delay retries too quickly for a node that is
    restarting (needs a moment to bind its socket) and wastes time
    with a second attempt that fails for the same transient reason.
    Doubling the delay gives the remote node progressively more time
    to recover while keeping total wait small for short blips.
    """
    last_exc = None
    for attempt in range(RPC_RETRIES + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < RPC_RETRIES:
                delay = RPC_RETRY_DELAY * (2 ** attempt)
                logger.debug(
                    "[transport] %s %s failed (attempt %d/%d): %s — retry in %.2fs",
                    method, url, attempt + 1, RPC_RETRIES + 1, exc, delay,
                )
                time.sleep(delay)
    # All attempts exhausted — re-raise the last exception
    raise last_exc


def _get(url, **kwargs):
    return _request("GET", url, **kwargs)


def _post(url, **kwargs):
    return _request("POST", url, **kwargs)


def _delete(url, **kwargs):
    """
    DELETE with the same exponential-backoff retry as GET/POST.
    Previously `transport.delete()` and `transport.delete_task_replica()`
    called `requests.delete()` directly — zero retries.  One transient
    network hiccup would silently skip deletion entirely.
    """
    return _request("DELETE", url, **kwargs)


def _patch(url, **kwargs):
    return _request("PATCH", url, **kwargs)


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
        # Previously: raw requests.delete() — no retry on transient failures.
        # Now routed through _delete() so it gets the same 3-retry
        # exponential-backoff treatment as GET and POST calls.
        r = _delete(
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
        # Previously: raw requests.delete() — no retry.
        # Now uses _delete() for consistent retry behaviour across all verbs.
        r = _delete(
            f"http://{address}/internal/tasks/replica/{task_key}",
            timeout=RPC_TIMEOUT,
        )
        r.raise_for_status()
        return True
