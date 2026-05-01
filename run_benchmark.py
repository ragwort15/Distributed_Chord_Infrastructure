"""
Run performance metrics benchmarks.

Usage:
    python run_benchmark.py                         # Default: 5 nodes, 20 jobs, 2 runs
    python run_benchmark.py --nodes 10 --jobs 50 --runs 3   # Custom
"""

import argparse
from simulator.benchmark import run_benchmark


def main():
    parser = argparse.ArgumentParser(description="Run strategy comparison benchmarks")
    parser.add_argument("--nodes", type=int, default=5,
                       help="Number of virtual nodes (default: 5)")
    parser.add_argument("--jobs", type=int, default=20,
                       help="Number of jobs per run (default: 20)")
    parser.add_argument("--runs", type=int, default=2,
                       help="Runs per strategy (default: 2)")
    
    args = parser.parse_args()
    run_benchmark(num_nodes=args.nodes, num_jobs=args.jobs, 
                 runs_per_strategy=args.runs)


if __name__ == "__main__":
    main()
