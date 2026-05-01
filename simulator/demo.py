"""
End-to-end demo scenario for the Distributed Chord platform.

Demonstrates:
1. Multi-node setup with resource profiles
2. Job submission through service layer
3. Placement agent selecting execution nodes
4. Task execution and completion
5. Result retrieval
"""

import time
from typing import List, Dict
from simulator import VirtualNode, NodeProfile, StubServiceLayer, StubPlacementAgent


class DemoScenario:
    """Orchestrates an end-to-end demo of the platform."""
    
    def __init__(self, num_nodes: int = 5, num_jobs: int = 10):
        """
        Initialize demo scenario.
        
        Args:
            num_nodes: Number of virtual nodes to simulate
            num_jobs: Number of jobs to submit
        """
        self.num_nodes = num_nodes
        self.num_jobs = num_jobs
        self.nodes: List[VirtualNode] = []
        self.service_layer = StubServiceLayer()
        self.placement_agent = StubPlacementAgent(strategy="least_loaded")
        
        self.execution_log = []  # (job_id, node_id, placement_time, completion_time)
    
    def setup_nodes(self):
        """Create and register virtual nodes."""
        print(f"\n[Setup] Creating {self.num_nodes} virtual nodes...")
        
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
            self.nodes.append(node)
            self.service_layer.register_node(i, node)
            print(f"  Node {i}: {profile.cpu_capacity} CPU, {profile.memory_capacity} GB RAM")
        
        print(f"✓ {self.num_nodes} nodes ready\n")
    
    def submit_jobs(self):
        """Submit jobs and place them using the placement agent."""
        print(f"[Execution] Submitting {self.num_jobs} jobs...")
        
        job_specs = [
            {"cpu": 0.5, "memory": 1.0, "priority": "low"},
            {"cpu": 1.0, "memory": 2.0, "priority": "medium"},
            {"cpu": 2.0, "memory": 4.0, "priority": "high"},
        ]
        
        placement_decisions = {}  # job_id -> node_id
        
        for job_id in range(self.num_jobs):
            # Vary job requirements
            spec = job_specs[job_id % len(job_specs)]
            
            # Submit job to service layer
            self.service_layer.put_task(f"job_{job_id}", {
                "cpu_required": spec["cpu"],
                "memory_required": spec["memory"],
                "priority": spec["priority"],
                "submitted_at": time.time(),
            })
            
            # Get placement decision from agent
            selected_node_id = self.placement_agent.select_placement(
                self.nodes, f"job_{job_id}", 
                {"cpu": spec["cpu"], "memory": spec["memory"]}
            )
            
            if selected_node_id is None:
                print(f"  ✗ job_{job_id}: No suitable node (placement failed)")
                continue
            
            # Submit task to selected node
            node = self.nodes[selected_node_id]
            success = node.submit_task(f"job_{job_id}", spec["cpu"], spec["memory"])
            
            if success:
                placement_decisions[f"job_{job_id}"] = selected_node_id
                print(f"  ✓ job_{job_id}: → Node {selected_node_id} "
                      f"({spec['cpu']} CPU, {spec['memory']} GB RAM)")
            else:
                print(f"  ✗ job_{job_id}: Submission to Node {selected_node_id} failed")
        
        print(f"✓ {len(placement_decisions)} of {self.num_jobs} jobs placed\n")
        return placement_decisions
    
    def simulate_execution(self, placement_decisions: Dict):
        """Simulate job execution on placed nodes."""
        print("[Execution] Simulating job execution...")
        
        # Simulate execution with simple timing
        execution_times = {
            "low": 0.1,
            "medium": 0.2,
            "high": 0.3,
        }
        
        for job_id, node_id in placement_decisions.items():
            task_metadata = self.service_layer.get_task(job_id)
            priority = task_metadata.get("priority", "medium")
            sim_time = execution_times[priority]
            
            # Simulate work
            time.sleep(sim_time)
            
            # Task completes
            node = self.nodes[node_id]
            node.complete_task(job_id)
            node.tasks_executed += 1
            
            self.execution_log.append({
                "job_id": job_id,
                "node_id": node_id,
                "priority": priority,
                "execution_time_ms": sim_time * 1000,
            })
        
        print(f"✓ {len(self.execution_log)} jobs executed\n")
    
    def print_results(self):
        """Print execution results and node state."""
        print("[Results] Final Node State:")
        print("-" * 80)
        for node in self.nodes:
            state = node.get_state_dict()
            print(f"  Node {state['node_id']}: "
                  f"CPU {state['cpu_utilization_pct']:.1f}% | "
                  f"Memory {state['memory_utilization_pct']:.1f}% | "
                  f"Queue: {state['queue_size']} | "
                  f"Executed: {state['tasks_executed']}")
        print("-" * 80)
        print()
        
        print("[Results] Job Execution Summary:")
        print("-" * 80)
        total_time = sum(log["execution_time_ms"] for log in self.execution_log)
        avg_time = total_time / len(self.execution_log) if self.execution_log else 0
        
        print(f"  Total jobs executed: {len(self.execution_log)}")
        print(f"  Total execution time: {total_time:.1f} ms")
        print(f"  Average job time: {avg_time:.1f} ms")
        print("-" * 80)
        print()
    
    def run(self):
        """Execute the full demo scenario."""
        print("\n" + "=" * 80)
        print("END-TO-END DEMO: Chord Edge Compute Platform")
        print("=" * 80)
        
        self.setup_nodes()
        placement_decisions = self.submit_jobs()
        self.simulate_execution(placement_decisions)
        self.print_results()
        
        print("=" * 80)
        print("Demo Complete\n")
        
        return {
            "nodes": self.nodes,
            "execution_log": self.execution_log,
            "service_layer": self.service_layer,
        }


def run_demo(num_nodes: int = 5, num_jobs: int = 10):
    """Convenience function to run demo scenario."""
    demo = DemoScenario(num_nodes=num_nodes, num_jobs=num_jobs)
    return demo.run()


if __name__ == "__main__":
    run_demo(num_nodes=5, num_jobs=10)
