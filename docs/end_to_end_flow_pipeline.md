# End-to-End Flow Pipeline: Distributed Job Execution Platform

## Complete System Overview (All 7 Phases)

This document shows the **complete data flow** through all seven phases of the system, from user request to task completion (or recovery).

---

## System Architecture (7-Layer View)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                   Phase 2: AI Agent / Coordinator                       │
│                   (Conversational interface)                            │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│              Phase 2/6: Frontend API Layer                              │
│         (/createTask, /getStatus, /workers/*)                          │
│     [Atomic writes, Phase 6 placement scoring]                         │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│              Phase 7: Recovery Manager                                  │
│    (Failure detection & intelligent recovery orchestration)            │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│              Phase 4: Worker Layer                                      │
│        (Polling HT2, execution, result write-back)                     │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│       Phase 3: Distributed Hash Tables (HT1 + HT2)                     │
│         (Task records + worker assignments)                            │
│        [Atomic 2-write, idempotency, retries]                          │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────┐
│              Phase 5: Chord Protocol Layer                              │
│    (O(log N) key lookup via finger tables)                             │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## End-to-End Scenario: Complete Task Lifecycle

### Scenario Setup

- **User**: Alice
- **AI Agent**: Conversational interface
- **Cluster**: 3 workers (Worker-1, Worker-2, Worker-3)
- **Task**: A backup script that normally succeeds, but this time Worker-1 crashes mid-execution

---

## PHASE 1: Task Submission (via AI Agent)

### Step 1.1: User Initiates Request

```
User (Alice):
  "I want to run my daily backup script"

AI Agent (Phase 2):
  "I can help with that. Is it a [1] Script or [2] Binary?"
```

### Step 1.2: AI Agent Collects Details

```
User:
  "1. It's a script"

AI Agent:
  "Please provide the script content or path"

User:
  "#!/bin/bash\necho Backing up... && tar czf /backup/db.tar.gz /data/db"

AI Agent:
  "Would you like to [1] Pick a specific worker, or [2] Let the system assign one?"

User:
  "2. Assign automatically"
```

### Step 1.3: AI Agent Prepares Task Metadata

```python
# AI Agent generates:
task_id = "backup-db-20250504-162830"
task_details = {
    "task_type": "SCRIPT",
    "path": "/scripts/backup.sh",
    "script": "#!/bin/bash\necho Backing up... && tar czf /backup/db.tar.gz /data/db"
}
assigned_by = None  # Will be filled in by Phase 6 logic
max_attempts = 3
retry_on_failure = True  # Backup script is idempotent
```

---

## PHASE 6: Intelligent Task Placement (Frontend)

### Step 2.1: Frontend Receives `/createTask` Request

```
POST /createTask
{
  "task_id": "backup-db-20250504-162830",
  "task_details": {...},
  "worker_id": null,  // User didn't pick, so AI Agent passes null
  "max_attempts": 3,
  "retry_on_failure": true
}
```

### Step 2.2: Frontend Scores Live Workers (Phase 6)

```python
# Worker Registry has live workers + metrics from Phase 6
workers_metrics = {
    "Worker-1": {
        "pending_tasks": 5,
        "latency_ms": 2.1,
        "success_rate": 0.98
    },
    "Worker-2": {
        "pending_tasks": 1,
        "latency_ms": 3.2,
        "success_rate": 0.95
    },
    "Worker-3": {
        "pending_tasks": 0,
        "latency_ms": 2.8,
        "success_rate": 1.0
    }
}

# Scoring function:
scores = {
    "Worker-1": (0.5 * -5) + (0.3 * -0.021) + (0.2 * 0.98) = -2.196,
    "Worker-2": (0.5 * -1) + (0.3 * -0.032) + (0.2 * 0.95) = -0.4096,
    "Worker-3": (0.5 * -0) + (0.3 * -0.028) + (0.2 * 1.0)   = 0.1916
}

# Best worker: Worker-3 (highest score)
chosen_worker_id = "Worker-3"
assigned_by = "intelligence"
placement_explanation = "Worker-3 chosen (pending: 0, latency: 2.8ms, success: 100%)"
```

### Step 2.3: Frontend Confirms Placement Decision

```python
# AI Agent receives:
{
    "message": "Task accepted",
    "task_id": "backup-db-20250504-162830",
    "worker_id": "Worker-3",
    "assigned_by": "intelligence",
    "placement_explanation": "Worker-3 chosen (pending: 0, latency: 2.8ms, success: 100%)"
}

# AI Agent tells user:
"✅ Task accepted and assigned to Worker-3 
   (the least loaded; it's currently free while others have work pending)"
```

---

## PHASE 3: Atomic Task Persistence

### Step 3.1: Frontend Performs Atomic 2-Write (with retries, timeouts, idempotency)

```python
# Step 1: Idempotency check
existing = HT1.get("backup-db-20250504-162830")
if existing:
    return existing  # Already submitted, return as-is

# Step 2: Write to HT1 (Task Registry) via Chord routing
with_timeout_and_retry(
    lambda: HT1.put("backup-db-20250504-162830", {
        "task_id": "backup-db-20250504-162830",
        "task_type": "SCRIPT",
        "path": "/scripts/backup.sh",
        "script": "#!/bin/bash\necho Backing up...",
        "status": "PENDING",
        "worker_id": "Worker-3",
        "assigned_by": "intelligence",
        "attempt_count": 0,
        "max_attempts": 3,
        "retry_on_failure": true,
        "recovery_history": [],
        "created_at": 1714862910.123,
        "updated_at": 1714862910.123
    }),
    timeout_ms=1500,
    max_retries=3
)

# Step 3: Write to HT2 (Worker Queue) via Chord routing
with_timeout_and_retry(
    lambda: HT2.append("Worker-3", "backup-db-20250504-162830"),
    timeout_ms=1500,
    max_retries=3
)

# Step 4: Both writes succeed
return 201, {
    "message": "Task accepted",
    "task_id": "backup-db-20250504-162830",
    "worker_id": "Worker-3",
    "assigned_by": "intelligence"
}
```

### Step 3.2: Data Now in DHT (Persisted via Chord)

```
Chord Ring (8-bit, 256 nodes):

HT1 (Task Registry):
  key: result:backup-db-20250504-162830
  hash: sha1(...) -> position 42
  primary: Node-42
  replicas: Node-43, Node-44
  value: {task_id, status: PENDING, worker_id: Worker-3, ...}

HT2 (Worker Queue):
  key: worker:Worker-3
  hash: sha1(...) -> position 157
  primary: Node-157
  replicas: Node-158, Node-159
  value: [task-123, ..., backup-db-20250504-162830]
```

---

## PHASE 4: Worker Pickup & Execution

### Step 4.1: Worker-3's Polling Loop (every 2 seconds)

```python
# Worker-3 runs a polling loop:
while True:
    assigned_tasks = HT2.get("Worker-3")  # Chord lookup: O(log N)
    
    for task_id in assigned_tasks:
        if task_id not in self.seen_task_ids:
            self.seen_task_ids.add(task_id)
            self._execute_task(task_id)
    
    sleep(2)  # Poll every 2 seconds
```

### Step 4.2: Worker-3 Detects New Task

```python
# After 0-2 seconds, Worker-3's polling loop finds the new task:
new_tasks = ["backup-db-20250504-162830"]

# For each new task:
task_record = HT1.get("backup-db-20250504-162830")
# Returns the full ResultDetails with script content
```

### Step 4.3: Worker-3 Executes the Script

```python
# Worker-3 runs the executor:
def run_task(task_type="SCRIPT", script="...", timeout_s=60):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh") as f:
        f.write(script)
        tmppath = f.name
    
    os.chmod(tmppath, 0o755)
    
    try:
        start = time.time()
        proc = subprocess.run(["bash", tmppath], capture_output=True, timeout=60, text=True)
        
        return ExecutionResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_ms=int((time.time() - start) * 1000),
            timed_out=False
        )
    except subprocess.TimeoutExpired:
        return ExecutionResult(
            exit_code=-1,
            stdout="",
            stderr="[TIMEOUT after 60s]",
            duration_ms=60000,
            timed_out=True
        )

# Execution happens:
output: "Backing up... {tar output} ..."
exit_code: 0
duration_ms: 2341
```

### Step 4.4: Worker-3 Writes Result Back to HT1

```python
# Worker-3 updates the HT1 record:
with_timeout_and_retry(
    lambda: HT1.put("backup-db-20250504-162830", {
        "status": "SUCCESS",
        "result": {
            "exit_code": 0,
            "stdout": "Backing up... {output}",
            "stderr": "",
            "duration_ms": 2341,
            "timed_out": false
        },
        "worker_id": "Worker-3",
        "updated_at": 1714862925.464
        # ... other fields unchanged ...
    }),
    timeout_ms=1500,
    max_retries=3
)

# Worker-3 also reports completion (Phase 6 stats):
POST /workers/complete
{
    "task_id": "backup-db-20250504-162830",
    "success": true
}
# This updates worker_stats["Worker-3"].success_rate
```

---

## PHASE 5: Status Reporting & AI Agent Translation

### Step 5.1: User Asks for Status

```
User:
  "How's the backup going?"

AI Agent:
  [Internally calls GET /getStatus/backup-db-20250504-162830]
```

### Step 5.2: `/getStatus` Returns Full Record

```python
# Frontend queries HT1 via Chord lookup:
record = HT1.get("backup-db-20250504-162830")

return 200, {
    "task_id": "backup-db-20250504-162830",
    "status": "SUCCESS",
    "result": {
        "exit_code": 0,
        "stdout": "Backing up...",
        "stderr": "",
        "duration_ms": 2341,
        "timed_out": false
    },
    "worker_id": "Worker-3",
    "assigned_by": "intelligence",
    "attempt_count": 0,
    "recovery_history": []
}
```

### Step 5.3: AI Agent Translates to Natural Language

```python
# AI Agent sees status=SUCCESS, exit_code=0, and tells user:
"✅ Your backup completed successfully!
   Backup took 2.3 seconds.
   Output: 'Backing up... {partial output truncated to 500 chars}...'"
```

---

## PHASE 7: Failure Recovery (Alternative Scenario)

### Scenario Twist: Worker-1 Crashes Mid-Execution

Imagine instead the task went to Worker-1 (which was overloaded). Worker-1 crashes after 30 seconds of execution.

### Step 7.1: Recovery Loop Detects Worker Crash

```python
# Recovery loop runs every 5 seconds:
def _detect_and_recover_worker_crashes(self):
    live_workers = self.registry.live_workers()
    
    # Worker-1's heartbeat is missing for >10 seconds
    if "Worker-1" not in live_workers:
        # Find tasks assigned to Worker-1
        tasks = HT2.get("Worker-1")  # ["...", "backup-db-20250504-162830", "..."]
        
        for task_id in tasks:
            task = HT1.get(task_id)
            if task.status in ["PENDING", "RUNNING"]:
                # Task is incomplete, attempt recovery
                self._attempt_recovery(task, "worker_crash")
```

### Step 7.2: Recovery Decision Tree

```python
def choose_recovery_path(task, failure_type="worker_crash", cluster_state):
    # task.attempt_count = 0 (first failure)
    # task.max_attempts = 3 (allowed to retry)
    # task.retry_on_failure = true (idempotent)
    
    if task.attempt_count >= task.max_attempts:
        return GIVE_UP  # No, we haven't exhausted retries
    
    if failure_type == "worker_crash":
        # Worker died mid-execution
        # Safe to retry on a different worker
        if has_available_workers(cluster_state):
            return RETRY_DIFFERENT  # Yes, we have available workers
        else:
            return WAIT_AND_RETRY
    
    # Decision: RETRY_DIFFERENT
```

### Step 7.3: Recovery Execution

```python
# Pick the best available worker (Phase 6 scoring):
cluster_state = scorer.score_all_workers()
# Worker-2: score 0.5, Worker-3: score 0.8 (less loaded now)

# Choose Worker-3 (highest score)
new_worker_id = "Worker-3"

# Update HT1:
HT1.put("backup-db-20250504-162830", {
    "status": "PENDING",  # Reset to pending
    "worker_id": "Worker-3",  # New worker
    "attempt_count": 1,  # Increment
    "recovery_history": ["RETRY_DIFFERENT on Worker-3"],
    "last_failure_reason": "worker_crash",
    # ... other fields ...
})

# Re-assign in HT2:
HT2.append("Worker-3", "backup-db-20250504-162830")

# Remove from old HT2 entry (optionally; not critical for Phase 7)
# HT2.remove("Worker-1", "backup-db-20250504-162830")
```

### Step 7.4: Task Retries on Worker-3

```python
# Worker-3's polling loop now sees the task again (attempt_count=1)
# It executes again (same script)

# This time, it succeeds and writes back:
HT1.put("backup-db-20250504-162830", {
    "status": "SUCCESS",
    "result": {...},
    "attempt_count": 1,  # Still 1 (only incremented on assignment, not on completion)
    "recovery_history": ["RETRY_DIFFERENT on Worker-3"],
    # ...
})
```

### Step 7.5: User Sees Recovery in Status

```
User (after Worker-1 crash + recovery):
  "How's the backup?"

AI Agent:
  [Calls GET /getStatus/backup-db-20250504-162830]
  
Response includes:
  "status": "SUCCESS",
  "attempt_count": 1,
  "recovery_history": ["RETRY_DIFFERENT on Worker-3"],
  "last_failure_reason": "worker_crash"

AI Agent tells user:
  "✅ Your backup completed successfully after a retry.
   (Worker-1 crashed mid-execution, so we retried on Worker-3.
    Final result: success)"
```

---

## Complete Timeline (Happy Path)

```
Time  Actor                   Action                              State
────  ────────────────────    ────────────────────────────────    ──────────────
T+0   User                    "I want to run a backup"            conversing
T+2   AI Agent                Collects task details               T_id created
T+3   AI Agent                Asks worker preference              User picks auto
T+4   AI Agent                Calls /createTask                   
T+5   Frontend (Phase 6)      Scores workers, picks Worker-3      placement decided
T+6   Frontend (Phase 3)      Atomic 2-write to HT1 + HT2         data persisted
T+7   AI Agent                Returns "Task accepted"             user informed
T+8   User                    "Let me know when it's done"        
T+9   AI Agent                Calls /getStatus?wait=120           waiting...
T+10  Worker-3 (Phase 4)      Polling loop finds new task         starts execution
T+15  Worker-3 (Phase 4)      Executes backup script              running...
T+17  Worker-3 (Phase 4)      Writes result to HT1                complete
T+17  AI Agent                /getStatus returns SUCCESS          
T+18  AI Agent                Reports to user in natural language  task done
T+19  User                    Sees "✅ Backup succeeded"           satisfied
```

---

## Complete Timeline (With Failure & Recovery)

```
Time  Actor                   Action                              State
────  ────────────────────    ────────────────────────────────    ──────────────
T+0   User → AI Agent         Task submission (same as above)
T+6   Frontend                Assigns to Worker-1 (overloaded)    
T+10  Worker-1                Starts execution
T+20  (Network/GC)            Worker-1 crashes                    Worker-1 dead
T+22  Heartbeat timeout       Worker-1 removed from live registry  dead
T+25  Recovery loop           Detects missing Worker-1            failure detected
T+26  Recovery loop           Finds incomplete tasks on Worker-1
T+27  Recovery loop           Chooses RETRY_DIFFERENT             recovery decision
T+28  Recovery loop           Updates HT1, appends to Worker-3    reassigned
T+30  Worker-3               Polling loop finds task              retry begins
T+35  Worker-3               Executes backup script               running...
T+37  Worker-3               Writes result + recovery_history     complete
T+38  AI Agent               /getStatus returns SUCCESS           
T+38  AI Agent               Includes recovery_history in status
T+39  AI Agent               "✅ Backup succeeded (retried after crash)"
T+40  User                   Sees recovery explanation             satisfied
```

---

## Data Flow Diagram (Chord Ring Perspective)

```
User Request comes in:
  ↓
AI Agent (stateless)
  ↓
Frontend /createTask
  ├─ Phase 6 Scoring: score_all_workers() [in-memory, O(1)]
  │  └─ Uses: WorkerMetrics from heartbeats
  ├─ Phase 3 Atomic Write:
  │  ├─ HT1.put(result:task_id)
  │  │  ├─ Chord: hash(key) → position 42 → Node-42 (primary)
  │  │  ├─ Replicate to Node-43, Node-44
  │  │  └─ Quorum: 2/3 must ack
  │  ├─ HT2.append(worker:worker_id)
  │  │  ├─ Chord: hash(key) → position 157 → Node-157 (primary)
  │  │  ├─ Replicate to Node-158, Node-159
  │  │  └─ Quorum: 2/3 must ack
  │  └─ Both succeed → 201 OK
  │
Phase 4 Worker Pickup:
  ├─ Worker-3's polling loop:
  │  └─ HT2.get(worker:Worker-3)
  │     ├─ Chord: hash(key) → position 157 → lookup via fingers
  │     ├─ O(log N) routing to primary + replica fallback
  │     └─ Returns: [task_ids...]
  │
  ├─ For each task_id:
  │  └─ HT1.get(result:task_id)
  │     ├─ Chord: hash(key) → position 42
  │     ├─ O(log N) lookup
  │     └─ Returns: ResultDetails
  │
  ├─ Worker-3 executes locally (Phase 4)
  │
  └─ Worker-3 writes result:
     └─ HT1.put(result:task_id, updated_record)
        ├─ Chord: same position 42 → update + replicate
        └─ Quorum: 2/3 must ack
        
Phase 5 Status:
  ├─ User asks AI Agent
  └─ AI Agent calls /getStatus
     └─ Frontend:
        └─ HT1.get(result:task_id)
           ├─ Chord lookup: O(log N)
           └─ Returns: latest ResultDetails with status, result, recovery_history

Phase 7 Recovery (if needed):
  ├─ Recovery loop detects failure
  │  └─ HT1.query(status=FAILURE) [on primary nodes or scan interval]
  │  └─ HT2.get(worker:dead_worker_id) [to find affected tasks]
  │
  ├─ Recovery decision
  │  └─ Phase 6 scoring: pick best worker
  │
  └─ Recovery execution:
     ├─ HT1.put(result:task_id, attempt_count++, recovery_history++)
     │  └─ Chord: position 42 → update + replicate
     └─ HT2.append(worker:new_worker_id, task_id)
        └─ Chord: position ??? → append + replicate
```

---

## Key Invariants Maintained Throughout

| Invariant | How It's Maintained | Phases |
|-----------|-------------------|--------|
| **Atomicity** | Both HT1 + HT2 writes succeed or both fail; rollback on partial failure | Phase 3 |
| **Idempotency** | Duplicate task_ids return existing record, no re-execution | Phase 3 |
| **Durability** | k=3 replication across Chord nodes; quorum writes | Phase 1 + 3 |
| **Consistency** | HT1 status matches reality (task is assigned ↔ in HT2) | Phase 4 + 7 |
| **Availability** | Workers marked dead are removed from live registry; tasks reassigned | Phase 4 + 7 |
| **Ordering** | Task attempts are sequenced: attempt 0 → attempt 1 → attempt 2 | Phase 7 |

---

## Summary: All 7 Phases in Action

| Phase | Layer | Responsibility | In This Flow |
|-------|-------|-----------------|--------------|
| **1** | Chord Ring | O(log N) key lookup, 256-node ring, k=3 replication | Every DHT operation |
| **2** | AI Agent | Conversational UX, task collection | "I want to run a backup" → natural language |
| **3** | DHT / Atomic Writes | Persistent storage, 2-write protocol, idempotency | Task and worker records persisted safely |
| **4** | Workers | Polling, execution, result write-back | Task picked up, script runs, output captured |
| **5** | Status API | Natural-language reporting, optional polling | User asks status, AI translates result |
| **6** | Placement | Load-aware scoring (load 50%, latency 30%, reliability 20%) | Best worker chosen instead of round-robin |
| **7** | Recovery | Failure detection, intelligent recovery paths | If Worker-1 crashes, task retried on Worker-3 |

---

## Scalability Limits (As-Is)

| Component | Limit | Reason |
|-----------|-------|--------|
| Chord ring | 10,000+ nodes | O(log N) lookups; tested to millions in theory |
| Concurrent tasks | 1M+ | Distributed across nodes via DHT |
| Live workers | ~10,000 | Heartbeat overhead + in-memory registry |
| Concurrent /createTask | ~100/sec | HT2 per-key lock contention on hot workers |
| Concurrent status pollers | ~100 | Thread pool exhaustion |

(Phase 8c removes the in-memory registry bottleneck by moving it to DHT; Phase 8f uses async instead of threads.)

---

## Implemented Extensions

### ✅ Observability (Fully Implemented)
- **Prometheus** metrics endpoint on every node (`/metrics`)
- **Grafana** auto-provisioned dashboard: request throughput, hop counts, queue depths, agent strategy mix
- **Dashboard tabs**: Ring topology, Jobs, Task Registry, DHT Store, Agent Log
- One command to start: `docker compose up -d` → Prometheus @ `localhost:9090`, Grafana @ `localhost:3000`

### ✅ Fault Lab & Chaos Engineering (Fully Implemented)
- **Fault Lab** tab in the dashboard for live failure injection
- Crash individual nodes and observe self-healing recovery in ~10 seconds
- `RecoveryManager` (`chord/recovery.py`) detects dead workers/nodes and redistributes orphaned tasks
- `run_fault_tests.py` CLI for automated fault injection test suite
- Validated: 15 consecutive failures with full ring stability maintained

---

## Future Extensions

### Data Limits
- Result size cap (e.g., 1MB stdout/stderr)
- Task queue depth limit per worker
- Automatic cleanup of old completed tasks

### Persistence
- Move worker registry from in-memory to DHT
- Move worker stats from in-memory to DHT
- Enable worker restarts without losing metrics

### Concurrency
- Multiple tasks executing per worker in parallel
- Worker thread pool instead of sequential execution
- Load-balancing within a worker

### Security
- User isolation: tasks tagged with user_id
- Sandboxing: containers or seccomp for worker execution
- Authentication: API keys for agents

