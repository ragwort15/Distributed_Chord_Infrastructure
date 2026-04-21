"""
IoT/Edge Node Simulator

Virtual nodes with configurable resource profiles for testing and evaluation.
Integrates with Chord ring and provides stubs for Service Layer and AI Agents.
"""

from simulator.virtual_node import VirtualNode, NodeProfile, NodeState
from simulator.stubs import StubServiceLayer, StubPlacementAgent

__all__ = [
    "VirtualNode",
    "NodeProfile",
    "NodeState",
    "StubServiceLayer",
    "StubPlacementAgent",
]
