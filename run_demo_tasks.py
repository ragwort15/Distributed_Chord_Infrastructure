#!/usr/bin/env python3
"""
Demo: submit 100 tasks to the running ring and watch them complete.

Each task script:
  - sleeps for a varying number of seconds (3..7, rotating)
  - prints "Welcome to Rakesh Ranjan's Distributed Class"

For each task we print:
  ✓ created  task_id  →  worker  (PENDING)
and then poll until SUCCESS / FAILURE, printing:
  ✓ done     task_id  →  worker  (SUCCESS)  output: ...

Usage:
  python3 run_demo_tasks.py             # uses default count 100
  python3 run_demo_tasks.py --count 25  # smaller demo
"""

import argparse
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
import json

DEFAULT_FRONTEND = "http://127.0.0.1:5005"
SLEEP_PATTERN = [3, 4, 5, 6, 7]  # rotates per task
WELCOME = "Welcome to Rakesh Ranjan's Distributed Class"
POLL_INTERVAL_S = 1.0
MAX_WAIT_S = 120

_print_lock = threading.Lock()


def post_json(url: str, payload: dict, timeout: float = 5.0) -> dict:
    body = json.dumps(payload).encode()
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get_json(url: str, timeout: float = 5.0) -> dict:
    with urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def submit_one(idx: int, frontend: str) -> dict:
    sleep_s = SLEEP_PATTERN[idx % len(SLEEP_PATTERN)]
    task_id = f"demo-{idx:03d}"
    script = f'echo "[task {task_id}] starting (sleep {sleep_s}s)"; sleep {sleep_s}; echo "{WELCOME}"; echo "[task {task_id}] done"'
    payload = {
        "task_id": task_id,
        "task_details": {"task_type": "SCRIPT", "path": "", "script": script},
    }
    res = post_json(f"{frontend}/createTask", payload)
    worker = res.get("worker_id", "?")
    with _print_lock:
        print(f"✓ CREATED  {task_id}  →  worker {worker}  (PENDING, will sleep {sleep_s}s)")
    return {"task_id": task_id, "worker": worker, "sleep": sleep_s}


def wait_for_completion(task_id: str, frontend: str) -> dict:
    deadline = time.time() + MAX_WAIT_S
    last_status = None
    while time.time() < deadline:
        try:
            d = get_json(f"{frontend}/getStatus/{task_id}")
            status = d.get("status")
            if status != last_status:
                last_status = status
            if status in ("SUCCESS", "FAILURE"):
                return d
        except Exception:
            pass
        time.sleep(POLL_INTERVAL_S)
    return {"status": "TIMEOUT", "task_id": task_id}


def run_one(idx: int, frontend: str) -> None:
    info = submit_one(idx, frontend)
    final = wait_for_completion(info["task_id"], frontend)
    status = final.get("status", "?")
    result = final.get("result") or {}
    stdout = (result.get("stdout") or "").strip()
    duration_ms = result.get("duration_ms", "?")
    icon = "✅" if status == "SUCCESS" else "❌"
    with _print_lock:
        print(f"\n{icon} DONE     {info['task_id']}  →  worker {info['worker']}  "
              f"({status}, {duration_ms}ms)")
        if stdout:
            for ln in stdout.splitlines():
                print(f"     │ {ln}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=100, help="how many tasks to submit")
    p.add_argument("--frontend", default=DEFAULT_FRONTEND, help="ring entry URL")
    p.add_argument("--concurrency", type=int, default=20,
                   help="how many tasks to submit & track in parallel")
    args = p.parse_args()

    # Sanity check: the frontend must be reachable.
    try:
        get_json(f"{args.frontend}/workers/live", timeout=2.0)
    except Exception as exc:
        print(f"frontend at {args.frontend} not reachable: {exc}", file=sys.stderr)
        return 1

    print(f"submitting {args.count} tasks to {args.frontend} "
          f"(sleeps rotate through {SLEEP_PATTERN}s, +5s demo delay per worker)\n")
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(run_one, i, args.frontend) for i in range(1, args.count + 1)]
        ok = err = 0
        for f in as_completed(futures):
            try:
                f.result()
                ok += 1
            except Exception as exc:
                err += 1
                with _print_lock:
                    print(f"✕ task driver error: {exc}", file=sys.stderr)
    elapsed = time.time() - started
    print(f"\nfinished {ok} ok, {err} errors in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
