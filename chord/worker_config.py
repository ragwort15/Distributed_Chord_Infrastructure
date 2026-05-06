"""Phase 4: constants for the standalone task-execution worker.
Phase 6: also holds the placement-scoring weights used server-side.
"""

POLL_INTERVAL_SECONDS = 2          # how often the worker polls HT2 for new task_ids
HEARTBEAT_INTERVAL_SECONDS = 5     # how often the worker pings /workers/heartbeat
EXECUTION_TIMEOUT_SECONDS = 60     # wall-clock cap per task
HTTP_TIMEOUT_SECONDS = 5           # per-call socket timeout for worker→frontend HTTP
RESULT_REPORT_RETRIES = 3          # extra attempts to POST /…/complete on failure

# Phase 6 — intelligent placement weights.
# Score = LOAD * (-pending_tasks) + LATENCY * (-latency_ms / norm) + AVAILABILITY * success_rate
# Tune these to reflect priorities; weights don't have to sum to 1.0.
PLACEMENT_WEIGHT_LOAD = 0.5
PLACEMENT_WEIGHT_LATENCY = 0.3
PLACEMENT_WEIGHT_AVAILABILITY = 0.2
LATENCY_NORMALIZATION_MS = 100     # divisor for latency — tune to your network's typical RTT

# Phase 7 — failure recovery
MAX_RECOVERY_ATTEMPTS           = 8   # give up after this many total assignment attempts
RECOVERY_CHECK_INTERVAL_SECONDS = 8   # how often the recovery loop scans for failures
RECOVERY_WAIT_SECONDS           = 30  # delay before retrying when all workers are down
