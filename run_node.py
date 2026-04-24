"""
CLI entrypoint to start a single Chord node.

# Bootstrap the first node (no known peer)
python run_node.py --host 127.0.0.1 --port 5001

# Join an existing ring via a known peer
python run_node.py --host 127.0.0.1 --port 5002 --join 127.0.0.1:5001

# Start with a worker (executes jobs locally) and agent AI
python run_node.py --host 127.0.0.1 --port 5001 --worker --workers 4

# Override strategy: heuristic only (no API key needed)
AGENT_STRATEGY=heuristic python run_node.py --host 127.0.0.1 --port 5001 --worker
"""

import argparse
import logging
from chord.server import start_node


def main():
    parser = argparse.ArgumentParser(description="Start a Chord DHT node")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, required=True, help="Bind port")
    parser.add_argument("--join", default=None, metavar="HOST:PORT",
                        help="Address of an existing ring node to join")
    parser.add_argument("--id", type=int, default=None,
                        help="Override node ID (default: SHA-1 hash of host:port)")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="Stabilization interval in seconds (default: 2.0)")
    parser.add_argument("--log", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Log level")

    # Worker options
    parser.add_argument("--worker", action="store_true",
                        help="Enable the local job worker (executes PENDING jobs)")
    parser.add_argument("--worker-interval", type=float, default=1.0,
                        help="Job scan interval for the worker thread (default: 1.0s)")
    parser.add_argument("--workers", type=int, default=4,
                        help="Thread pool size for job execution (default: 4)")

    # Agent options
    parser.add_argument("--agent-key", default=None,
                        help="Anthropic API key (overrides ANTHROPIC_API_KEY env var)")
    parser.add_argument("--agent-loop-interval", type=float, default=5.0,
                        help="Agent decision-loop polling interval in seconds (default: 5.0)")

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    start_node(
        host=args.host,
        port=args.port,
        known_address=args.join,
        node_id=args.id,
        maintenance_interval=args.interval,
        enable_worker=args.worker,
        worker_interval=args.worker_interval,
        worker_threads=args.workers,
        agent_key=args.agent_key,
        agent_loop_interval=args.agent_loop_interval,
    )


if __name__ == "__main__":
    main()
