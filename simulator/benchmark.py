"""
Benchmarking framework for comparing placement strategies.

Runs multiple scenarios with different strategies and collects comparative metrics.
"""

import time
from typing import List
from simulator import VirtualNode, NodeProfile, StubServiceLayer, StubPlacementAgent
from simulator.metrics import MetricsCollector, PlacementMetric, ExecutionMetric


class StrategyBenchmark:
    """Benchmark different placement strategies."""
    
    STRATEGIES = ["random", "round_robin", "least_loaded"]
    
    def __init__(self, num_nodes: int = 5, num_jobs: int = 20):
        """
        Initialize benchmark.
        
        Args:
            num_nodes: Number of virtual nodes
            num_jobs: Number of jobs to submit per run
        """
        self.num_nodes = num_nodes
        self.num_jobs = num_jobs
        self.metrics_collector = MetricsCollector()
    
    def _setup_nodes(self) -> tuple[List[VirtualNode], StubServiceLayer]:
        """Create nodes and service layer."""
        nodes = []
        service_layer = StubServiceLayer()
        
        profiles = [
            NodeProfile(cpu_capacity=4.0, memory_capacity=8.0, 
                       disk_capacity=100.0, network_latency_ms=5.0),
            NodeProfile(cpu_capacity=2.0, memory_capacity=4.0, 
                       disk_capacity=50.0, network_latency_ms=10.0),
            NodeProfile(cpu_capacity=8.0, memory_capacity=16.0, 
                       disk_capacity=200.0, network_latency_ms=3.0),
            NodeProfile(cpu_capacity=1.0, memory_capacity=2.0, 
                       disk_capacity=25.0, network_latency_ms=15.0),
            NodeProfile(cpu_capacity=6.0, memory_capacity=12.0, 
                       disk_capacity=150.0, network_latency_ms=8.0),
        ]
        
        for i in range(self.num_nodes):
            profile = profiles[i % len(profiles)]
            node = VirtualNode(node_id=i, profile=profile)
            nodes.append(node)
            service_layer.register_node(i, node)
        
        return nodes, service_layer
    
    def run_benchmark_with_strategy(self, strategy: str, run_id: str) -> dict:
        """
        Run benchmark with a specific placement strategy.
        
        Args:
            strategy: "random", "round_robin", or "least_loaded"
            run_id: Unique identifier for this run
            
        Returns:
            Summary metrics dict
        """
        nodes, service_layer = self._setup_nodes()
        agent = StubPlacementAgent(strategy=strategy)
        
        # Start metrics collection
        self.metrics_collector.start_run(run_id, strategy, self.num_nodes, self.num_jobs)
        
        job_specs = [
            {"cpu": 0.5, "memory": 1.0, "priority": "low"},
            {"cpu": 1.0, "memory": 2.0, "priority": "medium"},
            {"cpu": 2.0, "memory": 4.0, "priority": "high"},
        ]
        
        placement_count = 0
        
        # Submit and place jobs
        for job_id in range(self.num_jobs):
            spec = job_specs[job_id % len(job_specs)]
            
            # Record placement metric
            placement_start = time.time()
            selected_node_id = agent.select_placement(nodes, f"job_{job_id}",
                                                     {"cpu": spec["cpu"], 
                                                      "memory": spec["memory"]})
            placement_time = (time.time() - placement_start) * 1000  # ms
            
            if selected_node_id is None:
                self.metrics_collector.record_placement(
                    f"job_{job_id}", placement_time, -1,
                    spec["cpu"], spec["memory"],
                    success=False, reason="no_capacity"
                )
                continue
            
            # Submit to node
            node = nodes[selected_node_id]
            success = node.submit_task(f"job_{job_id}", spec["cpu"], spec["memory"])
            
            if success:
                placement_count += 1
                self.metrics_collector.record_placement(
                    f"job_{job_id}", placement_time, selected_node_id,
                    spec["cpu"], spec["memory"],
                    success=True
                )
        
        # Simulate execution and collect metrics
        execution_times = {"low": 0.05, "medium": 0.10, "high": 0.15}
        
        for job_id in range(min(placement_count, self.num_jobs)):
            spec = job_specs[job_id % len(job_specs)]
            selected_node_id = agent.select_placement(nodes, f"job_{job_id}",
                                                     {"cpu": spec["cpu"], 
                                                      "memory": spec["memory"]})
            
            if selected_node_id is None:
                continue
            
            node = nodes[selected_node_id]
            sim_time = execution_times[spec["priority"]]
            time.sleep(sim_time)
            
            # Record execution metric
            self.metrics_collector.record_execution(
                f"job_{job_id}",
                selected_node_id,
                sim_time * 1000,  # ms
                cpu_util=spec["cpu"],
                mem_util=spec["memory"]
            )
            
            node.complete_task(f"job_{job_id}")
        
        self.metrics_collector.end_run(run_id)
        return self.metrics_collector.get_run_summary(run_id)
    
    def run_all_strategies(self, runs_per_strategy: int = 2) -> dict:
        """
        Run benchmark for all strategies.
        
        Args:
            runs_per_strategy: Number of runs per strategy
            
        Returns:
            Comparison results
        """
        print("\n" + "=" * 80)
        print("BENCHMARK: Placement Strategy Comparison")
        print("=" * 80)
        print(f"Configuration: {self.num_nodes} nodes, {self.num_jobs} jobs per run")
        print(f"Runs per strategy: {runs_per_strategy}\n")
        
        for strategy in self.STRATEGIES:
            print(f"Testing strategy: {strategy}")
            for run in range(runs_per_strategy):
                run_id = f"{strategy}_run_{run+1}"
                summary = self.run_benchmark_with_strategy(strategy, run_id)
                
                placement_info = summary.get("placement", {})
                success_rate = placement_info.get("success_rate", 0)
                avg_time = placement_info.get("avg_placement_time_ms", 0)
                
                print(f"  Run {run+1}: {success_rate:.1f}% placements, "
                      f"{avg_time:.2f}ms avg placement time")
            print()
        
        # Compare strategies
        comparison = self.metrics_collector.compare_strategies()
        
        print("=" * 80)
        print("COMPARISON RESULTS")
        print("=" * 80)
        
        for strategy, metrics in comparison.items():
            print(f"\n{strategy.upper()}:")
            print(f"  Runs: {metrics['runs']}")
            print(f"  Avg placement success rate: {metrics['avg_placement_success_rate']}%")
            print(f"  Avg execution time: {metrics['avg_execution_time_ms']:.2f} ms")
            if metrics['avg_recovery_time_ms']:
                print(f"  Avg recovery time: {metrics['avg_recovery_time_ms']:.2f} ms")
        
        print("\n" + "=" * 80)
        
        return comparison


def run_benchmark(num_nodes: int = 5, num_jobs: int = 20, 
                 runs_per_strategy: int = 2):
    """Convenience function to run benchmarks."""
    benchmark = StrategyBenchmark(num_nodes=num_nodes, num_jobs=num_jobs)
    return benchmark.run_all_strategies(runs_per_strategy=runs_per_strategy)


if __name__ == "__main__":
    run_benchmark(num_nodes=5, num_jobs=20, runs_per_strategy=2)
