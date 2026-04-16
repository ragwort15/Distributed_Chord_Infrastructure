# Distributed Coordination Layer — Chord DHT

A fully decentralized coordination layer for edge and IoT devices built on the **Chord Distributed Hash Table**. No central server, no cloud dependency. Nodes self-organize into a consistent hash ring, route jobs to responsible peers, and recover from failures automatically.

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
- Jobs are stored and routed using **consistent hashing** (SHA-1)
- Any node can accept a job and route it to the correct peer in **O(log N) hops**
- When a node fails, the ring **self-heals** automatically
- An AI agent layer optimizes job placement, replication, and failure recovery on top of the DHT

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Chord DHT Ring                              │
│                                                                 │
│         Node 10 ──────────── Node 80                           │
│        /    ↑                    ↓    \                         │
│       /     │  finger table      │     \                        │
│    Node 150 ◀───────────────────────── Node 80                 │
│       \                                /                        │
│        └──────────── Node 10 ─────────┘                        │
└─────────────────────────────────────────────────────────────────┘
         ↑                   ↑                    ↑
   Chord Core         Job Storage           AI Agents
   finger tables      task put/get/route    placement / recovery
   join / leave       MCP interface         replication
   stabilize          service registry      Prometheus metrics
                               ↑
                         Frontend
                         ring visualizer
                         demo + evaluation
```

### Request flow — job submission

```
Client submits job
      │
      ▼
Any Chord node (entry point)
      │  SHA-1(job_id) → key_id
      │  find_successor(key_id)
      ▼
Closest finger table entry
      │  hop...
      ▼
Responsible node
      │  store job locally
      ▼
AI agent reads node states → decides execution target
      │
      ▼
Job dispatched to selected edge node
```

---

## Components

### 1. Chord Core
The DHT backbone. Every other component builds on top of this.

- **Consistent hashing** — SHA-1 maps any string key into a 2^160 ring
- **Finger table** — M shortcuts per node enabling O(log N) lookup
- **Stabilization protocol** — background process keeps successor/predecessor pointers correct as nodes join and leave
- **Failure detection** — heartbeat-based predecessor liveness check
- **Graceful leave** — data handoff to successor before departure

### 2. Job Storage & Service Layer
Sits on top of the Chord ring to provide application-level operations.

- Task/job schema definition and DHT storage
- Routed put/get/delete — any node can accept a request and route it correctly
- Service registry — nodes register capabilities into the DHT
- REST/MCP interface for other components

### 3. AI Agent System
Intelligent decision-making layer that reads live node state from the DHT.

- **Task placement agent** — selects the optimal node based on load, latency, availability
- **Adaptive replication agent** — decides replica count and placement per job priority
- **Failure recovery agent** — chooses recovery strategy when a node dies mid-execution
- Prometheus metrics + structured JSON logging

### 4. Demo, Frontend & Evaluation
- Live **Chord ring visualizer** — nodes on a circle, routing hops animated
- Node join/leave animation showing ring rebalancing
- Finger table overlay (toggleable)
- Job submission dashboard
- Local IoT/edge node simulator
- Performance evaluation and final report

---

## Project Structure

```
Distributed_Chord_Infrastructure/
│
├── chord/                     # Chord DHT core
│   ├── __init__.py
│   ├── node.py                # Ring logic: finger table, join, stabilize, leave
│   ├── transport.py           # HTTP RPC layer for inter-node calls
│   └── server.py              # Flask server: Chord endpoints + routed data API
│
├── storage/                   # Job storage & service layer
│   ├── __init__.py
│   └── ...
│
├── agents/                    # AI agents + observability
│   ├── __init__.py
│   └── ...
│
├── ui/                        # Ring visualizer frontend
│   └── ...
│
├── tests/                     # Unit + integration tests
│   ├── __init__.py
│   └── test_chord.py          # 47 tests for Chord core
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
| AI agents | Rule-based / LLM policy |
| Metrics | Prometheus |
| Logging | Structured JSON (structlog) |
| Frontend | HTML + D3.js (ring visualizer) |
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
| `--log` | Log level: DEBUG / INFO / WARNING / ERROR | `INFO` |

---

## API Reference

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

### Store and retrieve a job (routed automatically)

```bash
# Store a job — routes to the responsible node automatically
curl -X POST http://127.0.0.1:5001/put/job:1 \
  -H "Content-Type: application/json" \
  -d '{"name": "process_sensor_data", "status": "pending", "priority": "high"}'

# Expected: {"ok": true, "key": "job:1", "stored_at": 80}

# Retrieve from ANY node — routing handles it
curl http://127.0.0.1:5003/get/job:1

# Expected: {"name": "process_sensor_data", "status": "pending", "priority": "high"}
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
# Run all tests with verbose output
pytest tests/test_chord.py -v

# Run a specific test class
pytest tests/test_chord.py::TestFindSuccessorTwoNodes -v

# Run with coverage (if pytest-cov installed)
pytest tests/test_chord.py --cov=chord --cov-report=term-missing
```

Expected output:
```
tests/test_chord.py::TestHashing::test_sha1_id_returns_int         PASSED
tests/test_chord.py::TestHashing::test_sha1_id_in_range            PASSED
tests/test_chord.py::TestInRange::test_wraparound_inside           PASSED
...
======= 47 passed in 0.25s =======
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

### Routed job storage
```json
{"ok": true, "key": "job:1", "stored_at": 80}
```

### Job retrieval
```json
{"name": "process_sensor_data", "status": "pending", "priority": "high"}
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