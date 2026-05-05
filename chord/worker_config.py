"""Phase 4: constants for the standalone task-execution worker."""

POLL_INTERVAL_SECONDS = 2          # how often the worker polls HT2 for new task_ids
HEARTBEAT_INTERVAL_SECONDS = 5     # how often the worker pings /workers/heartbeat
EXECUTION_TIMEOUT_SECONDS = 60     # wall-clock cap per task
HTTP_TIMEOUT_SECONDS = 5           # per-call socket timeout for worker→frontend HTTP
RESULT_REPORT_RETRIES = 3          # extra attempts to POST /…/complete on failure
