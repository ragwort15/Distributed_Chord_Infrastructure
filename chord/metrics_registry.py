"""
Prometheus metrics registry — module-level singletons shared across the Flask app.
Each metric is labelled with node_id so Grafana can split by node.
"""
from prometheus_client import Counter, Gauge, Histogram

# ── File request metrics ───────────────────────────────────────────
FILE_REQUESTS = Counter(
    'chord_file_requests_total',
    'Total file requests handled by this node',
    ['node_id', 'file_type'],
)
FILE_REQUEST_HOPS = Histogram(
    'chord_file_request_hops',
    'Chord routing hops per file request (proves O(log N))',
    ['node_id'],
    buckets=[1, 2, 3, 4, 5, 6, 7, 8],
)
FILE_REQUEST_DURATION = Histogram(
    'chord_file_request_duration_seconds',
    'End-to-end file request latency in seconds',
    ['node_id'],
    buckets=[.005, .01, .025, .05, .1, .25, .5, 1.0, 2.5, 5.0],
)

# ── Job metrics ────────────────────────────────────────────────────
JOBS_TOTAL = Counter(
    'chord_jobs_total',
    'Total jobs by outcome',
    ['node_id', 'status'],
)
QUEUE_DEPTH = Gauge(
    'chord_queue_depth',
    'Active jobs (pending/claimed/running) in local store',
    ['node_id'],
)

# ── Ring / protocol metrics ────────────────────────────────────────
RING_SIZE = Gauge(
    'chord_ring_size',
    'Estimated number of live nodes in the ring',
    ['node_id'],
)
DATA_KEYS = Gauge(
    'chord_data_keys_total',
    'Total keys in local DHT store',
    ['node_id'],
)
STABILIZE_RUNS = Counter(
    'chord_stabilize_runs_total',
    'Number of stabilization protocol executions',
    ['node_id'],
)
FINGER_FIX_RUNS = Counter(
    'chord_finger_fix_runs_total',
    'Number of finger-table fix executions',
    ['node_id'],
)
PREDECESSOR_FAILURES = Counter(
    'chord_predecessor_failures_total',
    'Number of times predecessor was detected as dead',
    ['node_id'],
)
