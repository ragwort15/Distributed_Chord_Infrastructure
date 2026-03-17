# Distributed_Chord_Infrastructure
# Chord DHT + MCP — Project Plan

## Motivation / Problem Statement

A typical household has multiple devices (e.g. MacBook, iPhone, iPad, desktop, another laptop). Files created on one device are often not accessible from another. A common workaround is to sync a shared folder via a cloud provider (iCloud, Google Drive, etc.). These providers charge for storage (e.g. on the order of $2 per 100GB); even when home devices have plenty of free space (e.g. hundreds of GB each), users still pay for cloud storage to share files across their own machines.

Because these devices usually share a home network, we can avoid that cost and dependency by solving **file sharing in a peer-to-peer way over the local network**. A Chord Distributed Hash Table (DHT) is well-suited to this: it provides decentralized, scalable key-based lookup and storage without a central server. Each device can act as a Chord node; files are stored and discovered across the home network using the DHT, using existing local storage instead of paid cloud space. This project implements that P2P file-sharing solution using Chord, with MCP as the interface for users and administration.

---

## 1. Overview

A Chord DHT where each node is an MCP server. External **User** and **Admin** clients connect via MCP to perform file operations and inspect the ring. All processes run as TCP servers on the same machine (localhost) on different ports.

---

## 2. Distributed Entities

| Entity        | Role              | Port range (example) | Transport        |
|---------------|-------------------|----------------------|------------------|
| **Chord Node**| DHT peer + MCP server | e.g. 9001, 9002, … | TCP (MCP over TCP/HTTP or stdio) |
| **User MCP**  | Client + TCP server (optional) | e.g. 8000       | User connects to nodes via MCP |
| **Admin MCP** | Client + TCP server + metrics | e.g. 7000    | Connects to User/nodes; exposes Prometheus |

For simplicity, “TCP server” here means each process listens on a port; MCP can run over HTTP on that port (e.g. `/mcp` or similar) or over a dedicated MCP transport.

---

## 3. Entity Specifications

### 3.1 Chord Node MCP

- **Identity:** Node ID (m-bit, e.g. 160), listening host:port (e.g. `localhost:9001`).
- **Role:** Participates in Chord ring; stores keys (file metadata + blocks) for which it is responsible; answers MCP requests from User, Admin, and other nodes.

**Chord APIs (tools):**

- `chord_find_successor(key)` — Return node responsible for `key` (id + endpoint).
- `chord_get_predecessor()` — Return this node’s predecessor (id + endpoint).
- `chord_notify(node_id, endpoint)` — “I am your predecessor.”
- `chord_get_successor_list()` — Return list of successors (id + endpoint).
- `chord_get_id()` — Return this node’s id and endpoint (for bootstrap/admin).

**File / DHT APIs (tools):**

- `dht_put_file(key, metadata, ...)` — Store file (or chunk) at this node if responsible; else forward.
- `dht_get_file(key)` — Return file (or chunk) if this node has it; else forward.
- `dht_delete_file(key)` — Delete file at this node if responsible; else forward.
- `dht_list_files()` — List keys (files) stored at **this** node only.

**Observability:**

- `dht_list_finger_table()` — Return this node’s finger table (for Admin).
- Every request: emit metrics (counters/histograms); structured logs (request id, key, node id, success/error, latency).

---

### 3.2 User MCP

- **Identity:** Optional listening port (e.g. 8000) if User is itself a small server (e.g. for health or future extensions). Primary role: **MCP client**.
- **Role:** Connects to **one** Chord node (bootstrap) via MCP. All file operations are done by calling that node’s tools; the node (and ring) handle routing.

**User-facing operations (implemented by calling Chord node tools):**

- **Create file** — Call `dht_put_file` on the bootstrap node (key = e.g. hash(filename) or assigned id); node routes to responsible node(s).
- **Fetch file** — Call `dht_get_file(key)` on bootstrap node; node routes to responsible node and returns value.
- **Delete file** — Call `dht_delete_file(key)` on bootstrap node; node routes to responsible node.

**Observability:**

- Each User operation: log (operation, key, target node, success/error); emit metric (e.g. `user_requests_total`, `user_request_duration_seconds`).
- Optionally report metrics to Admin’s Prometheus (push or Admin scrapes a small metrics endpoint on User).

---

### 3.3 Admin MCP

- **Identity:** Listening port (e.g. 7000) for Admin API and/or Prometheus scrape.
- **Role:** MCP client that can connect to **any** User or **any** Chord node to inspect state; also hosts **Prometheus metrics** aggregating (or exposing) request metrics from the system.

**Admin capabilities:**

- **Connect to a node:** Use MCP client to that node’s endpoint.
- **List files on a node:** Call `dht_list_files()` on that node.
- **List finger table:** Call `dht_list_finger_table()` on that node.
- **Optional:** List files “globally” by querying a set of known nodes (or walking the ring) and aggregating.
- **Metrics server (Prometheus):**
  - Option A: Admin runs a Prometheus HTTP server (e.g. `:9090`) that **scrapes** each Chord node (and optionally User) if they expose `/metrics` (Prometheus format).
  - Option B: Nodes (and User) **push** metrics to Admin; Admin exposes them for Prometheus scrape.
  - Store and expose: request counts, latencies, errors, per operation (put/get/delete) and per node.

**Observability:**

- Admin’s own requests: logged and counted (e.g. `admin_list_files_total`, `admin_list_fingers_total`).

---

## 4. Observability

### 4.1 Metrics (Prometheus)

- **Suggested metrics (each Chord node):**
  - `chord_requests_total{operation="find_successor|get_predecessor|notify|..."}`
  - `chord_request_duration_seconds{operation="..."}`
  - `dht_operations_total{operation="put|get|delete", result="success|error"}`
  - `dht_operation_duration_seconds{operation="put|get|delete"}`
  - `chord_ring_size` (gauge, optional)

- **User MCP:**
  - `user_operations_total{operation="create|fetch|delete", result="success|error"}`
  - `user_operation_duration_seconds{operation="..."}`

- **Admin:** Optionally re-expose or aggregate the above; add `admin_*` metrics for list_files / list_finger_table.

- **Aggregation:** All request metrics from nodes and User should be visible in one place (Prometheus scraping Admin or each component).

### 4.2 Logging

- **Format:** Structured (e.g. JSON) with consistent fields:
  - `timestamp`, `level`, `node_id` or `entity`, `message`, `request_id`, `key` (if applicable), `error`, `duration_ms`
- **Levels:** DEBUG (finger updates, stabilize), INFO (incoming request, success), WARN (retries, timeouts), ERROR (failures).
- **Request correlation:** Each request gets a `request_id`; same id logged at each hop (User → node → … → node) for tracing.

---

## 5. Technology Stack (Suggestions)

| Concern           | Option |
|-------------------|--------|
| Language          | Go, Python, or Node — pick one for both Chord and MCP. |
| MCP               | Official MCP SDK for chosen language (e.g. `mcp` package); each Chord node = MCP server; User/Admin = MCP clients. |
| Transport         | MCP over HTTP on TCP (each node: `http://localhost:<port>`). |
| Metrics           | Prometheus client lib (e.g. `prometheus/client_golang` or `prometheus_client` in Python); expose `/metrics` per process. |
| Logging           | Structured logger (e.g. `zerolog`/`zap` in Go, `structlog` in Python) with same schema across entities. |

---

## 6. Directory Structure (Proposed)

```
ChordDHT/
├── PROJECT_PLAN.md          # This file
├── README.md
├── go.mod / requirements.txt / package.json  # As per language
│
├── cmd/
│   ├── chord-node/          # Chord node MCP server (main)
│   ├── user-mcp/            # User MCP client (and optional server)
│   └── admin-mcp/           # Admin MCP client + Prometheus server
│
├── internal/
│   ├── chord/               # Chord logic (ring, fingers, stabilize, find_successor)
│   ├── dht/                 # Key-value/file storage on top of Chord
│   ├── mcp/
│   │   ├── chord_tools.go   # Chord MCP tools (find_successor, notify, ...)
│   │   ├── dht_tools.go     # DHT MCP tools (put_file, get_file, list_files, list_finger_table)
│   │   └── server.go        # MCP server setup per node
│   ├── metrics/             # Prometheus metrics definitions + middleware
│   └── logging/             # Structured logger config
│
├── configs/                  # Example configs (ports, bootstrap, etc.)
└── scripts/                 # Start N nodes, one user, one admin (e.g. docker-compose or shell)
```

---

## 7. Implementation Phases

### Phase 1: Core Chord + single node

- [ ] Chord data structures (node id, finger table, successor list, predecessor).
- [ ] `find_successor`, `get_predecessor`, `notify`, stabilize, fix_fingers (in-memory or local-only first).
- [ ] One Chord node as MCP server exposing Chord tools only (no DHT yet).
- [ ] Logging: structured, consistent format.
- [ ] Metrics: basic counters for Chord RPCs.

### Phase 2: Multi-node ring + DHT

- [ ] Node join: bootstrap to existing node, key transfer, stabilize.
- [ ] MCP client inside each node to call other nodes (store node_id + endpoint in fingers/successor list).
- [ ] DHT: `dht_put_file`, `dht_get_file`, `dht_delete_file` (forward if not responsible).
- [ ] `dht_list_files()` and `dht_list_finger_table()` on each node.

### Phase 3: User MCP + Admin MCP

- [ ] User MCP client: connect to one node; implement create/fetch/delete file by calling node tools.
- [ ] Admin MCP client: connect to any node; list files, list finger table.
- [ ] Admin: Prometheus HTTP server; scrape nodes (and optionally User) at `/metrics`, or collect pushed metrics.
- [ ] Ensure every request (User, Admin, node-to-node) emits metrics and uses consistent logging.

### Phase 4: Observability + hardening

- [ ] Request IDs across hops; same ID in logs at each node.
- [ ] Dashboards (Grafana) or simple Prometheus queries for request rate, latency, errors.
- [ ] Node leave/failure: successor list + stabilize; basic replication for files (e.g. replicate to next k nodes).

---

## 8. Configuration (Example)

- **Chord node:** `config.json` or env: `NODE_ID`, `LISTEN_PORT`, `BOOTSTRAP_ENDPOINT` (optional), `METRICS_PORT` (optional, or same server `/metrics`).
- **User MCP:** `BOOTSTRAP_NODE_URL` (e.g. `http://localhost:9001`), optional `USER_LISTEN_PORT`.
- **Admin MCP:** `PROMETHEUS_PORT` (e.g. 9090), list of node URLs for “list all” or scrape targets.

---

## 9. Summary

| Entity       | Listens (TCP) | Connects via MCP to      | Main APIs / Actions |
|-------------|---------------|---------------------------|----------------------|
| Chord Node  | Yes (e.g. 900x) | Other nodes (fingers, successor) | Chord + dht_put/get/delete/list_files/list_finger_table |
| User MCP    | Optional (8000) | One Chord node           | Create / fetch / delete file |
| Admin MCP   | Yes (7000 + 9090) | Any User or node       | List files, list finger table; Prometheus metrics |

All request paths emit metrics and use consistent structured logging; Prometheus is hosted at Admin and scrapes (or receives) metrics from nodes and optionally User.
