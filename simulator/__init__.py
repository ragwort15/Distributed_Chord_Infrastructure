"""
IoT/Edge Node Simulator

Virtual nodes with configurable resource profiles for testing and evaluation.
Integrates with Chord ring and provides stubs for Service Layer and AI Agents.
"""

from simulator.virtual_node import VirtualNode, NodeProfile, NodeState
from simulator.stubs import StubServiceLayer, StubPlacementAgent
from simulator.demo import DemoScenario, run_demo
from simulator.fault_injection import FaultInjectionTester, FaultEvent
from simulator.metrics import MetricsCollector, RunMetrics, PlacementMetric

__all__ = [
    "VirtualNode",
    "NodeProfile",
    "NodeState",
    "StubServiceLayer",
    "StubPlacementAgent",
    "DemoScenario",
    "run_demo",
    "FaultInjectionTester",
    "FaultEvent",
    "MetricsCollector",
    "RunMetrics",
    "PlacementMetric",
]
