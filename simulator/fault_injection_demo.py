"""
Fault injection test scenarios for evaluation.

Demonstrates:
1. Single node failure and recovery
2. Cascading failures
3. Recovery measurement
4. Task re-routing to healthy nodes
"""

import time
from typing import Dict, List
from simulator import VirtualNode, NodeProfile, StubServiceLayer, StubPlacementAgent
from simulator.fault_injection import FaultInjectionTester


class FaultInjectionScenario:
    """Run fault injection test scenarios."""
    
    def __init__(self, num_nodes: int = 5):
        """Initialize scenario with virtual nodes."""
        self.num_nodes = num_nodes
        self.nodes: List[VirtualNode] = []
        self.service_layer = StubServiceLayer()
        self.placement_agent = StubPlacementAgent(strategy="least_loaded")
        self.tester = FaultInjectionTester(self.nodes, self.placement_agent, 
                                          self.service_layer)
    
    def setup(self):
        """Create nodes for testing."""
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
    
    def run_single_failure_test(self):
        """Test single node failure and recovery."""
        print("\n[Test 1] Single Node Failure")
        print("-" * 80)
        
        # Submit jobs
        print("Submitting jobs to nodes...")
        for job_id in range(8):
            node_id = job_id % self.num_nodes
            node = self.nodes[node_id]
            node.submit_task(f"job_{job_id}", cpu_required=0.5, memory_required=1.0)
            print(f"  job_{job_id} → Node {node_id}")
        
        # Inject failure
        failed_node = 1
        print(f"\nInjecting failure: Node {failed_node}")
        event = self.tester.inject_single_node_failure(failed_node, task_on_node="job_1")
        
        # Simulate recovery
        time.sleep(0.1)
        self.tester.recover_node(failed_node)
        print(f"Node {failed_node} recovered")
        
        self.tester.print_fault_report()
        return self.tester.get_recovery_metrics()
    
    def run_cascading_failure_test(self):
        """Test cascading failures."""
        print("\n[Test 2] Cascading Failures")
        print("-" * 80)
        
        # Reset nodes
        for node in self.nodes:
            if not node.is_healthy():
                self.tester.recover_node(node.node_id)
        
        # Submit jobs
        print("Submitting jobs...")
        for job_id in range(10):
            node_id = job_id % self.num_nodes
            node = self.nodes[node_id]
            node.submit_task(f"job_cascade_{job_id}", cpu_required=0.5, memory_required=1.0)
        
        # Inject cascading failures
        print(f"Injecting cascading failures (3 nodes)...")
        events = self.tester.inject_cascading_failures(num_nodes=3)
        
        # Simulate recovery
        time.sleep(0.1)
        for event in events:
            self.tester.recover_node(event.node_id)
        
        self.tester.print_fault_report()
        return self.tester.get_recovery_metrics()
    
    def run_all_tests(self):
        """Run all fault injection tests."""
        print("\n" + "=" * 80)
        print("FAULT INJECTION TEST SUITE")
        print("=" * 80)
        
        self.setup()
        
        metrics1 = self.run_single_failure_test()
        metrics2 = self.run_cascading_failure_test()
        
        print("\n" + "=" * 80)
        print("SUMMARY")
        print("=" * 80)
        
        print("\nTest 1 - Single Failure:")
        for key, value in metrics1.items():
            print(f"  {key}: {value}")
        
        print("\nTest 2 - Cascading Failures:")
        for key, value in metrics2.items():
            print(f"  {key}: {value}")
        
        print("\n" + "=" * 80)


def run_fault_injection_tests(num_nodes: int = 5):
    """Convenience function to run fault injection tests."""
    scenario = FaultInjectionScenario(num_nodes=num_nodes)
    scenario.run_all_tests()


if __name__ == "__main__":
    run_fault_injection_tests(num_nodes=5)
