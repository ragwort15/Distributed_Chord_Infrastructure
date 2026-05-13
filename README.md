**Team Members:**

1. Anupama Singh (SJSU ID: 019142305)
2. Neeraja Abhinav Buch (SJSU ID: 018178238)
3. Vi Thi Tuong Nguyen (SJSU ID: 013832546)
4. Sreya Somisetty (SJSU ID: 019126419)
**Course:** CMPE 273 — Distributed Systems  

# Distributed Coordination Layer — Chord DHT

A decentralized coordination layer for edge and IoT workloads built on the **Chord Distributed Hash Table**. Nodes self-organize into a consistent-hash ring, expose task and job APIs over **REST + gRPC**, execute jobs via an AI-assisted placement agent, and persist replicated task metadata without a central coordinator.

**🔗 Presentation Webpage:** [View Demo Presentation](http://ec2-52-41-200-180.us-west-2.compute.amazonaws.com:5001/presentation)

**📹 Demo Video:** [Watch Demo](https://drive.google.com/file/d/1cJXFB4xjgzt4SEyrVpzH4pbrj-rRCZWq/view?usp=sharing)

**📄 Paper:** [Read Full Paper](https://github.com/ragwort15/Distributed_Chord_Infrastructure/tree/main/paper)


## The Problem
The cloud fails, and relying on centralized coordination at the edge is risky and expensive. A single point of failure can bring down an entire edge fleet—drones, sensors, and devices—leading to an average recovery time of 72 minutes and costing up to $50K per hour in downtime for an industrial IoT scheduler.

## The Solution & Real-World Impact
We built a **Peer-to-Peer Coordination Layer** using Chord DHT to eliminate the central coordinator. This is built for the world where the cloud isn't enough, providing measurably better peer-to-peer coordination for real-world edge deployments across several domains:

* **🚒 Emergency Response:** Drone fleets coordinate search zones and sensor data with zero cloud dependency. The swarm remains resilient even when every cell tower is down.
* **🏭 Industrial IoT:** Factory floor sensors and actuators route jobs peer-to-peer. The failure of one central sensor no longer halts the entire production line.
* **🚗 Autonomous Vehicles:** Edge nodes in a vehicle fleet share routing and task data directly. This enables sub-100ms coordination without requiring a roundtrip to a distant cloud server.



## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Components](#components)
- [Project Structure](#project-structure)
- [Tech Stack](#tech-stack)
- [Quick Start](#quick-start)
  - [Option A — Docker Compose (recommended)](#option-a--docker-compose-recommended)
  - [Option B — Local Python](#option-b--local-python)
- [Running a Multi-Node Ring](#running-a-multi-node-ring)
- [Control Center Dashboard](#control-center-dashboard)
- [AI Task Chat](#ai-task-chat)
- [Observability Stack](#observability-stack)
- [Simulator & Benchmarking](#simulator--benchmarking)
- [Utility Scripts](#utility-scripts)
- [AWS Demo Deployment](#aws-demo-deployment)
- [Retry & Fallback Policy](#retry--fallback-policy)
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

### 1. Chord Core (`chord/node.py`)
The DHT backbone. Every other component builds on top of this.

- **Consistent hashing** — SHA-1 maps keys into ring identifiers (8-bit test ring by default)
- **Finger table** — M shortcuts per node enabling O(log N) lookup
- **Stabilization protocol** — background process keeps successor/predecessor pointers correct as nodes join and leave
- **Failure detection** — heartbeat-based predecessor liveness check
- **Graceful leave** — data handoff to successor before departure

### 2. Transport Layer (`chord/transport.py`)
All inter-node HTTP calls go through a single wrapper — `ChordNode` logic stays decoupled from networking.

- Unified `_request()` covers GET, POST, DELETE, PATCH
- **Exponential back-off retry**: 3 attempts, delays 0.1 s → 0.2 s → 0.4 s

### 3. Task Service & Storage Layer (`storage/`)
Sits on top of the Chord ring for application-level task operations.

- **`schema.py`** — task record schema, validation, full lifecycle (REGISTERED → QUEUED → RUNNING → COMPLETED / FAILED / CANCELLED / DELETED), `update_task_record()` for partial patches
- **`task_service.py`** — register / get / update / deregister / query tasks; primary-write retry with re-resolution on stale routing; replica fallback on primary failure
- **`replication.py`** — `ReplicationManager`: successor chain walk, write/delete to k replicas with per-replica retry and quorum check (`ceil(k/2)`)

### 4. Agent & Orchestration (`chord/agent.py`, `chord/agent_loop.py`)
AI-powered job placement and continuous ring monitoring.

- **`OrchestratorAgent`** — uses Claude LLM to select the best node for job placement; falls back to heuristic (least-loaded node) on any LLM exception
- **`AgentLoop`** — daemon thread that walks the full ring every 5 s, collects metrics from all reachable nodes, skips dead nodes via finger-table fallback, and logs structured decisions to `agent_decisions.jsonl`

### 5. Worker & Job Execution (`chord/worker.py`, `chord/job.py`)
Local job execution with retry.

- **`WorkerThread`** — scans local DHT for `PENDING` jobs, atomically claims them, executes in a thread pool
- **Job retry** — re-queues on failure up to `MAX_JOB_RETRIES = 3`; timed-out jobs (`JOB_TIMEOUT = 120 s`) follow the same budget
- **Job types**: `echo`, `sleep`, `compute`

### 6. Recovery Manager (`chord/recovery.py`)
Tracks task failures and coordinates recovery across the distributed ring.

- **Event emission** — logs recovery events with timestamp, task ID, worker ID, and failure reason
- **Event types**: `retry` (green), `wait_retry` (yellow), `give_up` (red), `worker_crash` (red)
- **Event retrieval** — `/api/recovery/events` endpoint for dashboard and monitoring
- **Automatic retry** — failed tasks are re-queued to alternative workers up to max retries
- **Dashboard integration** — Observability tab displays real-time recovery events with filtering and stats

### 7. Metrics (`chord/metrics_registry.py`)
Prometheus-compatible counters and gauges scraped by Grafana.

- `FILE_REQUESTS`, `FILE_REQUEST_HOPS`, `FILE_REQUEST_DURATION`
- `QUEUE_DEPTH`, `RING_SIZE`, `DATA_KEYS`
- `STABILIZE_RUNS`, `FINGER_FIX_RUNS`, `PREDECESSOR_FAILURES`, `JOBS_TOTAL`

### 7.5. Result Service & Worker Management (`storage/result_service.py`, `storage/worker_assignment.py`, `chord/worker_registry.py`)
Tracks task execution results and manages worker lifecycle across the distributed ring.

- **`ResultService`** — stores task results in DHT with replication; tracks completion status, errors, and timing
- **`WorkerAssignmentService`** — maintains per-node worker process pools; tracks worker health and assigns pending jobs
- **`WorkerRegistry`** — global registry of active workers across the ring; detects worker crashes via heartbeat; supports graceful worker shutdown
- **Worker Heartbeat** — `/workers/heartbeat` endpoint for workers to report liveness; detected via periodic checks at `/workers/live`

### 8. Control Center Dashboard (`chord/static/index.html`)
A single-file browser UI with 8 tabs served by each node's Flask server.

| Tab | What it shows |
|---|---|
| Ring Topology | Live SVG ring; finger table arcs; **Add Node** (auto-spawns via `sys.executable`), Remove / Crash / Leave controls |
| Analytics | 4 stat cards + Chart.js charts: request throughput, hop distribution, node queue depth, agent strategy mix |
| Observability | Embedded Grafana iframe (requires Prometheus + Grafana running) |
| All Jobs | Filterable job list with status dots, copy-to-clipboard Job ID buttons |
| Task Registry | Register / get / update / deregister tasks; **Reset** button to restore form defaults; ring owner lookup |
| File Requests | DHT file routing log with hop counts |
| DHT Store | Raw key-value GET / PUT / DELETE; local key browser; ring key-owner lookup |
| Agent Log | Structured agent decision log with strategy breakdown |

The sidebar has an **"Assistant"** section and a pulsing floating button (bottom-right) that both open the AI Task Chat in a new tab.

### 9. AI Task Chat (`chord/static/chat.html`)
Conversational interface served at `/chat` on every node. Submit tasks and query ring state in natural language.

### 10. External gRPC Interface (`api/`)
- `TaskService` — `RegisterTask`, `GetTask`, `DeregisterTask`, `QueryTasks`, `LookupTaskOwner`, `GetNodeInfo`
- `InternalReplicationService` — `ReplicateTask`
- Contract: `api/task_service.proto`

### 11. Simulator (`simulator/`)
In-process virtual-node simulator for benchmarking and fault injection — no real HTTP servers required.

- **`virtual_node.py`** — lightweight Chord node stub
- **`benchmark.py`** — strategy comparison across LLM / heuristic / fallback agents
- **`fault_injection.py`** — node crash/partition scenarios
- **`metrics.py`** — latency, hop count, throughput collectors
- Results captured in `simulator/results.md`

### 12. Testing & Validation (`tests/`)
- `test_chord.py` — ring behavior (hashing, join, stabilize, leave)
- `test_task_service.py` — task CRUD + replica fallback
- `test_replication.py` — quorum write/delete
- `test_grpc_service.py` — gRPC service mapping

---

## Project Structure

```text
Distributed_Chord_Infrastructure/
│
├── api/                         # gRPC contracts + generated stubs + gRPC server
├── chord/                       # Chord DHT core + application layer
├── deploy/                      # Deployment scripts (AWS EC2 demo)
├── docs/                        # API specifications and documentation
├── observability/               # Prometheus + Grafana stack
├── paper/                       # Project paper and related documents
├── simulator/                   # In-process virtual-node simulator (no real HTTP)
├── storage/                     # Task schema, service layer, replication logic
├── tests/                       # Unit + integration tests (pytest)
│
├── AGENT_POLICY.md              # Policy definitions for the AI agent
├── DEPLOYMENT_GUIDE.md          # Step-by-step AWS EC2 demo deployment guide
├── Dockerfile                   # Production image (python:3.11-slim, multi-stage)
├── Dockerfile.demo              # Demo image (python:3.11-slim, no dev deps)
├── QUICKSTART.md                # Quick start guide
├── README.md                    # Main project documentation
├── docker-compose.yml           # Full local stack: 3 nodes + Prometheus + Grafana
├── end_to_end_flow_pipeline.md  # Pipeline documentation
├── entrypoint.sh                # Docker entrypoint script
├── requirements.txt             # Python dependencies
│
├── run_benchmark.py             # CLI: strategy comparison benchmarks
├── run_demo.py                  # CLI: end-to-end simulator demo
├── run_demo_tasks.py            # CLI: automated demo task submissions
├── run_fault_tests.py           # CLI: fault injection test suite
├── run_node.py                  # CLI: start a single Chord node
├── serve_presentation.py        # CLI: serves the presentation locally
└── submit_job.py                # CLI: submit jobs to a running ring
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| Chord transport | HTTP over TCP (Flask + requests) |
| gRPC | grpcio + grpcio-tools |
| API style | REST + gRPC |
| Dashboard UI | Vanilla JS + Chart.js 4.4.2 (single HTML file) |
| AI placement | Anthropic Claude API (with heuristic fallback) |
| Metrics | Prometheus + Grafana |
| Containerisation | Docker + Docker Compose |
| Cloud deployment | AWS EC2 (demo) · EKS (production Terraform) · Helm |
| Testing | pytest |

---

## Quick Start

### Option A — Docker Compose (recommended)

Spins up 3 Chord nodes + Prometheus + Grafana with a single command. No Python setup needed.

**Prerequisites:** Docker Desktop (or Docker Engine + Docker Compose v2)

```bash
git clone https://github.com/ragwort15/Distributed_Chord_Infrastructure.git
cd Distributed_Chord_Infrastructure

docker compose up --build
```

| Service | URL |
|---|---|
| Chord Node 1 | http://localhost:5001 |
| Chord Node 2 | http://localhost:5002 |
| Chord Node 3 | http://localhost:5003 |
| AI Task Chat | http://localhost:5001/chat |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 (admin / admin) |

To stop and clean up:
```bash
docker compose down -v
```

---

### Option B — Local Python

**Prerequisites:** Python 3.11+, pip

```bash
git clone https://github.com/ragwort15/Distributed_Chord_Infrastructure.git
cd Distributed_Chord_Infrastructure
pip install -r requirements.txt
```

Start a single bootstrap node:

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

Open **http://127.0.0.1:5001** — the Control Center dashboard loads automatically.

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

Open **http://127.0.0.1:5001** to see all three nodes on the live ring diagram.

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

### Control Center Dashboard tabs

Each node serves the dashboard at its root URL (e.g. `http://127.0.0.1:5001`).

### Control Center Dashboard tabs

Each node serves the dashboard at its root URL (e.g. `http://127.0.0.1:5001`).

| Tab | What it shows |
|---|---|
| **Ring Topology** | Live SVG ring with auto-refresh every 2 sec; nodes coloured by position; finger table shortcuts as arcs; controls: **Add Node**, **Remove / Crash / Leave** |
| **Observability** | Real-time system metrics: throughput, worker latency, task status distribution, system health, replication status, and **Recovery Events & Timeline** with filtering |
| **Workloads** | Task registration, demo launchers (10/25/100 tasks), **Inject 15 Failures** button, kill-worker test, and HT2 queue view |
| **Fault Lab** | Failure injection and recovery testing tools |
| **Tasks (HT1)** | All tasks on this node — queryable by ID/status with live filtering; shows task lifecycle (PENDING/RUNNING/COMPLETED/FAILED) |
| **DHT Store** | Raw key-value store — GET/PUT/DELETE; local keys browser; ring key-owner lookup |
| **Agent Log** | Structured decisions log from the AI placement agent (LLM vs heuristic strategy, node selection reasoning) |
| **Recovery** | Recovery event viewer with detailed timeline and filtering by event type (retry / wait_retry / give_up / worker_crash) |

---

## Recovery System

The recovery manager (`chord/recovery.py`) tracks task failures and coordinates recovery attempts across the distributed ring.

### Recovery Event Types

| Type | Color | When triggered |
|---|---|---|
| `retry` | Green | Task failed but will be retried on another worker |
| `wait_retry` | Yellow | Task failed, waiting for retry opportunity |
| `give_up` | Red | Task exceeded max retries, marked as failed |
| `worker_crash` | Red | Worker process terminated unexpectedly |

### How it works

1. **Task Failure Detection** — When a worker fails to execute a task (or crashes), the result service records the failure
2. **Recovery Event Emission** — RecoveryManager logs the event with timestamp, task ID, worker ID, and failure reason
3. **Dashboard Display** — Observability tab retrieves and displays events with filtering
4. **Retry Logic** — Failed tasks are automatically re-queued to another worker (up to `MAX_JOB_RETRIES = 3`)

### Failure Injection

Use the **💥 Inject 15 Failures** button in the Workloads tab to trigger a stress test:
- Submits 15 tasks that fail immediately
- Observe recovery events populate in real-time in the Observability tab
- Watch workers retry failing tasks across the ring
- Monitor how the system recovers under load

### API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/recovery/events?since=<timestamp>` | Fetch recovery events since timestamp (0 for all) |
| `POST` | `/api/demo/fail-all` | Submit 50 failing demo tasks (triggers recovery) |
| `POST` | `/api/demo/run` | Submit N demo tasks with varying sleep durations |

---

## AI Task Chat

Every node exposes a conversational chat interface at `/chat` (e.g. `http://127.0.0.1:5001/chat`).

Open it via:
- The **"AI Task Chat"** link in the sidebar "Assistant" section of the dashboard
- The **pulsing chat bubble** fixed to the bottom-right corner of the dashboard
- Directly in the browser

The chat lets you submit tasks and query ring state in natural language. It is backed by the same REST API as the dashboard.

---

## Observability Stack

Prometheus and Grafana provide real-time observability of the Chord DHT cluster. The Grafana dashboard is **embedded directly in the Chord control center** (Observability tab) as well as available standalone.

### Setup

**Start Prometheus + Grafana:**

```bash
cd observability
docker-compose up -d
```

**Start a Chord node** (in another terminal):

```bash
python3 run_node.py --port 5005
```

**Open the dashboard:**

Visit **http://127.0.0.1:5005/dashboard** and click the **Observability** tab.

You should see the live Grafana dashboard with:
- **Request Throughput per Node** — file requests/sec routed by each node
- **Avg Hop Count** — average Chord routing hops (O(log N) guarantee)
- **Ring Size** — number of live nodes
- **Total Requests** — cumulative file requests across the ring

The dashboard auto-refreshes every 5 seconds.

### Configuration

| Service | URL | Credentials |
|---|---|---|
| Prometheus | http://localhost:9090 | — |
| Grafana (embedded) | http://127.0.0.1:5005/dashboard → Observability tab | — (anonymous) |
| Grafana (standalone) | http://localhost:3000 | admin / chord123 |

### How It Works

- **Prometheus** scrapes `/prom_metrics` endpoint on each Chord node every 5 seconds
- **Grafana** displays the scraped metrics using the pre-built **Chord DHT** dashboard (`observability/grafana/dashboards/chord_dht.json`)
- The **Observability tab** in the control center loads Grafana dynamically via `/api/config` endpoint, allowing the Grafana URL to be overridden via the `GRAFANA_URL` environment variable (useful for Docker, Kubernetes, or cloud deployments)

### Environment Variable

To use a different Grafana URL (e.g., for Docker-to-container communication):

```bash
GRAFANA_URL=http://grafana:3000 python3 run_node.py --port 5005
```

The `/api/config` endpoint will return the custom URL, and the iframe will use it.

---

## Environment Configuration

Configure Chord nodes using the following environment variables:

| Variable | Description | Default |
|---|---|---|
| `CHORD_DATA_DIR` | Directory for persisting DHT data and state | `~/.chord-data` |
| `REQUEST_LOG_PATH` | Path to request log file (JSONL format) | `request_log.jsonl` |
| `GRAFANA_URL` | Override Grafana URL for observability tab (useful for Docker/K8s) | `http://localhost:3000` |
| `ANTHROPIC_API_KEY` | API key for Claude LLM (required for AI agent placement) | — |
| `LOG_LEVEL` | Python logging level: DEBUG / INFO / WARNING / ERROR | `INFO` |

### Example: Start node with custom data directory and log level

```bash
CHORD_DATA_DIR=/tmp/chord-data LOG_LEVEL=DEBUG python run_node.py --port 5001
```

---

The `simulator/` package runs an in-process virtual ring — no real HTTP servers, ports, or Docker required.

### End-to-end demo
```bash
python run_demo.py                       # 5 nodes, 10 jobs (default)
python run_demo.py --nodes 10 --jobs 20
```

### Strategy benchmarks
Compares LLM agent, heuristic, and fallback placement across multiple runs:
```bash
python run_benchmark.py                              # 5 nodes, 20 jobs, 2 runs
python run_benchmark.py --nodes 10 --jobs 50 --runs 3
```
Results are printed to stdout and summarised in `simulator/results.md`.

### Fault injection tests
```bash
python run_fault_tests.py                # 5 nodes (default)
python run_fault_tests.py --nodes 10
```
Exercises node crash, partition, and recovery scenarios against the virtual ring.

---

## Utility Scripts

| Script | Purpose |
|---|---|
| `run_node.py` | Start a single Chord node (see CLI options above) |
| `run_demo.py` | End-to-end simulator demo |
| `run_benchmark.py` | Strategy comparison benchmarks |
| `run_fault_tests.py` | Fault injection test suite |
| `submit_job.py` | Submit jobs to a live ring from the command line |

### `submit_job.py` examples

```bash
# Echo job (no polling)
python submit_job.py --node 127.0.0.1:5001 --type echo \
  --payload '{"message": "hello ring"}'

# Compute job with polling until done
python submit_job.py --node 127.0.0.1:5001 --type compute \
  --payload '{"n": 50000}' --replicas 2 --poll

# Sleep job
python submit_job.py --node 127.0.0.1:5001 --type sleep \
  --payload '{"seconds": 3}' --poll
```

---

## AWS Demo Deployment

For a class demo on AWS with minimal cost (**~$0 on Free Tier**, ~$14/month on t3.small).

**Single EC2 instance running all 5 containers via Docker Compose** — no EKS, no RDS.

```bash
# Prerequisites: AWS CLI configured, Terraform ≥ 1.3, rsync
cd Distributed_Chord_Infrastructure
chmod +x deploy/ec2-demo/scripts/*.sh
bash deploy/ec2-demo/scripts/deploy.sh    # provisions + uploads + starts everything (~10 min)
```

After deploy, the script prints all URLs. To destroy all resources when done:

```bash
bash deploy/ec2-demo/scripts/teardown.sh
```

See **[DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)** for prerequisites, cost breakdown, step-by-step manual instructions, and troubleshooting.

> **Why not EKS?** EKS costs ~$163/month (control plane + 3 nodes). The EC2 approach achieves the same demo at ~$0–14/month.

---

## Retry & Fallback Policy

Retries are layered so a single transient failure never surfaces to the caller.

| Layer | Policy |
|---|---|
| Transport (`transport.py`) | 3 retries, exponential back-off: 0.1 s → 0.2 s → 0.4 s, all HTTP verbs |
| Routed endpoints (`server.py`) | Re-resolve Chord successor + retry ×2 for `/put`, `/get`, `/del`, `GET /jobs/<id>` |
| Primary write (`task_service.py`) | Re-resolve + retry ×2 if remote primary write fails due to stale routing |
| Read fallback (`task_service.py`) | If primary throws a network exception on `get_task`, automatically falls through to replica nodes |
| Replication (`replication.py`) | Per-replica retry ×2 (0.2 s → 0.4 s); declared `COMPLETE` at `ceil(k/2)` — one flaky replica does not degrade every write |
| Agent (`agent.py`) | Falls back to heuristic (least-loaded node) on any LLM exception; logged as `strategy="heuristic_fallback"` |
| Agent ring walk (`agent_loop.py`) | Dead nodes are skipped via finger-table jump — walk continues over healthy peers |
| Worker (`worker.py`) | Jobs retried up to `MAX_JOB_RETRIES = 3`; timed-out jobs (`JOB_TIMEOUT = 120 s`) follow the same budget |

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

### Routed Data API (auto-routes to responsible node, with re-resolve retry)

| Method | Path | Description |
|---|---|---|
| `POST` | `/put/<key>` | Hash the key, route to responsible node, store there |
| `GET` | `/get/<key>` | Hash the key, route to responsible node, retrieve from there |
| `DELETE` | `/del/<key>` | Hash the key, route to responsible node, delete there |
| `GET` | `/chord/key-owner/<key>` | Find the Chord-responsible node for any raw key |

### Job API

| Method | Path | Description |
|---|---|---|
| `POST` | `/jobs` | Submit a job — agent selects placement node, worker executes it |
| `GET` | `/jobs` | List all jobs on this node (`?status=pending\|running\|done\|failed`) |
| `GET` | `/jobs/<job_id>` | Retrieve a job by ID (routed to responsible node) |

Job body: `{"type": "echo|sleep|compute", "payload": {...}, "replicas": 1}`

### Task Service REST Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/tasks` | Register a task with schema validation + replication |
| `GET` | `/tasks/<task_id>` | Retrieve task by ID (primary first, replica fallback on failure) |
| `PATCH` | `/tasks/<task_id>` | Partial update: `status`, `result`, `error`, `payload`, `priority` |
| `DELETE` | `/tasks/<task_id>?hard=true\|false` | Soft deregister (tombstone) or hard delete |
| `GET` | `/tasks?job_id=&status=&include_deleted=&limit=` | Query local tasks on this node |
| `GET` | `/ring/lookup/<task_id>` | Resolve primary owner + replica chain for a task |
| `GET` | `/nodes/self` | Return local node state |
| `GET` | `/nodes/query?address=<host:port>` | Query remote node state |

### Metrics & Observability

| Method | Path | Description |
|---|---|---|
| `GET` | `/metrics` | Node metrics snapshot: queue depth, jobs, hop stats, agent decisions |
| `GET` | `/prom_metrics` | Prometheus text-format metrics (scraped by Prometheus) |
| `GET` | `/api/logs` | Last 40 structured agent decision log entries |
| `GET` | `/api/ring` | Full ring snapshot: all known nodes with IDs and addresses |
| `GET` | `/api/status` | Health summary: queue depth, jobs completed/failed, ring size |
| `GET` | `/api/recovery/events?since=<timestamp>` | Recovery events since Unix timestamp; returns `{"events": [...], "total_recovery_attempts": N}` |
| `GET` | `/api/workers/status` | Worker status including latency, pending tasks, and performance scores |
| `GET` | `/api/metrics/snapshot` | System-wide metrics: throughput, latency, hops, job counts |
| `GET` | `/api/observability/events` | Real-time observability events stream |
| `GET` | `/api/observability/trace/<task_id>` | Trace execution path and timeline for a specific task |
| `GET` | `/api/observability/timeseries` | Time-series data for metrics visualization |
| `POST` | `/api/tasks/reset-all` | Clear all local tasks and reset counters (debug only) |
| `POST` | `/api/tasks/clear-completed` | Archive completed tasks (cleanup) |

### Demo & Failure Injection

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/demo/run` | Submit N demo tasks; body: `{"count": N}` (default 10, max 200) |
| `POST` | `/api/demo/fail-all` | Submit 15 tasks that fail immediately; triggers recovery event logging |

### Admin & Debug Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/admin/leave` | Gracefully remove this node from the ring (data handoff to successor) |
| `POST` | `/admin/crash` | Force-crash this node (for testing failure scenarios) |
| `DELETE` | `/api/nodes/<address>` | Remove a node from the ring (remote control) |
| `POST` | `/api/nodes/<address>/crash` | Crash a remote node (for testing) |
| `POST` | `/api/nodes/add` | Add a new node to the ring and spawn a subprocess |
| `POST` | `/api/workers/spawn` | Spawn additional worker threads on this node |
| `POST` | `/api/workers/rebalance` | Rebalance worker load across the ring |
| `POST` | `/api/workers/<worker_id>/kill` | Terminate a specific worker (fault injection) |
| `GET` | `/debug/worker-tasks/<worker_id>` | Inspect tasks assigned to a worker |
| `GET` | `/debug/result-record/<task_id>` | View raw result record for debugging |
| `GET` | `/debug/worker-metrics` | Worker performance and latency metrics |
| `GET` | `/api/dht/contents` | Dump full DHT store contents (all keys and values) |
| `POST` | `/debug/trigger-recovery` | Manually trigger recovery event emission (testing) |
| `GET` | `/workers/live` | Check liveness of workers on this node |
| `GET` | `/workers/status` | Worker status summary |

### Task Execution API (Streaming)

| Method | Path | Description |
|---|---|---|
| `GET` | `/live` | WebSocket-compatible live task submission UI |
| `POST` | `/api/live/submit` | Submit and stream task results in real-time; returns `{"result": {...}, "status": "..."}` |

### Public Task Endpoints (Simplified Interface)

| Method | Path | Description |
|---|---|---|
| `POST` | `/createTask` | Simplified task creation; body: `{"type": "...", "payload": {...}}` |
| `GET` | `/getStatus/<task_id>` | Poll task status and result; returns `{"status": "...", "result": {...}}` |

### Internal Task Replication REST Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/internal/tasks/replica/<task_key>` | Store replica on local node |
| `GET` | `/internal/tasks/replica/<task_key>` | Retrieve replica from local node |
| `DELETE` | `/internal/tasks/replica/<task_key>` | Delete replica from local node |

### gRPC Services

- `TaskService` — `RegisterTask`, `GetTask`, `DeregisterTask`, `QueryTasks`, `LookupTaskOwner`, `GetNodeInfo`
- `InternalReplicationService` — `ReplicateTask`
- Contract file: `api/task_service.proto`
- Enable with: `python run_node.py --port 5001 --grpc-port 50051`

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

### Submit and query jobs

```bash
# Submit an echo job — agent places it on the best node
curl -X POST http://127.0.0.1:5001/jobs \
  -H "Content-Type: application/json" \
  -d '{"type": "echo", "payload": {"message": "hello ring"}, "replicas": 1}'

# Submit a compute job
curl -X POST http://127.0.0.1:5002/jobs \
  -H "Content-Type: application/json" \
  -d '{"type": "compute", "payload": {"n": 10000}}'

# List all jobs on node 1
curl http://127.0.0.1:5001/jobs | python -m json.tool
```

### Register, update, retrieve, and remove a task

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

# 2) Update status (PATCH)
curl -X PATCH http://127.0.0.1:5001/tasks/task-curl-1 \
  -H "Content-Type: application/json" \
  -d '{"status": "RUNNING"}'

# 3) Retrieve from another node (replica fallback supported)
curl http://127.0.0.1:5003/tasks/task-curl-1

# 4) Query tasks by job_id/status
curl "http://127.0.0.1:5002/tasks?job_id=job-curl-1&status=RUNNING&limit=10"

# 5) Soft-deregister (tombstone)
curl -X DELETE "http://127.0.0.1:5001/tasks/task-curl-1?hard=false"

# Hard delete (removes from all replicas)
curl -X DELETE "http://127.0.0.1:5001/tasks/task-curl-1?hard=true"
```

### DHT Store (routed key-value)

```bash
# Store a value — routes to the responsible node
curl -X POST http://127.0.0.1:5001/put/config.json \
  -H "Content-Type: application/json" \
  -d '{"version": 2, "threshold": 0.85}'

# Retrieve from any node — auto-routes
curl http://127.0.0.1:5003/get/config.json

# Find which node owns a key
curl http://127.0.0.1:5001/chord/key-owner/config.json

# Delete via routed endpoint
curl -X DELETE http://127.0.0.1:5001/del/config.json

# List all keys stored locally on node 5001
curl http://127.0.0.1:5001/data
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
# Whole suite (71 tests, ~1 s)
pytest tests -v

# Quiet summary only
pytest -q

# A single test file
pytest tests/test_task_service.py -v

# A single test by name
pytest tests/test_session_fixes.py::test_score_all_workers_prefers_lower_load -v

# Stop on the first failure (handy while iterating)
pytest -x

# Just the new behaviour added in recent fixes
pytest tests/test_session_fixes.py tests/test_project_coverage.py -v

# Coverage report (requires pytest-cov: `pip install pytest-cov`)
pytest tests --cov=chord --cov=storage --cov=api --cov-report=term-missing
```

### What each test file covers

| File | Focus |
|---|---|
| `tests/test_chord.py` | Chord ring internals — hashing, join/leave, find_successor, stabilize, notify, predecessor failure detection |
| `tests/test_task_service.py` | TaskService CRUD: register / get / soft+hard deregister, duplicate-task conflict, query filters |
| `tests/test_replication.py` | k-successor replication, replica fallback on read, delete propagation |
| `tests/test_grpc_service.py` | gRPC bridge — RegisterTask, GetTask, replication RPC, error-code mapping |
| `tests/test_session_fixes.py` | Worker registry: duplicate-worker detection via process tokens, scored placement, optimistic-pending counter, liveness scoping |
| `tests/test_project_coverage.py` | Recovery decision matrix (RETRY/WAIT/GIVE_UP paths), task executor (SCRIPT success/timeout/error), data_store persistence snapshot |

> **Tip — clean state between runs:** the project persists each node's data_store to `~/.chord-data/node-<id>.json`. Tests use an isolated tmp dir via the `CHORD_DATA_DIR` env var (set automatically in fixtures), but if you've also run the dashboard locally and want a fresh slate: `rm -rf ~/.chord-data`.

### Failure Injection Testing

Test the recovery system manually via the dashboard:

1. **Dashboard approach** (easiest):
   - Open Observability tab → view initial Recovery Events (should be 0)
   - Click **💥 Inject 15 Failures** in Workloads tab
   - Watch Recovery Events appear in real-time with filtering by type
   - Observe retries and recovery progression

2. **API approach**:
   ```bash
   curl -X POST http://127.0.0.1:5005/api/demo/fail-all
   curl http://127.0.0.1:5005/api/recovery/events
   ```

3. **Simulator approach**:
   ```bash
   python run_fault_tests.py --nodes 5
   ```
   Tests node crash, partition, and recovery scenarios against virtual ring.

Expected output:
```
tests/test_chord.py::TestHashing::test_sha1_id_returns_int         PASSED
tests/test_task_service.py::test_register_and_get_task_local_store PASSED
tests/test_replication.py::test_successor_chain_and_replication... PASSED
tests/test_grpc_service.py::test_grpc_register_and_get_task        PASSED
...
======= 71 passed =======
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

### Job submission (`POST /jobs`)
```json
{
  "ok": true,
  "job_id": "a3f9c12d...",
  "primary_key": "job:a3f9c12d...",
  "stored_at_node": 80,
  "placement_reasoning": "Node 80 has lowest queue depth (0 pending jobs)",
  "replicas": []
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
      "status": "REGISTERED",
      "version": 1
    },
    "task_key": "task:task-curl-1",
    "storage": {
      "replication_state": "COMPLETE",
      "quorum": {"required": 2, "achieved": 3}
    }
  }
}
```

### Task partial update (`PATCH /tasks/task-curl-1`)
```json
{
  "ok": true,
  "data": {
    "task": {
      "task_id": "task-curl-1",
      "status": "RUNNING",
      "version": 2,
      "updated_at": "2025-05-04T12:34:56Z"
    },
    "storage": {"replication_state": "COMPLETE"}
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
      "status": "RUNNING",
      "version": 2
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
      "status": "DELETED",
      "deleted": true
    },
    "hard_delete": false,
    "storage": {"replication_state": "COMPLETE"}
  }
}
```

---

---

## Troubleshooting

### Issue: "Address already in use" error

**Cause:** Another Chord node or service is running on the same port.

**Solution:**
```bash
# Find and kill the process on port 5001
lsof -ti:5001 | xargs kill -9

# Or use a different port
python run_node.py --port 5005
```

### Issue: Nodes can't find each other / ring won't stabilize

**Cause:** Firewall blocking inter-node communication or incorrect join address.

**Solution:**
- Verify firewall allows traffic on your chosen ports (5001-5003)
- Use correct host:port when joining: `python run_node.py --port 5002 --join 127.0.0.1:5001`
- Check node logs for network errors: `LOG_LEVEL=DEBUG python run_node.py --port 5001`

### Issue: AI agent isn't placing tasks

**Cause:** `ANTHROPIC_API_KEY` environment variable not set, or API key is invalid.

**Solution:**
```bash
export ANTHROPIC_API_KEY="sk-..."
python run_node.py --port 5001
```

Node will fall back to heuristic (least-loaded) placement if LLM fails.

### Issue: Recovery events not appearing in dashboard

**Cause:** Recovery manager not enabled or events cleared.

**Solution:**
- Recovery tab should appear in dashboard after first failure injection
- Use **💥 Inject 15 Failures** button to trigger events
- Check `/api/recovery/events` endpoint directly: `curl http://127.0.0.1:5001/api/recovery/events`

### Issue: Docker Compose fails to build

**Cause:** Old Docker cache or missing dependencies.

**Solution:**
```bash
# Force rebuild without cache
docker compose down -v
docker compose up --build --no-cache
```

### Issue: Prometheus/Grafana not scraping metrics

**Cause:** Node not exposing `/prom_metrics` or scrape config incorrect.

**Solution:**
- Verify endpoint is reachable: `curl http://127.0.0.1:5001/prom_metrics`
- Check Prometheus config at `observability/prometheus.yml`
- Restart Prometheus: `docker compose -f observability/docker-compose.yml restart prometheus`

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

### Replication and quorum

When a task is registered it is written to the primary node (the Chord successor responsible for `hash("task:{task_id}")`), then replicated to `k-1` successor nodes. Replication is declared `COMPLETE` once at least `ceil(k/2)` copies exist (quorum). This means one slow or flaky replica does not block writes, while data still survives `floor(k/2)` simultaneous node failures.

### Replication and quorum

When a task is registered it is written to the primary node (the Chord successor responsible for `hash("task:{task_id}")`), then replicated to `k-1` successor nodes. Replication is declared `COMPLETE` once at least `ceil(k/2)` copies exist (quorum). This means one slow or flaky replica does not block writes, while data still survives `floor(k/2)` simultaneous node failures.

If the primary is later unreachable, `get_task` automatically falls back to the replica chain — no client retry needed.

---

## Key Files Reference

| File | Purpose |
|---|---|
| `chord/node.py` | Chord DHT core logic (hashing, ring, finger tables, stabilization) |
| `chord/server.py` | Flask HTTP server exposing all REST + gRPC endpoints |
| `chord/agent.py` | AI-powered task placement using Claude LLM |
| `chord/worker.py` | Local job execution and task runner |
| `chord/recovery.py` | Failure tracking and recovery event logging |
| `storage/task_service.py` | Task CRUD and replication logic |
| `storage/replication.py` | Quorum-based replication manager |
| `storage/result_service.py` | Task result storage and retrieval |
| `chord/static/index.html` | Control Center dashboard (8 tabs, single HTML file) |
| `api/task_service.proto` | gRPC service contract |
| `tests/` | Comprehensive test suite (71 tests) |
| `simulator/` | Virtual-node simulator for benchmarking and fault injection |
| `docs/` | API specs, deployment guides, agent policy |

---

## License

This project is part of CMPE 273 (Distributed Systems) at San Jose State University.

**Team:** Anupama Singh, Neeraja Abhinav Buch, Vi Thi Tuong Nguyen, Sreya Somisetty

---
