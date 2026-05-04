"""
Phase 3: retry-with-backoff helper for /createTask atomic write protocol.

Constants control retry budget and exponential backoff between attempts.
Tunable here so the rest of the codebase doesn't hard-code them.

Note on timeouts: per-write timeout is enforced at the *transport* layer
(``RPC_TIMEOUT`` in chord/transport.py — 3s socket timeout). An earlier
revision of this helper added a thread-based wrapper to enforce
``DHT_WRITE_TIMEOUT_MS`` independently, but in practice that produced
orphaned daemon threads under load (Flask dev server + self-HTTP calls
during replication) which cascaded into "can't start new thread"
failures. Trusting the transport's socket timeout is simpler and safe
because every Phase 3 op routes through the transport.
"""

import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# All values in milliseconds.
DHT_WRITE_TIMEOUT_MS = 1500     # informational; transport enforces the actual socket timeout
TOTAL_ACCEPT_TIMEOUT_MS = 5000  # informational upper bound on full createTask flow
MAX_RETRIES = 3
INITIAL_BACKOFF_MS = 100
BACKOFF_MULTIPLIER = 2


class FatalError(Exception):
    """Raise from a retried op to skip remaining retries (e.g. validation)."""


def with_timeout_and_retry(
    op: Callable[[], Any],
    timeout_ms: int = DHT_WRITE_TIMEOUT_MS,        # noqa: ARG001 — kept for API compat
    max_retries: int = MAX_RETRIES,
    initial_backoff_ms: int = INITIAL_BACKOFF_MS,
    backoff_multiplier: float = BACKOFF_MULTIPLIER,
    op_name: str = "op",
) -> Any:
    """
    Run ``op`` with exponential-backoff retries on transient errors.

    Returns the op's value on success.

    Raises:
        FatalError: re-raised immediately to skip remaining retries.
        RuntimeError: after exhausting all retries (wraps the last exception).
    """
    backoff_s = initial_backoff_ms / 1000.0
    last_exc: Optional[BaseException] = None

    for attempt in range(max_retries + 1):
        try:
            return op()
        except FatalError:
            raise
        except BaseException as exc:
            last_exc = exc
            logger.warning(
                "[retry] %s attempt %d/%d raised %s: %s",
                op_name, attempt + 1, max_retries + 1,
                type(exc).__name__, exc,
            )
            if attempt < max_retries:
                time.sleep(backoff_s)
                backoff_s *= backoff_multiplier

    raise RuntimeError(
        f"{op_name} failed after {max_retries + 1} attempts: {last_exc}"
    ) from last_exc
