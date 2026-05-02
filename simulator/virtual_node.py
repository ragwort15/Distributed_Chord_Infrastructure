"""
Virtual Node class representing an edge/IoT node in the simulator.

Each VirtualNode has:
- Resource profile (CPU, memory, disk capacity)
- State tracking (healthy/failed, load, queue)
- Chord node integration (uses Member 1's ChordNode)
"""

from dataclasses import dataclass
from enum import Enum
import time


class NodeState(Enum):
    """Current state of a virtual node."""
    HEALTHY = "healthy"
    FAILED = "failed"
    RECOVERING = "recovering"


@dataclass
class NodeProfile:
    """Resource profile for a virtual node."""
    cpu_capacity: float      # CPU cores available
    memory_capacity: float   # Memory in GB
    disk_capacity: float     # Disk in GB
    network_latency_ms: float  # Base network latency in ms
    
    def __post_init__(self):
        """Validate resource values."""
        if self.cpu_capacity <= 0:
            raise ValueError("cpu_capacity must be > 0")
        if self.memory_capacity <= 0:
            raise ValueError("memory_capacity must be > 0")
        if self.disk_capacity <= 0:
            raise ValueError("disk_capacity must be > 0")
        if self.network_latency_ms < 0:
            raise ValueError("network_latency_ms must be >= 0")


class VirtualNode:
    """
    Represents a simulated edge/IoT node in the platform.
    
    - Integrates with Chord DHT (Member 1)
    - Tracks resource usage and state
    - Can be failed/recovered for testing
    """
    
    def __init__(self, node_id: int, profile: NodeProfile, chord_node=None):
        """
        Initialize a virtual node.
        
        Args:
            node_id: Unique node identifier (0-2^M-1)
            profile: NodeProfile with resource specifications
            chord_node: Optional ChordNode instance from Member 1 (used in full integration)
        """
        self.node_id = node_id
        self.profile = profile
        self.chord_node = chord_node
        
        # State
        self.state = NodeState.HEALTHY
        self.failed_at = None
        
        # Load tracking
        self.cpu_load = 0.0  # current CPU usage (0.0 to capacity)
        self.memory_load = 0.0  # current memory usage (0.0 to capacity)
        self.task_queue = []  # list of (task_id, task_metadata)
        
        # Metrics
        self.tasks_executed = 0
        self.tasks_failed = 0
        self.created_at = time.time()
    
    def fail(self):
        """Mark this node as failed."""
        self.state = NodeState.FAILED
        self.failed_at = time.time()
    
    def recover(self):
        """Mark this node as recovered."""
        self.state = NodeState.HEALTHY
        self.failed_at = None
    
    def is_healthy(self) -> bool:
        """Check if node is in healthy state."""
        return self.state == NodeState.HEALTHY
    
    def get_cpu_utilization(self) -> float:
        """Get CPU utilization as percentage (0-100)."""
        return (self.cpu_load / self.profile.cpu_capacity) * 100.0
    
    def get_memory_utilization(self) -> float:
        """Get memory utilization as percentage (0-100)."""
        return (self.memory_load / self.profile.memory_capacity) * 100.0
    
    def submit_task(self, task_id: str, cpu_required: float, memory_required: float):
        """
        Submit a task to this node's queue.
        
        Args:
            task_id: Unique task identifier
            cpu_required: CPU cores needed
            memory_required: Memory in GB needed
            
        Returns:
            bool: True if task was queued, False if would exceed capacity
        """
        if self.cpu_load + cpu_required > self.profile.cpu_capacity:
            return False
        if self.memory_load + memory_required > self.profile.memory_capacity:
            return False
        
        self.task_queue.append({
            "task_id": task_id,
            "cpu_required": cpu_required,
            "memory_required": memory_required,
            "submitted_at": time.time(),
        })
        self.cpu_load += cpu_required
        self.memory_load += memory_required
        return True
    
    def complete_task(self, task_id: str):
        """
        Mark a task as completed and free resources.
        
        Args:
            task_id: Task identifier to complete
        """
        for task in self.task_queue:
            if task["task_id"] == task_id:
                self.cpu_load -= task["cpu_required"]
                self.memory_load -= task["memory_required"]
                self.task_queue.remove(task)
                self.tasks_executed += 1
                return
    
    def get_state_dict(self) -> dict:
        """Return full state as a dictionary for serialization/logging."""
        return {
            "node_id": self.node_id,
            "state": self.state.value,
            "cpu_utilization_pct": round(self.get_cpu_utilization(), 2),
            "memory_utilization_pct": round(self.get_memory_utilization(), 2),
            "queue_size": len(self.task_queue),
            "tasks_executed": self.tasks_executed,
            "tasks_failed": self.tasks_failed,
            "uptime_seconds": time.time() - self.created_at,
        }
