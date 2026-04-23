# Simulator — IoT/Edge Node Simulator

Lightweight simulator for testing and evaluating the Chord platform with virtual nodes.

## Components

### VirtualNode
Simulated edge/IoT node with:
- Resource profiles (CPU, memory, disk, latency)
- State tracking (healthy/failed/recovering)
- Load and task queue management
- Metrics collection

### StubServiceLayer
Placeholder for Member 2's Distributed Service Layer.
- Node registration/discovery
- Task metadata storage and retrieval
- Will be replaced with DHT-backed implementation

### StubPlacementAgent
Baseline strategies for task placement (for evaluation comparison).
- **random**: Pick a random healthy node
- **round_robin**: Cycle through nodes
- **least_loaded**: Pick node with lowest CPU utilization
- Stub replication and recovery strategies

### DemoScenario
End-to-end workflow demonstrating:
- Multi-node setup with resource profiles
- Job submission and placement decisions
- Execution simulation
- Results collection and reporting

## Usage

### Running the Demo

```bash
# Default: 5 nodes, 10 jobs
python3 run_demo.py

# Custom configuration
python3 run_demo.py --nodes 10 --jobs 50
```

### Programmatic Usage

```python
from simulator import VirtualNode, NodeProfile, StubServiceLayer, StubPlacementAgent

# Create nodes with resource profiles
profile = NodeProfile(cpu_capacity=4.0, memory_capacity=8.0, 
                      disk_capacity=100.0, network_latency_ms=10.0)
node = VirtualNode(node_id=10, profile=profile)

# Submit a task
node.submit_task(task_id="task_1", cpu_required=1.0, memory_required=2.0)

# Check state
print(node.get_state_dict())

# Use placement agent
agent = StubPlacementAgent(strategy="least_loaded")
nodes = [node1, node2, node3]
selected_node_id = agent.select_placement(nodes, "task_1", 
                                          {"cpu": 1.0, "memory": 2.0})
```

## Integration Points

- **Member 1 (Chord Core)**: VirtualNode can wrap a ChordNode instance
- **Member 2 (Service Layer)**: Replace StubServiceLayer with real implementation
- **Member 3 (AI Agent)**: Replace StubPlacementAgent with real agents
