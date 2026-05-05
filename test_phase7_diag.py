"""
Focused diagnostic for Phase 7 recovery_history not being written.
Starts fresh server+workers, creates one FAILURE task, and prints
the raw HT1 record at each stage.
"""
import json, os, subprocess, sys, time, requests

BASE = "http://127.0.0.1:5001"
procs = []

def j(r): return json.dumps(r.json(), indent=2)

def post(path, body=None): return requests.post(f"{BASE}{path}", json=body, timeout=5)
def get(path):             return requests.get(f"{BASE}{path}", timeout=5)

def raw_record(tid):
    r = get(f"/debug/result-record/{tid}")
    if r.status_code == 200: return r.json()
    return {"error": r.status_code}

def start():
    env = {**os.environ, "AGENT_STRATEGY": "heuristic"}
    procs.append(subprocess.Popen(
        [sys.executable, "run_node.py", "--host", "127.0.0.1", "--port", "5001",
         "--worker", "--log", "WARNING"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ))
    time.sleep(3)
    for wid in ["w1", "w2"]:
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "chord.task_runner",
             "--worker-id", wid, "--frontend-url", "http://localhost:5001",
             "--log-level", "WARNING"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
    time.sleep(5)

def stop():
    for p in procs:
        try: p.terminate()
        except: pass

def wait_for_failure(tid, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = raw_record(tid)
        if r.get("status") == "FAILURE":
            return r
        time.sleep(0.5)
    return raw_record(tid)

def run():
    print("Starting server + 2 workers...")
    start()

    r = get("/chord/ping")
    print(f"Ping: {r.json()}")

    r = get("/debug/worker-metrics")
    live = [w["worker_id"] for w in r.json()["workers"] if w["is_live"]]
    print(f"Live workers: {live}")

    # Create a task that will fail (non-zero exit)
    tid = "diag-fail-1"
    r = post("/createTask", {
        "task_id": tid,
        "task_details": {"task_type": "SCRIPT", "path": "", "script": "exit 1"},
        "max_attempts": 3,
        "retry_on_failure": False,
    })
    print(f"\n/createTask status={r.status_code}: {r.json()}")

    print("\nWaiting for task to fail...")
    rec = wait_for_failure(tid)
    print(f"\nRaw HT1 record BEFORE trigger:\n{json.dumps(rec, indent=2)}")

    print("\nTriggering recovery scan...")
    r = post("/debug/trigger-recovery")
    print(f"Trigger response: {r.json()}")
    time.sleep(2)  # let it process

    rec2 = raw_record(tid)
    print(f"\nRaw HT1 record AFTER trigger:\n{json.dumps(rec2, indent=2)}")

    print("\n--- Key fields ---")
    for field in ["status", "timed_out", "recovery_status", "recovery_history",
                  "retry_on_failure", "max_attempts", "attempt_count", "kind"]:
        print(f"  {field}: {rec2.get(field)!r}")

if __name__ == "__main__":
    try:
        run()
    finally:
        stop()
