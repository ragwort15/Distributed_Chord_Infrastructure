# Quick Start — Run the Distributed Job Execution Platform

End-to-end setup for a fresh clone. Brings up a Chord node, two workers, and the chatbot — in under 2 minutes.

> For deep architecture details (how Chord routes, replication, schema), see the main [README.md](./README.md). This file is just **how to run it**.

---

## 1. Prerequisites

- **Python 3.10+** (project tested on 3.14 via Homebrew on macOS; any 3.10+ works on Linux/macOS)
- **pip**
- **bash** (for SCRIPT-type tasks; comes with macOS/Linux by default)

**Optional**: an Anthropic API key if you want the AI agent to use Claude. Without one, the agent falls back to a deterministic scripted flow that walks you through the same steps.

---

## 2. Install

```bash
git clone https://github.com/ragwort15/Distributed_Chord_Infrastructure.git
cd Distributed_Chord_Infrastructure
pip install -r requirements.txt
```

---

## 3. Start the system (3 commands)

Open **3 terminals** in the repo root.

### Terminal 1 — start the Chord node

```bash
HEARTBEAT_TIMEOUT_S=15 python3 run_node.py --host 127.0.0.1 --port 5005
```

Wait until you see `Started on 127.0.0.1:5005`. The `HEARTBEAT_TIMEOUT_S=15` env var gives workers a 15-second window before they're marked dead.

### Terminal 2 — start worker `w1`

```bash
python3 -m chord.task_runner \
    --worker-id w1 \
    --frontend-url http://127.0.0.1:5005
```

You should see `[Worker w1] starting`. The worker heartbeats every 5s, polls for assigned tasks every 2s.

### Terminal 3 — start worker `w2`

```bash
python3 -m chord.task_runner \
    --worker-id w2 \
    --frontend-url http://127.0.0.1:5005
```

Now both workers are alive. Quick check:

```bash
curl http://127.0.0.1:5005/workers/live
# → {"live_workers":["w1","w2"]}
```

---

## 4. Open the chatbot (the landing page)

Visit **http://127.0.0.1:5005/** in your browser.

This is the AI agent UI. It walks you through submitting a task in plain English.

### Try it

1. Type **`I want to run a task`** and send.
2. Reply **`1`** (Script) or **`2`** (Binary).
3. For SCRIPT, paste a shell script body, e.g.: `echo hello world`.
4. Reply **`2`** to auto-assign (or `1` to pick a specific worker).
5. The agent confirms: `✓ Task accepted, ID: task_xxx, assigned to w1 (by frontend).`

To check status later, ask: `status of task_xxx` (use the actual ID from the previous step).

---

## 5. See what's happening — open the dashboard

Visit **http://127.0.0.1:5005/dashboard**.

You'll see:

- **Ring topology** — your one Chord node, positioned by SHA-1(host:port)
- **Workers panel** (right sidebar) — `w1` and `w2` with `idle` / `busy` / `dead` status, polled every 3s
- **Metric tiles** — Ring Size, Queue Depth, Jobs Done, Failed
- **Submit Job form** — Phase 1 jobs (`echo`, `sleep`, `compute`) — distinct from the Phase 2/3/4 task pipeline used by the chatbot

When the chatbot submits a task, watch a worker flip `idle → busy → idle` in real time.

---

## 6. Submit a task without the chatbot (curl)

```bash
# Submit (auto-assigned to a live worker)
curl -X POST http://127.0.0.1:5005/createTask \
    -H "Content-Type: application/json" \
    -d '{"task_id":"demo-1","task_details":{"task_type":"SCRIPT","path":"","script":"echo hello && date"}}'

# Wait a couple of seconds, then check
sleep 2
curl http://127.0.0.1:5005/getStatus/demo-1
```

Expected output:

```json
{
  "task_id": "demo-1",
  "status": "SUCCESS",
  "result": {
    "exit_code": 0,
    "stdout": "hello\nMon May  4 ...\n",
    "stderr": "",
    "duration_ms": 23,
    "timed_out": false
  },
  "worker_id": "w1",
  "assigned_by": "frontend"
}
```

---

## 7. Multi-node ring (optional)

Open more terminals to add nodes that **join the existing ring**:

```bash
# Terminal 4 — second node
python3 run_node.py --host 127.0.0.1 --port 5006 --join 127.0.0.1:5005

# Terminal 5 — third node
python3 run_node.py --host 127.0.0.1 --port 5007 --join 127.0.0.1:5005
```

The dashboard's ring view will now show 3 circles. Tasks are still routed by Chord, replicated k=3 across successors.

---

## 8. Run the regression / acceptance suites

If you want to verify everything works after a code change:

```bash
# Phase 3 acceptance (in-process — no live server needed)
python3 /tmp/phase3_test.py

# Phase 4 acceptance (in-process)
python3 /tmp/phase4_test.py
```

Each prints a per-criterion pass/fail and an aggregate count at the end.

> Note: those test files live in `/tmp/` from the development session. If they're missing, see the test scripts referenced in the project's PR descriptions.

---

## 9. URLs cheat sheet

| URL | What it is |
|---|---|
| `http://127.0.0.1:5005/` | **Chatbot** (landing page) |
| `http://127.0.0.1:5005/dashboard` | Ring + metrics + workers panel |
| `http://127.0.0.1:5005/createTask` | `POST` — submit a task |
| `http://127.0.0.1:5005/getStatus/<task_id>` | `GET` — read status + result |
| `http://127.0.0.1:5005/workers/live` | `GET` — currently-alive workers |
| `http://127.0.0.1:5005/workers/status` | `GET` — live/idle/busy/dead breakdown |
| `http://127.0.0.1:5005/agent/chat` | `POST` — agent endpoint (the chatbot UI calls this) |
| `http://127.0.0.1:5005/metrics` | Prometheus scrape endpoint |
| `http://127.0.0.1:5005/api/ring` | JSON ring topology (used by dashboard) |

---

## 10. Stop everything

```bash
# Find the python processes
ps aux | grep -E "run_node|task_runner" | grep -v grep

# Or just kill all of them
pkill -f "run_node.py"
pkill -f "task_runner"
```

---

## 11. Troubleshooting

### Port 5005 already in use

Something else is bound. Check with `lsof -nP -i :5005`. Either kill that process or pick a different port (`--port 5006`).

### Chatbot says "no live workers available"

No worker is currently heartbeating. Either:
- Start a worker (Terminal 2 above), or
- Wait — if a worker just died, it takes up to `HEARTBEAT_TIMEOUT_S` seconds to expire from the registry.

### Dashboard says "Connecting…" forever

The browser tab can't reach the server. Confirm the node is running on the URL you opened (`http://127.0.0.1:5005/dashboard` for default port).

### `python -m chord.task_runner` says `No module named chord`

You're not in the repo root. `cd` to the directory containing `run_node.py` first.

### Task submitted but never executes

- Check `curl http://127.0.0.1:5005/workers/live` — at least one worker must be live.
- Check the worker's terminal log. If you see "_fetch_assigned failed" repeatedly, the server is overloaded or unreachable.
- Check the server's terminal log for errors.

### Chatbot replies with "Task rejected" but the JSON body says "Task accepted"

This was a known bug in early Phase 3 (status-code mismatch) — fixed by commit `0ae8002` and earlier. Pull latest and restart.

---

## 12. What's next

Once you're up and running, look at:

- **README.md** — full architecture, how Chord works, API reference
- **chord/conversation_agent.py** — how the AI agent decides what to do
- **storage/result_service.py** + **storage/worker_assignment.py** — the two DHT-backed services (HT1 and HT2)
- **chord/task_runner.py** — the Phase 4 worker that polls HT2 and executes subprocesses
