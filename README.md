# Distributed Coordination Layer — Chord DHT

A decentralized coordination layer for edge and IoT workloads built on the **Chord Distributed Hash Table**. Nodes self-organize into a consistent-hash ring, expose task APIs over **REST + gRPC**, and persist replicated task metadata without a central coordinator.

**Course:** CMPE 273 — Distributed Systems  
**Option:** A — Distributed Job Execution Platform

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Components](#components)
- [Project Structure](#project-structure)
- [Tech Stack](#tech-stack)
- [Quick Start](#quick-start)
- [Running a Multi-Node Ring](#running-a-multi-node-ring)
- [API Reference](#api-reference)
- [Testing with curl](#testing-with-curl)
- [Run Tests](#run-tests)
- [Expected Outputs](#expected-outputs)
- [How Chord Works](#how-chord-works)

---

## Overview

Traditional job execution platforms rely on a central coordinator to route and schedule tasks. This project eliminates that single point of failure by using **Chord**, a peer-to-peer protocol where:

- Every node is equal — no master, no leader
- Task metadata is stored and routed using **consistent hashing** (SHA-1)
- Any node can accept a task request and route it to the correct peer in **O(log N) hops**
- Task objects follow a defined schema and are persisted in DHT key-value format (`task:{task_id}`)
- Replication writes are performed to the **k-nearest successors** for resilience
- The ring self-heals via stabilize/fix_fingers/check_predecessor background maintenance

---

## Architecture

```
Client / Scheduler / Edge Control Plane
                │
                │ REST (`/tasks/...`) or gRPC (`TaskService`)
                ▼
        Any Chord Node (entry point)
                │
                │ hash(`task:{task_id}`) -> key_id
                │ find_successor(key_id)
                ▼
         Responsible Primary Node
                │
                ├── store task metadata (primary)
                └── replicate to k-1 successor nodes
                      (`InternalReplicationService` / internal REST)

      Chord Ring Maintenance (all nodes)
      stabilize() • fix_fingers() • check_predecessor()
```

### Request flow — job submission

```
Client submits task
      │
      ▼
Any Chord node (entry point)
      │  validate schema
      │  build key: task:{task_id}
      │  SHA-1(task_key) -> key_id
      │  find_successor(key_id) on ring
      ▼
Responsible node (primary)
      │  persist task record
      │  replicate to k-nearest successors
      ▼
Ack with replication status (COMPLETE / DEGRADED)
```

---

## Components

### 1. Chord Core
The DHT backbone. Every other component builds on top of this.

- **Consistent hashing** — SHA-1 maps keys into ring identifiers (8-bit test ring by default)
- **Finger table** — M shortcuts per node enabling O(log N) lookup
- **Stabilization protocol** — background process keeps successor/predecessor pointers correct as nodes join and leave
- **Failure detection** — heartbeat-based predecessor liveness check
- **Graceful leave** — data handoff to successor before departure

### 2. Task Service & Storage Layer
Sits on top of the Chord ring to provide application-level operations.

- Task schema definition (`storage/schema.py`) and DHT persistence format
- Register / deregister / get / query task APIs (`storage/task_service.py`)
- Replication to `k` successors (`storage/replication.py`)
- REST endpoints in `chord/server.py` and gRPC services in `api/grpc_server.py`

### 3. External Interfaces
The system is consumable by schedulers, workers, or orchestration layers through two API styles.

- **REST API** — task registration/deregistration/lookup + ring/node query endpoints
- **gRPC API** — typed `TaskService` and `InternalReplicationService` contracts
- **Proto contract** — `api/task_service.proto`
- **Detailed spec** — `docs/api_spec.md`

### 4. Testing & Validation
- Chord ring behavior tests (`tests/test_chord.py`)
- Task service tests (`tests/test_task_service.py`)
- Replication tests (`tests/test_replication.py`)
- gRPC service mapping tests (`tests/test_grpc_service.py`)

---

## Project Structure

```
Distributed_Chord_Infrastructure/
│
├── api/                       # gRPC contracts + generated stubs + gRPC server
│   ├── task_service.proto
│   ├── task_service_pb2.py
│   ├── task_service_pb2_grpc.py
│   └── grpc_server.py
│
├── chord/                     # Chord DHT core
│   ├── __init__.py
│   ├── node.py                # Ring logic: finger table, join, stabilize, leave
│   ├── transport.py           # HTTP transport and inter-node RPC helpers
│   └── server.py              # Flask server: Chord endpoints + task REST APIs
│
├── storage/                   # Task schema, service layer, replication logic
│   ├── __init__.py
│   ├── schema.py
│   ├── task_service.py
│   └── replication.py
│
├── docs/
│   └── api_spec.md            # REST + gRPC API details
│
├── tests/                     # Unit + integration tests
│   ├── __init__.py
│   ├── test_chord.py
│   ├── test_task_service.py
│   ├── test_replication.py
│   └── test_grpc_service.py
│
├── run_node.py                # CLI entrypoint to start a Chord node
├── requirements.txt
└── README.md
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12+ |
| Chord transport | HTTP over TCP (Flask + requests) |
| gRPC | grpcio + grpcio-tools |
| API style | REST + gRPC |
| Testing | pytest |

---

## Quick Start

### Prerequisites

- Python 3.12+
- pip

### 1. Clone the repository

```bash
git clone https://github.com/ragwort15/Distributed_Chord_Infrastructure.git
cd Distributed_Chord_Infrastructure
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Start a single bootstrap node

```bash
python run_node.py --port 5001 --id 10
```

Expected output:
```
INFO chord.node: [Node 10] Initialized at 127.0.0.1:5001
INFO chord.node: [Node 10] Bootstrapped as first node
INFO root: Starting Chord node 10 on 127.0.0.1:5001
 * Running on http://127.0.0.1:5001
```

---

## Running a Multi-Node Ring

Open **3 separate terminals** inside the project folder.

**Terminal 1 — Bootstrap node:**
```bash
python run_node.py --port 5001 --id 10
```

**Terminal 2 — Join the ring:**
```bash
python run_node.py --port 5002 --id 80 --join 127.0.0.1:5001
```

**Terminal 3 — Join the ring:**
```bash
python run_node.py --port 5003 --id 150 --join 127.0.0.1:5001
```

After a few seconds of stabilization, the ring forms:
```
Node 10  →  successor: Node 80   │  predecessor: Node 150
Node 80  →  successor: Node 150  │  predecessor: Node 10
Node 150 →  successor: Node 10   │  predecessor: Node 80
```

### CLI options

| Flag | Description | Default |
|---|---|---|
| `--host` | Bind host | `127.0.0.1` |
| `--port` | Bind port (required) | — |
| `--join` | Address of existing node to join (`host:port`) | None (bootstrap) |
| `--id` | Override node ID (useful for testing) | SHA-1 of `host:port` |
| `--interval` | Stabilization interval in seconds | `2.0` |
| `--grpc-port` | Enable gRPC TaskService on this port | Disabled |
| `--log` | Log level: DEBUG / INFO / WARNING / ERROR | `INFO` |

---

## API Reference

Detailed request/response payloads are documented in `docs/api_spec.md`.

### Chord Internal Endpoints (node-to-node RPC)

| Method | Path | Description |
|---|---|---|
| `GET` | `/chord/ping` | Liveness probe — returns node ID and address |
| `GET` | `/chord/state` | Full node state: ID, successor, predecessor, finger table, stored keys |
| `GET` | `/chord/find_successor?id=<int>` | Find the successor node responsible for a key ID |
| `GET` | `/chord/predecessor` | Return this node's current predecessor |
| `POST` | `/chord/notify` | Notify this node of a potential new predecessor |
| `POST` | `/chord/update_predecessor` | Force-set predecessor pointer (used on graceful leave) |
| `POST` | `/chord/update_successor` | Force-set successor pointer (used on graceful leave) |
| `POST` | `/chord/bulk_put` | Accept a batch of key/value pairs (used on node leave handoff) |

### Local Data Store Endpoints (direct, no routing)

| Method | Path | Description |
|---|---|---|
| `POST` | `/data/<key>` | Store a value directly on this node |
| `GET` | `/data/<key>` | Retrieve a value from this node's local store |
| `DELETE` | `/data/<key>` | Delete a key from this node's local store |

### Routed Data API (auto-routes to responsible node)

| Method | Path | Description |
|---|---|---|
| `POST` | `/put/<key>` | Hash the key, route to responsible node, store there |
| `GET` | `/get/<key>` | Hash the key, route to responsible node, retrieve from there |

### Task Service REST Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/tasks` | Register a task with schema validation + replication |
| `GET` | `/tasks/<task_id>` | Retrieve task by ID (with replica fallback) |
| `DELETE` | `/tasks/<task_id>?hard=true|false` | Soft deregister (tombstone) or hard delete |
| `GET` | `/tasks?job_id=&status=&include_deleted=&limit=` | Query local tasks on node |
| `GET` | `/ring/lookup/<task_id>` | Resolve primary owner + replica chain |
| `GET` | `/nodes/self` | Return local node state |
| `GET` | `/nodes/query?address=<host:port>` | Query remote node state |

### Internal Task Replication REST Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/internal/tasks/replica/<task_key>` | Store replica on local node |
| `GET` | `/internal/tasks/replica/<task_key>` | Retrieve replica from local node |
| `DELETE` | `/internal/tasks/replica/<task_key>` | Delete replica from local node |

### gRPC Services

- `TaskService` (`RegisterTask`, `GetTask`, `DeregisterTask`, `QueryTasks`, `LookupTaskOwner`, `GetNodeInfo`)
- `InternalReplicationService` (`ReplicateTask`)
- Contract file: `api/task_service.proto`

---

## Testing with curl

Start a 3-node ring first (see [Running a Multi-Node Ring](#running-a-multi-node-ring)), then open a 4th terminal.

### Health checks

```bash
# Ping all nodes
curl http://127.0.0.1:5001/chord/ping
curl http://127.0.0.1:5002/chord/ping
curl http://127.0.0.1:5003/chord/ping
```

### Inspect node state

```bash
# Full state of node 10 (successor, predecessor, finger table)
curl http://127.0.0.1:5001/chord/state | python -m json.tool
```

### Test routing — find_successor

```bash
# Key 50 → should resolve to Node 80 (first node at or after 50)
curl "http://127.0.0.1:5001/chord/find_successor?id=50"

# Key 100 → should resolve to Node 150
curl "http://127.0.0.1:5001/chord/find_successor?id=100"

# Key 200 → should wrap around to Node 10
curl "http://127.0.0.1:5001/chord/find_successor?id=200"
```

### Register, retrieve, and remove a task

```bash
# 1) Register a task (can hit any node)
curl -X POST http://127.0.0.1:5001/tasks \
      -H "Content-Type: application/json" \
      -d '{
            "task_id": "task-curl-1",
            "job_id": "job-curl-1",
            "type": "process.sensor",
            "payload": {"sensor_id": "A12", "window": "5m"},
            "priority": 5
      }'

# Expected (shape):
# {"ok": true, "data": {"task": {...}, "task_key": "task:task-curl-1", "storage": {...}}}

# 2) Retrieve the task from another node (replica fallback supported)
curl http://127.0.0.1:5003/tasks/task-curl-1

# Expected (shape):
# {"ok": true, "data": {"task": {"task_id": "task-curl-1", "job_id": "job-curl-1", ...}}}

# 3) Query tasks by job_id/status on local node store
curl "http://127.0.0.1:5002/tasks?job_id=job-curl-1&status=REGISTERED&limit=10"

# 4) Soft-deregister the task (writes DELETED tombstone + replicates)
curl -X DELETE "http://127.0.0.1:5001/tasks/task-curl-1?hard=false"

# Optional hard delete:
# curl -X DELETE "http://127.0.0.1:5001/tasks/task-curl-1?hard=true"
```

### Test failure detection

```bash
# 1. Kill node 80 (Ctrl+C in Terminal 2)
# 2. Wait 5-6 seconds
# 3. Check node 10's state — predecessor should be cleared

curl http://127.0.0.1:5001/chord/state | python -m json.tool
# Expected: "predecessor": null  (dead node cleared)
```

### Notify manually

```bash
curl -X POST http://127.0.0.1:5001/chord/notify \
  -H "Content-Type: application/json" \
  -d '{"id": 200, "address": "127.0.0.1:5999"}'
```

---

## Run Tests

```bash
# Run all tests
pytest tests -v

# Run only task-layer and grpc tests
pytest tests/test_task_service.py tests/test_replication.py tests/test_grpc_service.py -v

# Run with coverage (if pytest-cov installed)
pytest tests --cov=chord --cov=storage --cov=api --cov-report=term-missing
```

Expected output:
```
tests/test_chord.py::TestHashing::test_sha1_id_returns_int         PASSED
tests/test_task_service.py::test_register_and_get_task_local_store PASSED
tests/test_replication.py::test_successor_chain_and_replication... PASSED
tests/test_grpc_service.py::test_grpc_register_and_get_task        PASSED
...
======= 58 passed =======
```

### Test coverage by area

| Test Class | What it covers |
|---|---|
| `TestHashing` | SHA-1 key derivation, ring bounds |
| `TestInRange` | Circular interval arithmetic, wraparound |
| `TestNodeInit` | Finger table initialization, default state |
| `TestBootstrap` | Single-node ring formation |
| `TestJoin` | Peer join via known node |
| `TestFindSuccessorSingleNode` | 1-node ring routing |
| `TestFindSuccessorTwoNodes` | 2-node routing, boundary cases |
| `TestStabilize` | Successor update, notify call |
| `TestNotify` | Predecessor acceptance and rejection |
| `TestCheckPredecessor` | Failure detection, pointer clearing |
| `TestDataStore` | put / get / delete / bulk_put |
| `TestLeave` | Data handoff, pointer updates on departure |
| `TestState` | State introspection output |

---

## Expected Outputs

### Single node bootstrap
```json
{
  "node_id": 10,
  "address": "127.0.0.1:5001",
  "successor": {"id": 10, "address": "127.0.0.1:5001"},
  "predecessor": null,
  "fingers": [...],
  "data_keys": []
}
```

### After 3-node ring stabilizes
```json
{
  "node_id": 10,
  "address": "127.0.0.1:5001",
  "successor": {"id": 80, "address": "127.0.0.1:5002"},
  "predecessor": {"id": 150, "address": "127.0.0.1:5003"},
  "data_keys": []
}
```

### Task registration (`POST /tasks`)
```json
{
      "ok": true,
      "data": {
            "task": {
                  "task_id": "task-curl-1",
                  "job_id": "job-curl-1",
                  "type": "process.sensor",
                  "status": "REGISTERED"
            },
            "task_key": "task:task-curl-1",
            "storage": {
                  "replication_state": "COMPLETE"
            }
      }
}
```

### Task retrieval (`GET /tasks/task-curl-1`)
```json
{
      "ok": true,
      "data": {
            "task": {
                  "task_id": "task-curl-1",
                  "job_id": "job-curl-1",
                  "type": "process.sensor",
                  "status": "REGISTERED"
            }
      }
}
```

### Task soft-deregister (`DELETE /tasks/task-curl-1?hard=false`)
```json
{
      "ok": true,
      "data": {
            "task": {
                  "task_id": "task-curl-1",
                  "status": "DELETED"
            },
            "hard_delete": false,
            "storage": {
                  "replication_state": "COMPLETE"
            }
      }
}
```

---

## How Chord Works

### The ring

Every node and every piece of data gets a number (0–255 in test mode, 0–2^160 in production) using SHA-1 hashing. Nodes sit at positions on a circular number line. Data lives at the **first node whose ID is ≥ the data's number**.

```
        0
   200     10        ← Node IDs
      \   /
  150  ring  80      ← Data with ID 50 lives at Node 80
        |             (first node at or after 50)
```

### Finger table (why lookup is O(log N))

Instead of only knowing your immediate neighbor, each node keeps M shortcuts pointing progressively further around the ring. Finding any key takes at most log₂(N) hops.

### Stabilization

Every 2 seconds each node runs:
1. `stabilize()` — verify successor, update if a better one exists, notify successor of our presence
2. `fix_fingers()` — refresh one finger table entry
3. `check_predecessor()` — ping predecessor, clear if unreachable

This keeps the ring correct as nodes join and leave without any central coordinator.

---