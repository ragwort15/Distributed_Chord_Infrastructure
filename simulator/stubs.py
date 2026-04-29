"""
Real implementations for Service Layer and Placement Agent.

Wraps Member 2 (TaskService) and Member 3 (PlacementAgent) implementations
for use in the simulator.
"""

import random
import logging
from typing import List, Dict, Optional

from simulator.virtual_node import VirtualNode
from storage.task_service import TaskService
from chord.agent import PlacementAgent as RealPlacementAgent

logger = logging.getLogger(__name__)


class StubServiceLayer:
    """
    Real Service Layer implementation using Member 2's TaskService.
    
    Manages:
    - Task registration and metadata
    - Node discovery and health tracking
    - Task lookup via DHT
    """
    
    def __init__(self):
        """Initialize service layer."""
        self.task_service = TaskService(replication_factor=3)
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
        """Store task metadata in the service."""
        try:
            self.task_service.put_task(task_id, task_metadata)
            logger.debug(f"Stored task {task_id}")
        except Exception as e:
            logger.warning(f"Failed to store task {task_id}: {e}")
    
    def get_task(self, task_id: str) -> Optional[Dict]:
        """Retrieve task metadata from the service."""
        try:
            return self.task_service.get_task(task_id)
        except Exception as e:
            logger.warning(f"Failed to get task {task_id}: {e}")
            return None
    
    def get_all_nodes(self) -> List[int]:
        """Get list of all registered nodes."""
        return list(self.nodes_registry.keys())
    
    def get_node_info(self, node_id: int) -> Optional[Dict]:
        """Get info about a specific node."""
        return self.nodes_registry.get(node_id)


class StubPlacementAgent:
    """
    Real Placement Agent implementation using Member 3's PlacementAgent.
    
    Provides:
    - Intelligent task placement decisions
    - Adaptive replication strategies
    - Failure recovery routing
    """
    
    STRATEGIES = ["random", "round_robin", "least_loaded"]
    
    def __init__(self, strategy: str = "least_loaded"):
        """
        Initialize placement agent.
        
        Args:
            strategy: "random", "round_robin", or "least_loaded"
                     (ignored when using real agent; kept for API compatibility)
        """
        if strategy not in self.STRATEGIES:
            raise ValueError(f"Unknown strategy: {strategy}. Use {self.STRATEGIES}")
        self.strategy = strategy
        self.round_robin_index = 0
        self.real_agent = RealPlacementAgent()
        logger.info(f"Initialized PlacementAgent with strategy: {strategy}")
    
    def select_placement(self, nodes: List[VirtualNode], task_id: str, 
                        task_requirements: Dict) -> Optional[int]:
        """
        Select a node for task placement using real agent when available,
        fallback to baseline strategies.
        
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
        
        # Try to use real agent; fallback to baseline strategies
        try:
            node_metrics = [
                {
                    "node_id": n.node_id,
                    "address": f"localhost:{5000 + n.node_id}",
                    "queue_depth": n.task_queue.__len__() if hasattr(n, 'task_queue') else 0,
                    "jobs_completed": n.jobs_completed,
                    "jobs_failed": n.jobs_failed,
                }
                for n in capable_nodes
            ]
            job = {"job_id": task_id, "type": "compute"}
            result = self.real_agent.select(job, node_metrics)
            selected_id = result.get("node_id")
            logger.debug(f"Agent selected node {selected_id} for task {task_id}")
            return selected_id
        except Exception as e:
            logger.debug(f"Real agent failed, using fallback strategy: {e}")
            # Fallback to baseline strategies
            return self._fallback_select(capable_nodes)
    
    def _fallback_select(self, capable_nodes: List[VirtualNode]) -> int:
        """Fallback baseline strategy when real agent unavailable."""
        if self.strategy == "random":
            return random.choice(capable_nodes).node_id
        elif self.strategy == "round_robin":
            idx = self.round_robin_index % len(capable_nodes)
            self.round_robin_index += 1
            return capable_nodes[idx].node_id
        elif self.strategy == "least_loaded":
            return min(capable_nodes, 
                      key=lambda n: n.get_cpu_utilization()).node_id
        return random.choice(capable_nodes).node_id
    
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
