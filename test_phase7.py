"""
Phase 7 integration test.
Starts the server + 3 workers, runs all key scenarios, prints results.
"""
import json
import os
import subprocess
import sys
import time
import requests

BASE = "http://127.0.0.1:5001"
PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "

results = []

def log(label, ok, detail=""):
    sym = PASS if ok else FAIL
    line = f"  {sym}  {label}"
    if detail:
        line += f"  →  {detail}"
    print(line)
    results.append((ok, label))

def post(path, body):
    return requests.post(f"{BASE}{path}", json=body, timeout=5)

def get(path):
    return requests.get(f"{BASE}{path}", timeout=5)

def status(task_id):
    r = get(f"/getStatus/{task_id}")
    return r.json() if r.status_code == 200 else {"error": r.status_code}

def trigger():
    post("/debug/trigger-recovery", {})
    time.sleep(1)

def wait_for(task_id, expected_status, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = status(task_id)
        if s.get("status") == expected_status:
            return s
        time.sleep(0.5)
    return status(task_id)

procs = []
RUN_ID = str(int(time.time()))[-5:]  # short suffix to keep task IDs unique per run

def _free_port():
    """Kill any process already bound to 5001 so we always get a clean start."""
    import signal as _sig
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", ":5001"], text=True
        ).strip()
        if out:
            for pid in out.split():
                try:
                    os.kill(int(pid), _sig.SIGKILL)
                except OSError:
                    pass
            time.sleep(1)
    except Exception:
        pass

def start_all():
    _free_port()
    env = {**os.environ, "AGENT_STRATEGY": "heuristic"}
    srv = subprocess.Popen(
        [sys.executable, "run_node.py", "--host", "127.0.0.1",
         "--port", "5001", "--worker", "--log", "WARNING"],
        env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    procs.append(srv)
    time.sleep(3)

    for wid in ["w1", "w2", "w3"]:
        w = subprocess.Popen(
            [sys.executable, "-m", "chord.task_runner",
             "--worker-id", wid,
             "--frontend-url", "http://localhost:5001",
             "--log-level", "WARNING"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(w)
    time.sleep(5)   # wait for heartbeats to register

def stop_all():
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass

def check_setup():
    print("\n── Setup ────────────────────────────────────────")
    try:
        r = get("/chord/ping")
        log("Server responds on /chord/ping", r.status_code == 200)
    except Exception as e:
        log("Server responds on /chord/ping", False, str(e))
        return False

    try:
        r = get("/debug/worker-metrics")
        live = [w["worker_id"] for w in r.json()["workers"] if w["is_live"]]
        log(f"Workers registered ({len(live)} live)", len(live) >= 2, str(live))
    except Exception as e:
        log("Workers registered", False, str(e))
    return True

def test_new_fields_present():
    print("\n── Test 1: New fields in /createTask + /getStatus ───")
    r = post("/createTask", {
        "task_id": f"t1-fields-{RUN_ID}",
        "task_details": {"task_type": "SCRIPT", "path": "", "script": "echo hi"},
        "max_attempts": 5,
        "retry_on_failure": True,
    })
    log("/createTask returns 201", r.status_code == 201, str(r.status_code))

    s = wait_for(f"t1-fields-{RUN_ID}", "SUCCESS", timeout=15)
    log("Task completes with SUCCESS", s.get("status") == "SUCCESS", s.get("status"))
    log("attempt_count in response", "attempt_count" in s, str(s.get("attempt_count")))
    log("recovery_history in response", "recovery_history" in s, str(s.get("recovery_history")))
    log("last_failure_reason in response", "last_failure_reason" in s)
    log("recovery_note in response", "recovery_note" in s, str(s.get("recovery_note")))

def test_give_up_no_retry():
    print("\n── Test 2: task_failure + retry_on_failure=false → GIVE_UP ─")
    r = post("/createTask", {
        "task_id": f"t2-giveup-{RUN_ID}",
        "task_details": {"task_type": "SCRIPT", "path": "", "script": "exit 1"},
        "max_attempts": 3,
        "retry_on_failure": False,
    })
    log("/createTask returns 201", r.status_code == 201)

    wait_for(f"t2-giveup-{RUN_ID}", "FAILURE", timeout=15)
    trigger()

    s = status(f"t2-giveup-{RUN_ID}")
    log("Status is FAILURE", s.get("status") == "FAILURE", s.get("status"))
    log("attempt_count stays 0 (no retry allowed)", s.get("attempt_count", -1) == 0,
        str(s.get("attempt_count")))
    log("recovery_history=['GIVE_UP']",
        "GIVE_UP" in (s.get("recovery_history") or []),
        str(s.get("recovery_history")))

def test_retry_on_failure():
    print("\n── Test 3: task_failure + retry_on_failure=true → RETRY_DIFFERENT ─")
    r = post("/createTask", {
        "task_id": f"t3-retry-{RUN_ID}",
        "task_details": {"task_type": "SCRIPT", "path": "", "script": "exit 1"},
        "max_attempts": 3,
        "retry_on_failure": True,
    })
    log("/createTask returns 201", r.status_code == 201)

    wait_for(f"t3-retry-{RUN_ID}", "FAILURE", timeout=15)
    trigger()
    time.sleep(3)  # let re-queued task run and fail again

    s = status(f"t3-retry-{RUN_ID}")
    log("attempt_count > 0 after retry", s.get("attempt_count", 0) > 0,
        f"attempt_count={s.get('attempt_count')}")
    hist = s.get("recovery_history") or []
    retried = any("RETRY_DIFFERENT" in h for h in hist)
    log("recovery_history contains RETRY_DIFFERENT", retried, str(hist))

def test_max_attempts_respected():
    print("\n── Test 4: max_attempts=1 → GIVE_UP after 1 retry ─")
    tid = f"t4-maxout-{RUN_ID}"
    r = post("/createTask", {
        "task_id": tid,
        "task_details": {"task_type": "SCRIPT", "path": "", "script": "exit 1"},
        "max_attempts": 1,
        "retry_on_failure": True,
    })
    log("/createTask returns 201", r.status_code == 201)

    # Wait for first failure, trigger recovery (→ RETRY_DIFFERENT, attempt=1)
    wait_for(tid, "FAILURE", timeout=15)
    trigger()

    # Now wait for the re-queued task to fail a second time, then trigger again
    # poll until attempt_count > 0 AND status == FAILURE (means retry was run & failed)
    deadline = time.time() + 15
    while time.time() < deadline:
        s = status(tid)
        if s.get("attempt_count", 0) > 0 and s.get("status") == "FAILURE":
            break
        time.sleep(0.5)

    trigger()  # second trigger should now see attempt_count >= max_attempts → GIVE_UP
    time.sleep(1)

    s = status(tid)
    hist = s.get("recovery_history") or []
    gave_up = "GIVE_UP" in hist
    log("GIVE_UP in history after max_attempts exceeded", gave_up, str(hist))

def test_trigger_recovery_endpoint():
    print("\n── Test 5: /debug/trigger-recovery endpoint ─")
    r = post("/debug/trigger-recovery", {})
    log("/debug/trigger-recovery returns 200", r.status_code == 200, str(r.status_code))
    log("Response has ok=true", r.json().get("ok") is True, str(r.json()))

def run():
    print("=" * 56)
    print("  Phase 7 Integration Test")
    print("=" * 56)

    print("\nStarting server + 3 workers…")
    start_all()

    try:
        ok = check_setup()
        if not ok:
            print("\n[FATAL] Server did not start — aborting tests")
            return

        test_new_fields_present()
        test_give_up_no_retry()
        test_retry_on_failure()
        test_max_attempts_respected()
        test_trigger_recovery_endpoint()

    finally:
        stop_all()

    # Summary
    passed = sum(1 for ok, _ in results if ok)
    total  = len(results)
    print(f"\n{'='*56}")
    print(f"  Result: {passed}/{total} checks passed")
    if passed == total:
        print("  ALL PASSED ✅")
    else:
        print("  FAILED checks:")
        for ok, label in results:
            if not ok:
                print(f"    {FAIL} {label}")
    print("=" * 56)

if __name__ == "__main__":
    run()
