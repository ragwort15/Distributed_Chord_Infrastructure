"""
Stub implementations for Service Layer and Placement Agent.

These are minimal placeholders that will be replaced by Member 2 (Service Layer)
and Member 3 (AI Agent System) when their implementations are ready.
"""

import random
from typing import List, Dict, Optional
from simulator.virtual_node import VirtualNode


class StubServiceLayer:
    """
    Stub for Member 2's Distributed Service Layer.
    
    Will be replaced with:
    - DHT-backed metadata store
    - Task registration / replication logic
    - Lookup API
    
    For now: simple in-memory storage.
    """
    
    def __init__(self):
        """Initialize stub service layer."""
        self.nodes_registry = {}  # node_id -> node_info
        self.tasks_registry = {}  # task_id -> task_metadata
    
    def register_node(self, node_id: int, node: VirtualNode):
        """Register a node with its capabilities."""
        self.nodes_registry[node_id] = {
            "node_id": node_id,
            "cpu_capacity": node.profile.cpu_capacity,
            "memory_capacity": node.profile.memory_capacity,
            "disk_capacity": node.profile.disk_capacity,
            "is_healthy": node.is_healthy(),
        }
    
    def deregister_node(self, node_id: int):
        """Deregister a node (e.g., on failure)."""
        if node_id in self.nodes_registry:
            del self.nodes_registry[node_id]
    
    def put_task(self, task_id: str, task_metadata: Dict):
        """Store task metadata."""
        self.tasks_registry[task_id] = task_metadata
    
    def get_task(self, task_id: str) -> Optional[Dict]:
        """Retrieve task metadata."""
        return self.tasks_registry.get(task_id)
    
    def get_all_nodes(self) -> List[int]:
        """Get list of all registered nodes."""
        return list(self.nodes_registry.keys())
    
    def get_node_info(self, node_id: int) -> Optional[Dict]:
        """Get info about a specific node."""
        return self.nodes_registry.get(node_id)


class StubPlacementAgent:
    """
    Stub for Member 3's Placement Agent.
    
    Will be replaced with:
    - Intelligent task placement (load, latency, availability)
    - Adaptive replication decisions
    - Failure recovery strategies
    
    For now: simple baselines for comparison.
    """
    
    STRATEGIES = ["random", "round_robin", "least_loaded"]
    
    def __init__(self, strategy: str = "least_loaded"):
        """
        Initialize stub placement agent.
        
        Args:
            strategy: "random", "round_robin", or "least_loaded"
        """
        if strategy not in self.STRATEGIES:
            raise ValueError(f"Unknown strategy: {strategy}. Use {self.STRATEGIES}")
        self.strategy = strategy
        self.round_robin_index = 0
    
    def select_placement(self, nodes: List[VirtualNode], task_id: str, 
                        task_requirements: Dict) -> Optional[int]:
        """
        Select a node for task placement.
        
        Args:
            nodes: List of available VirtualNode instances
            task_id: Task identifier
            task_requirements: {"cpu": float, "memory": float}
            
        Returns:
            Node ID of selected node, or None if no suitable node
        """
        healthy_nodes = [n for n in nodes if n.is_healthy()]
        if not healthy_nodes:
            return None
        
        cpu_req = task_requirements.get("cpu", 1.0)
        mem_req = task_requirements.get("memory", 1.0)
        
        # Filter nodes that can fit the task
        capable_nodes = [
            n for n in healthy_nodes
            if n.cpu_load + cpu_req <= n.profile.cpu_capacity
            and n.memory_load + mem_req <= n.profile.memory_capacity
        ]
        
        if not capable_nodes:
            return None
        
        if self.strategy == "random":
            return random.choice(capable_nodes).node_id
        
        elif self.strategy == "round_robin":
            idx = self.round_robin_index % len(capable_nodes)
            self.round_robin_index += 1
            return capable_nodes[idx].node_id
        
        elif self.strategy == "least_loaded":
            # Pick node with lowest CPU utilization
            return min(capable_nodes, 
                      key=lambda n: n.get_cpu_utilization()).node_id
        
        return None
    
    def decide_replication(self, task_id: str, task_priority: str) -> int:
        """
        Decide how many replicas needed.
        
        Args:
            task_id: Task identifier
            task_priority: "low", "medium", or "high"
            
        Returns:
            Number of replicas (stub: 1 for low, 2 for medium, 3 for high)
        """
        replica_map = {"low": 1, "medium": 2, "high": 3}
        return replica_map.get(task_priority, 1)
    
    def decide_recovery(self, failed_node_id: int, 
                       nodes: List[VirtualNode]) -> Optional[int]:
        """
        Decide recovery strategy when a node fails.
        
        Args:
            failed_node_id: Node that failed
            nodes: List of remaining nodes
            
        Returns:
            Node ID to retry on, or None
        """
        healthy_nodes = [n for n in nodes if n.is_healthy()]
        if healthy_nodes:
            # Stub: retry on least-loaded healthy node
            return min(healthy_nodes, 
                      key=lambda n: n.get_cpu_utilization()).node_id
        return None
