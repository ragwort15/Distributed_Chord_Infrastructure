"""
Run fault injection test scenarios.

Usage:
    python run_fault_tests.py                  # Default: 5 nodes
    python run_fault_tests.py --nodes 10      # Custom node count
"""

import argparse
from simulator.fault_injection_demo import run_fault_injection_tests


def main():
    parser = argparse.ArgumentParser(description="Run fault injection test suite")
    parser.add_argument("--nodes", type=int, default=5,
                       help="Number of virtual nodes (default: 5)")
    
    args = parser.parse_args()
    run_fault_injection_tests(num_nodes=args.nodes)


if __name__ == "__main__":
    main()
