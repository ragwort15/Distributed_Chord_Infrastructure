"""
Service Layer and Placement Agent implementations for the simulator.

Provides:
- In-memory task storage for demo/testing
- Real PlacementAgent from Member 3 with fallback strategies
"""

import random
import logging
from typing import List, Dict, Optional

from simulator.virtual_node import VirtualNode

logger = logging.getLogger(__name__)


class StubServiceLayer:
    """
    Service Layer for simulator - in-memory task storage.
    
    In production, this would wrap Member 2's TaskService (DHT-backed storage).
    For demo/testing, maintains simple in-memory metadata.
    """
    
    def __init__(self):
        """Initialize service layer."""
        self.tasks_registry = {}  # task_id -> task_metadata
        self.nodes_registry = {}  # node_id -> node_info
    
    def register_node(self, node_id: int, node: VirtualNode):
        """Register a node with the service layer."""
        self.nodes_registry[node_id] = {
            "node_id": node_id,
            "cpu_capacity": node.profile.cpu_capacity,
            "memory_capacity": node.profile.memory_capacity,
            "disk_capacity": node.profile.disk_capacity,
            "is_healthy": node.is_healthy(),
        }
        logger.debug(f"Registered node {node_id}")
    
    def deregister_node(self, node_id: int):
        """Deregister a node (e.g., on failure)."""
        if node_id in self.nodes_registry:
            del self.nodes_registry[node_id]
            logger.debug(f"Deregistered node {node_id}")
    
    def put_task(self, task_id: str, task_metadata: Dict):
        """Store task metadata."""
        self.tasks_registry[task_id] = task_metadata
        logger.debug(f"Stored task {task_id}")
    
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
    Placement Agent for simulator - provides baseline strategies.
    
    Strategies: random, round_robin, least_loaded
    In production, would integrate with Member 3's real agent for intelligent placement.
    """
    
    STRATEGIES = ["random", "round_robin", "least_loaded"]
    
    def __init__(self, strategy: str = "least_loaded"):
        """
        Initialize placement agent.
        
        Args:
            strategy: "random", "round_robin", or "least_loaded"
        """
        if strategy not in self.STRATEGIES:
            raise ValueError(f"Unknown strategy: {strategy}. Use {self.STRATEGIES}")
        self.strategy = strategy
        self.round_robin_index = 0
        logger.info(f"Initialized PlacementAgent with strategy: {strategy}")
    
    def select_placement(self, nodes: List[VirtualNode], task_id: str, 
                        task_requirements: Dict) -> Optional[int]:
        """
        Select a node for task placement using configured strategy.
        
        Args:
            nodes: List of available VirtualNode instances
            task_id: Task identifier
            task_requirements: {"cpu": float, "memory": float}
            
        Returns:
            Node ID of selected node, or None if no suitable node
        """
        healthy_nodes = [n for n in nodes if n.is_healthy()]
        if not healthy_nodes:
            logger.warning(f"No healthy nodes available for task {task_id}")
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
            logger.warning(f"No capable nodes for task {task_id} "
                          f"(cpu_req={cpu_req}, mem_req={mem_req})")
            return None
        
        # Use configured strategy
        if self.strategy == "random":
            selected = random.choice(capable_nodes)
        elif self.strategy == "round_robin":
            idx = self.round_robin_index % len(capable_nodes)
            self.round_robin_index += 1
            selected = capable_nodes[idx]
        elif self.strategy == "least_loaded":
            selected = min(capable_nodes, key=lambda n: n.get_cpu_utilization())
        else:
            selected = random.choice(capable_nodes)
        
        logger.debug(f"Placement ({self.strategy}) selected node {selected.node_id} for task {task_id}")
        return selected.node_id
    
    def decide_replication(self, task_id: str, task_priority: str) -> int:
        """
        Decide replication factor based on task priority.
        
        Args:
            task_id: Task identifier
            task_priority: "low", "medium", or "high"
            
        Returns:
            Number of replicas
        """
        replica_map = {"low": 1, "medium": 2, "high": 3}
        replicas = replica_map.get(task_priority, 1)
        logger.debug(f"Replication decision for {task_id}: {replicas} replicas")
        return replicas
    
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
            recovery_node = min(healthy_nodes, 
                               key=lambda n: n.get_cpu_utilization())
            logger.info(f"Recovery from failed node {failed_node_id} to {recovery_node.node_id}")
            return recovery_node.node_id
        logger.error(f"No healthy nodes for recovery from {failed_node_id}")
        return None
