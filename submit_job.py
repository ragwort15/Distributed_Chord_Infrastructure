"""
CLI tool for submitting jobs to the Chord DHT job platform.

Examples:
  # Submit an echo job (no polling)
  python submit_job.py --node 127.0.0.1:5001 --type echo --payload '{"message": "hello"}'

  # Submit a compute job with 2 replicas, then poll until done
  python submit_job.py --node 127.0.0.1:5001 --type compute --payload '{"n": 50000}' \
                       --replicas 2 --poll

  # Submit a sleep job
  python submit_job.py --node 127.0.0.1:5001 --type sleep --payload '{"seconds": 3}' --poll
"""

import argparse
import json
import sys
import time
import requests


def submit(node_address: str, job_type: str, payload: dict, replicas: int) -> dict:
    url = f"http://{node_address}/jobs"
    resp = requests.post(url, json={"type": job_type, "payload": payload,
                                    "replicas": replicas}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def poll_job(node_address: str, job_id: str,
             timeout: float = 60.0, interval: float = 1.0) -> dict:
    url = f"http://{node_address}/jobs/{job_id}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") in ("done", "failed"):
                return data
        time.sleep(interval)
    raise TimeoutError(f"Job {job_id} did not complete within {timeout}s")


def main():
    parser = argparse.ArgumentParser(description="Submit a job to the Chord DHT platform")
    parser.add_argument("--node", required=True, metavar="HOST:PORT",
                        help="Address of any ring node")
    parser.add_argument("--type", required=True, dest="job_type",
                        choices=["echo", "sleep", "compute"],
                        help="Job type")
    parser.add_argument("--payload", default="{}", metavar="JSON",
                        help="Job payload as a JSON string")
    parser.add_argument("--replicas", type=int, default=1,
                        help="Requested replication factor (agent may adjust)")
    parser.add_argument("--poll", action="store_true",
                        help="Poll until job completes and print result")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="Polling timeout in seconds (default: 60)")
    args = parser.parse_args()

    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON payload: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        result = submit(args.node, args.job_type, payload, args.replicas)
    except requests.RequestException as e:
        print(f"ERROR: could not submit job: {e}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2))

    if args.poll:
        job_id = result.get("job_id")
        if not job_id:
            print("ERROR: no job_id in response", file=sys.stderr)
            sys.exit(1)
        print(f"\nPolling for job {job_id} …")
        try:
            final = poll_job(args.node, job_id, timeout=args.timeout)
            print(json.dumps(final, indent=2))
            if final.get("status") == "failed":
                sys.exit(2)
        except TimeoutError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        except requests.RequestException as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
