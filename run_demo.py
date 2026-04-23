"""
Run the end-to-end demo scenario.

Usage:
    python run_demo.py                  # Default: 5 nodes, 10 jobs
    python run_demo.py --nodes 10 --jobs 20   # Custom configuration
"""

import argparse
from simulator import run_demo


def main():
    parser = argparse.ArgumentParser(description="Run end-to-end demo scenario")
    parser.add_argument("--nodes", type=int, default=5,
                       help="Number of virtual nodes (default: 5)")
    parser.add_argument("--jobs", type=int, default=10,
                       help="Number of jobs to submit (default: 10)")
    
    args = parser.parse_args()
    run_demo(num_nodes=args.nodes, num_jobs=args.jobs)


if __name__ == "__main__":
    main()
