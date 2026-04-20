"""
CLI entrypoint to start a single Chord node.

# Bootstrap the first node (no known peer)
python run_node.py --host 127.0.0.1 --port 5001

# Join an existing ring via a known peer
python run_node.py --host 127.0.0.1 --port 5002 --join 127.0.0.1:5001

# Override the node ID
python run_node.py --host 127.0.0.1 --port 5003 --join 127.0.0.1:5001 --id 42
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
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    start_node(
        host=args.host,
        port=args.port,
        known_address=args.join,
        node_id=args.id,
        maintenance_interval=args.interval,
    )


if __name__ == "__main__":
    main()
