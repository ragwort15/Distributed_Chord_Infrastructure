"""
Fault injection testing for the Chord platform.

Simulates node failures mid-execution and measures recovery time.
Scenarios: single node failure, cascading failures.
"""

import time
from typing import List, Callable, Dict, Optional
from dataclasses import dataclass, asdict
from simulator import VirtualNode, StubPlacementAgent


@dataclass
class FaultEvent:
    """Record of a fault injection event."""
    fault_id: str
    node_id: int
    failure_time: float
    failure_type: str  # "single", "cascading"
    
    # Recovery tracking
    recovery_start: Optional[float] = None
    recovery_end: Optional[float] = None
    
    def recovery_duration_ms(self) -> Optional[float]:
        """Get recovery duration in milliseconds."""
        if self.recovery_start and self.recovery_end:
            return (self.recovery_end - self.recovery_start) * 1000
        return None
    
    def time_to_recovery_start_ms(self) -> float:
        """Get time from failure to start of recovery in ms."""
        if self.recovery_start:
            return (self.recovery_start - self.failure_time) * 1000
        return -1


class FaultInjectionTester:
    """Injects faults and measures recovery in the simulation."""
    
    def __init__(self, nodes: List[VirtualNode], 
                 placement_agent: StubPlacementAgent,
                 service_layer):
        """
        Initialize fault injection tester.
        
        Args:
            nodes: List of VirtualNode instances to test
            placement_agent: StubPlacementAgent for recovery decisions
            service_layer: StubServiceLayer for node registry
        """
        self.nodes = nodes
        self.placement_agent = placement_agent
        self.service_layer = service_layer
        self.fault_events: List[FaultEvent] = []
        self.fault_counter = 0
    
    def _get_next_fault_id(self) -> str:
        """Generate unique fault ID."""
        self.fault_counter += 1
        return f"fault_{self.fault_counter}"
    
    def inject_single_node_failure(self, node_id: int, 
                                   task_on_node: Optional[str] = None) -> FaultEvent:
        """
        Inject a single node failure.
        
        Args:
            node_id: ID of node to fail
            task_on_node: Optional task ID running on failed node (for recovery tracking)
            
        Returns:
            FaultEvent with failure recorded
        """
        fault_id = self._get_next_fault_id()
        node = self.nodes[node_id]
        
        failure_time = time.time()
        
        # Fail the node
        node.fail()
        self.service_layer.deregister_node(node_id)
        
        event = FaultEvent(
            fault_id=fault_id,
            node_id=node_id,
            failure_time=failure_time,
            failure_type="single",
        )
        
        # Attempt recovery if there's a task
        if task_on_node:
            recovery_node_id = self.placement_agent.decide_recovery(node_id, self.nodes)
            if recovery_node_id is not None:
                event.recovery_start = time.time()
                # Recovery completed immediately in stub
                event.recovery_end = time.time()
        
        self.fault_events.append(event)
        return event
    
    def inject_cascading_failures(self, num_nodes: int) -> List[FaultEvent]:
        """
        Inject cascading failures (multiple nodes failing in sequence).
        
        Args:
            num_nodes: Number of nodes to fail sequentially
            
        Returns:
            List of FaultEvent records
        """
        events = []
        healthy_nodes = [n for n in self.nodes if n.is_healthy()]
        
        for i in range(min(num_nodes, len(healthy_nodes))):
            node_to_fail = healthy_nodes[i]
            event = self.inject_single_node_failure(node_to_fail.node_id)
            events.append(event)
            
            # Small delay between failures
            time.sleep(0.01)
        
        return events
    
    def recover_node(self, node_id: int) -> None:
        """
        Mark a node as recovered.
        
        Args:
            node_id: ID of node to recover
        """
        node = self.nodes[node_id]
        node.recover()
        self.service_layer.register_node(node_id, node)
        
        # Update matching fault event
        for event in reversed(self.fault_events):
            if event.node_id == node_id and event.recovery_end is None:
                event.recovery_end = time.time()
                break
    
    def print_fault_report(self):
        """Print summary of fault injection test results."""
        if not self.fault_events:
            print("[Fault Injection] No faults injected\n")
            return
        
        print("[Fault Injection] Fault Report")
        print("-" * 80)
        
        for event in self.fault_events:
            print(f"  {event.fault_id} ({event.failure_type}): Node {event.node_id}")
            print(f"    Failure time: {event.failure_time:.3f}")
            
            if event.recovery_start:
                print(f"    Recovery start: {event.recovery_start:.3f} "
                      f"(+{event.time_to_recovery_start_ms():.1f} ms)")
            
            if event.recovery_duration_ms() is not None:
                print(f"    Recovery duration: {event.recovery_duration_ms():.1f} ms")
            else:
                print(f"    Recovery: Not yet recovered")
        
        print("-" * 80)
        print()
    
    def get_recovery_metrics(self) -> Dict:
        """Get aggregated recovery metrics."""
        if not self.fault_events:
            return {}
        
        recoveries = [e for e in self.fault_events if e.recovery_duration_ms() is not None]
        
        if not recoveries:
            return {
                "total_faults": len(self.fault_events),
                "recovered": 0,
            }
        
        recovery_times = [e.recovery_duration_ms() for e in recoveries]
        
        return {
            "total_faults": len(self.fault_events),
            "recovered": len(recoveries),
            "avg_recovery_time_ms": sum(recovery_times) / len(recovery_times),
            "min_recovery_time_ms": min(recovery_times),
            "max_recovery_time_ms": max(recovery_times),
        }
