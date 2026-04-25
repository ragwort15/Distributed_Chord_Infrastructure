# Task Service API Specification

## Overview

This document defines the task-oriented service layer built on top of the Chord DHT core.

- Primary storage key: `task:{task_id}`
- Consistent hash owner: successor of `sha1(task:{task_id})`
- Replication: write to primary + `k-1` nearest successors
- Consistency model: eventual consistency with primary-first reads and replica fallback

## Task Object Schema

```json
{
  "schema_version": 1,
  "kind": "task",
  "task_id": "a1b2c3",
  "job_id": "job-42",
  "type": "image.inference",
  "payload": {"input": "s3://bucket/key"},
  "priority": 5,
  "status": "REGISTERED",
  "result": null,
  "error": null,
  "created_at": "2026-04-24T18:40:00Z",
  "updated_at": "2026-04-24T18:40:00Z",
  "version": 2,
  "deleted": false,
  "placement": {
    "primary_node": {"id": 80, "address": "127.0.0.1:5002"},
    "replication_factor": 3,
    "replica_nodes": [
      {"id": 150, "address": "127.0.0.1:5003"},
      {"id": 10, "address": "127.0.0.1:5001"}
    ]
  }
}
```

## REST API

### 1) Register task

- **Method:** `POST /tasks`
- **Body:**

```json
{
  "task_id": "optional-explicit-id",
  "job_id": "job-42",
  "type": "image.inference",
  "payload": {"input": "s3://bucket/key"},
  "priority": 5
}
```

- **Response:** `201 Created`

```json
{
  "ok": true,
  "data": {
    "task": {"...": "task record"},
    "task_key": "task:optional-explicit-id",
    "storage": {
      "primary": {"id": 80, "address": "127.0.0.1:5002"},
      "replica_targets": [{"id": 150, "address": "127.0.0.1:5003"}],
      "replicas_written": [{"id": 150, "address": "127.0.0.1:5003"}],
      "failed": [],
      "replication_state": "COMPLETE"
    }
  }
}
```

### 2) Get task

- **Method:** `GET /tasks/{task_id}`
- **Response:** `200 OK` or `404 Not Found`

### 3) Deregister task

- **Method:** `DELETE /tasks/{task_id}?hard=true|false`
- **Behavior:**
  - `hard=false` (default): writes tombstone (`status=DELETED`) and replicates
  - `hard=true`: deletes from primary and replicas

### 4) Query local tasks

- **Method:** `GET /tasks?job_id=<id>&status=<status>&include_deleted=true|false&limit=100`
- **Scope:** local node store only

### 5) Lookup ownership and replicas

- **Method:** `GET /ring/lookup/{task_id}`
- **Response:** primary node + replica chain derived from successor traversal

### 6) Node querying

- **Method:** `GET /nodes/self`
- **Method:** `GET /nodes/query?address=<host:port>`

## Internal Replication REST API

### Store replica

- **Method:** `POST /internal/tasks/replica/{task_key}`
- **Body:** complete task object schema

### Read replica

- **Method:** `GET /internal/tasks/replica/{task_key}`

### Delete replica

- **Method:** `DELETE /internal/tasks/replica/{task_key}`

## Error Model

All external REST endpoints return this envelope:

```json
{
  "ok": false,
  "error": {
    "code": "TASK_NOT_FOUND",
    "message": "task not found: abc123"
  }
}
```

Common status codes:

- `201` created
- `200` success
- `404` not found
- `409` conflict
- `422` validation error
- `502` downstream node error

## gRPC API

Proto contract lives in `api/task_service.proto`.

Runtime notes:

- gRPC server starts when node is launched with `--grpc-port`.
- Example: `python run_node.py --port 5001 --id 10 --grpc-port 60051`.

Services:

- `TaskService`
  - `RegisterTask`
  - `GetTask`
  - `DeregisterTask`
  - `QueryTasks`
  - `LookupTaskOwner`
  - `GetNodeInfo`
- `InternalReplicationService`
  - `ReplicateTask`
