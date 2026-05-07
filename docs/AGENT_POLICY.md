# AI Agent System — Policy Documentation

## Overview

Three autonomous agents orchestrate job execution across a Chord DHT ring.
Each agent makes one structured decision via a Claude API `tool_use` call,
with an automatic heuristic fallback when the API is unavailable.

---

## Strategy Configuration

| Env var | Values | Default |
|---|---|---|
| `AGENT_STRATEGY` | `llm` / `heuristic` | `llm` |
| `ANTHROPIC_API_KEY` | your key | — |
| `AGENT_LOG_PATH` | file path | `agent_decisions.jsonl` |

Set `AGENT_STRATEGY=heuristic` to run fully offline (no API calls).

---

## Agent 1 — PlacementAgent (`chord/agent.py`)

**Goal:** Select the optimal DHT node to execute a newly submitted job.

### Signals used
| Signal | Source | Weight |
|---|---|---|
| `queue_depth` | `/metrics` on each node | Primary — lower is better |
| `jobs_failed` | `/metrics` | Secondary — prefer healthy nodes |
| Node liveness | Finger table reachability | Binary — unreachable nodes excluded |

### LLM policy
The agent sends all reachable node metrics to `claude-opus-4-7` and forces a
`select_placement_node` tool call. The model reasons over load and failure rate
and returns the chosen `node_id` with a textual justification.

### Heuristic fallback
`argmin(queue_depth, jobs_failed)` — pick the node with fewest active jobs,
break ties by fewest failures.

---

## Agent 2 — ReplicationAgent (`chord/agent.py`)

**Goal:** Decide how many total copies of a job to create and which nodes hold them.

### Signals used
| Signal | Source |
|---|---|
| `requested_replicas` | Client request body |
| `job.type` | Job definition (`compute` → high-priority, gets +1 replica) |
| `queue_depth` per node | `/metrics` — spread replicas to less-loaded nodes |

### LLM policy
The agent may **increase or decrease** the requested replication factor based on
job type and ring health. Replicas are spread across distinct nodes sorted by
queue depth. The model returns `primary_node_id`, `replica_node_ids[]`, and
`replication_factor`.

### Heuristic fallback
- `compute` jobs: bump factor to `max(requested, 2)` if ≥2 nodes available
- All jobs: cap at number of available nodes
- Placement: sort nodes by `(queue_depth, jobs_failed)`, assign in order

---

## Agent 3 — RecoveryAgent (`chord/agent.py`)

**Goal:** When a node dies, redistribute its orphaned jobs across survivors.

### Trigger
`FailureWatcherThread` (in `server.py`) watches the node's predecessor pointer.
When it drops to `None` (cleared by `check_predecessor()`), recovery runs
automatically.

### Signals used
| Signal | Source |
|---|---|
| Orphaned jobs | Local `data_store` (jobs `claimed_by` the failed node) |
| Surviving nodes | Finger table metrics snapshot |
| `queue_depth` per survivor | `/metrics` |

### LLM policy
The model assigns each `job_id` to a `target_node_id` in a single
`plan_failure_recovery` tool call. It is explicitly told **not** to blindly
route everything to the Chord successor — it must balance across all survivors.

### Heuristic fallback
Round-robin across survivors sorted by `(queue_depth, jobs_failed)`.

---

## Agent Decision Loop (`chord/agent_loop.py`)

`AgentLoop` runs as a daemon thread (default interval: 5 s). Each tick:
1. Walks the finger table and calls `/metrics` on every reachable node
2. Writes a `ring_snapshot` entry to the decision log
3. For up to 3 locally-queued `PENDING` jobs, calls `PlacementAgent.select()` and logs the advice (advisory only — actual routing already happened at submit time)

This provides continuous observability and a baseline for offline evaluation.

---

## Decision Log Format (`agent_decisions.jsonl`)

One JSON object per line:

```json
{
  "ts": 1713700000.123,
  "agent": "PlacementAgent",
  "tool": "select_placement_node",
  "strategy": "llm",
  "inputs_summary": {"job_id": "...", "job_type": "compute", "node_count": 3},
  "decision": {"node_id": 42, "reasoning": "Lowest queue_depth=1 ..."},
  "latency_ms": 342.1
}
```

`strategy` is one of: `llm`, `heuristic`, `heuristic_fallback`, `monitor`.

### Evaluation queries

```bash
# Count decisions by strategy
jq -r .strategy agent_decisions.jsonl | sort | uniq -c

# Average LLM latency
jq 'select(.strategy=="llm") | .latency_ms' agent_decisions.jsonl | awk '{s+=$1;c++} END{print s/c "ms avg"}'

# All recovery events
jq 'select(.agent=="RecoveryAgent")' agent_decisions.jsonl
```

---

## Baseline Comparison

The naïve baseline is standard Chord consistent hashing: every job is stored at
`find_successor(sha1_id("job:<uuid>"))` with no load awareness and no
replication. Compare against the AI-orchestrated run using:

- **Makespan**: time from first submit to last DONE
- **Imbalance**: `std(queue_depth)` across nodes at steady state
- **Recovery time**: elapsed between node failure and all orphaned jobs DONE
- **Replica overhead**: extra storage vs. jobs that survived a node failure
