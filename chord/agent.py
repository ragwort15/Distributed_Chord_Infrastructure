"""
AI Agent System for the Chord DHT Job Execution Platform.

Three agents, one configurable strategy (llm | heuristic), unified decision log.

Agents:
  PlacementAgent    — selects optimal node based on load, latency, availability
  ReplicationAgent  — decides replica count and placement per task priority
  RecoveryAgent     — chooses recovery strategy when a node dies mid-execution
"""

import os
import json
import uuid
import time
import logging
import threading
from pathlib import Path

from chord.node import sha1_id

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Strategy selection: set AGENT_STRATEGY=llm (default) or =heuristic
# ---------------------------------------------------------------------------

STRATEGY = os.environ.get("AGENT_STRATEGY", "llm").lower()

# ---------------------------------------------------------------------------
# Decision logger
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()
_LOG_PATH = Path(os.environ.get("AGENT_LOG_PATH", "agent_decisions.jsonl"))


def _log_decision(agent: str, tool: str, inputs: dict, output: dict, strategy: str,
                  latency_ms: float):
    entry = {
        "ts": time.time(),
        "agent": agent,
        "tool": tool,
        "strategy": strategy,
        "inputs_summary": inputs,
        "decision": output,
        "latency_ms": round(latency_ms, 1),
    }
    with _log_lock:
        with _LOG_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    logger.debug(f"[DecisionLog] {agent}/{tool} → {output}")


# ---------------------------------------------------------------------------
# Stable system prompt (placed under prompt cache)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an intelligent orchestrator for a distributed job execution platform
built on a Chord DHT ring.

Your responsibilities:
1. Intelligent Task Placement — choose the node with the lowest queue depth and failure
   rate to execute a job.  Prefer nodes with fewer pending jobs.

2. Adaptive Replication — decide how many total copies a job needs and which nodes hold them.
   High-priority or long-running jobs warrant more replicas; spread across distinct nodes.
   Never exceed the number of available nodes.

3. Failure Recovery — when a node fails and leaves orphaned jobs, reassign each job to a
   surviving node.  Distribute load evenly; do NOT always pick the Chord successor.

You MUST respond by calling the tool provided — no prose.
"""

# ---------------------------------------------------------------------------
# Tool schemas (one per agent)
# ---------------------------------------------------------------------------

_PLACEMENT_TOOL = {
    "name": "select_placement_node",
    "description": (
        "Select the optimal DHT node to execute a job. "
        "Choose based on queue_depth (lower is better), jobs_failed, and availability."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "node_id": {"type": "integer",
                        "description": "node_id of the selected node"},
            "reasoning": {"type": "string",
                          "description": "Brief explanation of the choice"},
        },
        "required": ["node_id", "reasoning"],
    },
}

_REPLICATION_TOOL = {
    "name": "decide_replication_plan",
    "description": (
        "Decide how many replicas to create and which nodes hold them. "
        "Return the primary node plus zero or more replica node IDs."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "primary_node_id": {"type": "integer"},
            "replica_node_ids": {"type": "array", "items": {"type": "integer"},
                                 "description": "Additional nodes holding replicas"},
            "replication_factor": {"type": "integer",
                                   "description": "Total copies including primary"},
            "reasoning": {"type": "string"},
        },
        "required": ["primary_node_id", "replica_node_ids", "replication_factor", "reasoning"],
    },
}

_RECOVERY_TOOL = {
    "name": "plan_failure_recovery",
    "description": (
        "Reassign orphaned jobs from a failed node to surviving nodes. "
        "Balance load; do not blindly assign everything to the Chord successor."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "assignments": {
                "type": "object",
                "description": "Mapping of job_id → target_node_id",
                "additionalProperties": {"type": "integer"},
            },
            "reasoning": {"type": "string"},
        },
        "required": ["assignments", "reasoning"],
    },
}


# ---------------------------------------------------------------------------
# Helper: generate a key whose SHA-1 ring ID lands on a specific node
# ---------------------------------------------------------------------------

def make_job_key_for(target_node_id: int, ring_size: int = 256,
                     max_attempts: int = 512) -> str:
    """
    Brute-force a UUID s.t. sha1_id("job:<uuid>") == target_node_id.
    Falls back to a random key if the target is never hit.
    """
    for _ in range(max_attempts):
        candidate = str(uuid.uuid4())
        key = f"job:{candidate}"
        if sha1_id(key) % ring_size == target_node_id % ring_size:
            return key
    return f"job:{uuid.uuid4()}"


# ---------------------------------------------------------------------------
# Shared LLM caller (used by all three agents)
# ---------------------------------------------------------------------------

def _llm_call(client, tool_schema: dict, user_content: str) -> dict:
    """Single Claude API call with forced tool use. Returns tool input dict."""
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=1024,
        thinking={"type": "adaptive"},
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[tool_schema],
        tool_choice={"type": "tool", "name": tool_schema["name"]},
        messages=[{"role": "user", "content": user_content}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_schema["name"]:
            return block.input
    raise RuntimeError(f"No tool_use block returned for {tool_schema['name']}")


# ===========================================================================
# Agent 1 — PlacementAgent
# ===========================================================================

class PlacementAgent:
    """
    Selects the optimal Chord node to execute a submitted job.
    Signals: queue_depth, jobs_failed, node liveness.
    """

    NAME = "PlacementAgent"

    def __init__(self, client=None):
        self._client = client

    def select(self, job: dict, node_metrics: list[dict]) -> dict:
        """
        Returns {"node_id": int, "reasoning": str}.
        """
        if not node_metrics:
            raise ValueError("node_metrics is empty")

        t0 = time.perf_counter()
        strategy = STRATEGY

        if self._client and strategy == "llm":
            try:
                result = _llm_call(
                    self._client, _PLACEMENT_TOOL,
                    self._prompt(job, node_metrics),
                )
            except Exception as e:
                logger.warning(f"[PlacementAgent] LLM failed ({e}), using heuristic")
                result = self._heuristic(node_metrics)
                strategy = "heuristic_fallback"
        else:
            result = self._heuristic(node_metrics)
            strategy = "heuristic"

        _log_decision(
            agent=self.NAME,
            tool="select_placement_node",
            inputs={"job_id": job["job_id"], "job_type": job["type"],
                    "node_count": len(node_metrics)},
            output=result,
            strategy=strategy,
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
        return result

    def _prompt(self, job: dict, metrics: list[dict]) -> str:
        rows = "\n".join(
            f"  node_id={m['node_id']} address={m['address']} "
            f"queue_depth={m['queue_depth']} completed={m['jobs_completed']} "
            f"failed={m['jobs_failed']}"
            for m in metrics
        )
        return (
            f"Job to place: type={job['type']}, id={job['job_id']}\n\n"
            f"Available nodes:\n{rows}\n\n"
            "Call select_placement_node with your choice."
        )

    def _heuristic(self, metrics: list[dict]) -> dict:
        best = min(metrics, key=lambda m: (m["queue_depth"], m["jobs_failed"]))
        return {
            "node_id": best["node_id"],
            "reasoning": f"Lowest queue_depth={best['queue_depth']} on node {best['node_id']}",
        }


# ===========================================================================
# Agent 2 — ReplicationAgent
# ===========================================================================

class ReplicationAgent:
    """
    Decides adaptive replication: how many copies and where.
    Considers task priority (job_type), node health, and requested minimum.
    """

    NAME = "ReplicationAgent"

    # job types that warrant extra replicas
    _HIGH_PRIORITY = {"compute"}

    def __init__(self, client=None):
        self._client = client

    def plan(self, job: dict, node_metrics: list[dict],
             requested_replicas: int = 1) -> dict:
        """
        Returns {
            "primary_node_id": int,
            "replica_node_ids": [int, ...],
            "replication_factor": int,
            "reasoning": str
        }.
        """
        if not node_metrics:
            raise ValueError("node_metrics is empty")

        t0 = time.perf_counter()
        strategy = STRATEGY

        if self._client and strategy == "llm":
            try:
                result = _llm_call(
                    self._client, _REPLICATION_TOOL,
                    self._prompt(job, node_metrics, requested_replicas),
                )
            except Exception as e:
                logger.warning(f"[ReplicationAgent] LLM failed ({e}), using heuristic")
                result = self._heuristic(job, node_metrics, requested_replicas)
                strategy = "heuristic_fallback"
        else:
            result = self._heuristic(job, node_metrics, requested_replicas)
            strategy = "heuristic"

        _log_decision(
            agent=self.NAME,
            tool="decide_replication_plan",
            inputs={"job_id": job["job_id"], "job_type": job["type"],
                    "requested_replicas": requested_replicas,
                    "node_count": len(node_metrics)},
            output=result,
            strategy=strategy,
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
        return result

    def _prompt(self, job: dict, metrics: list[dict], requested: int) -> str:
        rows = "\n".join(
            f"  node_id={m['node_id']} queue_depth={m['queue_depth']} "
            f"failed={m['jobs_failed']}"
            for m in metrics
        )
        return (
            f"Job: type={job['type']}, id={job['job_id']}\n"
            f"Requested replication factor: {requested}\n\n"
            f"Available nodes:\n{rows}\n\n"
            "Decide the replication plan. You may adjust the factor based on job type "
            "and node health. Call decide_replication_plan."
        )

    def _heuristic(self, job: dict, metrics: list[dict], requested: int) -> dict:
        # Bump replication for high-priority types if we have the nodes
        effective = requested
        if job.get("type") in self._HIGH_PRIORITY and len(metrics) >= 2:
            effective = max(requested, 2)
        effective = min(effective, len(metrics))

        sorted_nodes = sorted(metrics, key=lambda m: (m["queue_depth"], m["jobs_failed"]))
        primary = sorted_nodes[0]["node_id"]
        replicas = [m["node_id"] for m in sorted_nodes[1:effective]]
        return {
            "primary_node_id": primary,
            "replica_node_ids": replicas,
            "replication_factor": effective,
            "reasoning": (
                f"Heuristic: factor={effective}, "
                f"{'boosted for ' + job['type'] if effective > requested else 'as requested'}"
            ),
        }


# ===========================================================================
# Agent 3 — RecoveryAgent
# ===========================================================================

class RecoveryAgent:
    """
    Chooses a recovery strategy when a node dies mid-execution.
    NOT 'rehash and move to successor' — balances across all survivors.
    """

    NAME = "RecoveryAgent"

    def __init__(self, client=None):
        self._client = client

    def recover(self, failed_node_id: int, orphaned_jobs: list[dict],
                surviving_nodes: list[dict]) -> dict:
        """
        Returns {"assignments": {job_id: node_id, ...}, "reasoning": str}.
        """
        if not orphaned_jobs:
            return {"assignments": {}, "reasoning": "No orphaned jobs"}
        if not surviving_nodes:
            return {"assignments": {}, "reasoning": "No surviving nodes — cannot recover"}

        t0 = time.perf_counter()
        strategy = STRATEGY

        if self._client and strategy == "llm":
            try:
                result = _llm_call(
                    self._client, _RECOVERY_TOOL,
                    self._prompt(failed_node_id, orphaned_jobs, surviving_nodes),
                )
            except Exception as e:
                logger.warning(f"[RecoveryAgent] LLM failed ({e}), using heuristic")
                result = self._heuristic(orphaned_jobs, surviving_nodes)
                strategy = "heuristic_fallback"
        else:
            result = self._heuristic(orphaned_jobs, surviving_nodes)
            strategy = "heuristic"

        _log_decision(
            agent=self.NAME,
            tool="plan_failure_recovery",
            inputs={"failed_node_id": failed_node_id,
                    "orphaned_count": len(orphaned_jobs),
                    "survivor_count": len(surviving_nodes)},
            output={
                "assignment_count": len(result.get("assignments", {})),
                "reasoning": result.get("reasoning", ""),
            },
            strategy=strategy,
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
        return result

    def _prompt(self, failed_id: int, jobs: list[dict],
                survivors: list[dict]) -> str:
        job_rows = "\n".join(
            f"  job_id={j['job_id']} type={j['type']} status={j['status']}"
            for j in jobs
        )
        node_rows = "\n".join(
            f"  node_id={m['node_id']} queue_depth={m['queue_depth']} "
            f"failed={m['jobs_failed']}"
            for m in survivors
        )
        return (
            f"Node {failed_id} has failed.\n\n"
            f"Orphaned jobs ({len(jobs)}):\n{job_rows}\n\n"
            f"Surviving nodes ({len(survivors)}):\n{node_rows}\n\n"
            "Reassign each orphaned job. Balance load; do NOT always pick the "
            "Chord successor. Call plan_failure_recovery."
        )

    def _heuristic(self, jobs: list[dict], survivors: list[dict]) -> dict:
        sorted_nodes = sorted(survivors, key=lambda m: (m["queue_depth"], m["jobs_failed"]))
        assignments = {}
        for i, job in enumerate(jobs):
            target = sorted_nodes[i % len(sorted_nodes)]["node_id"]
            assignments[job["job_id"]] = target
        return {
            "assignments": assignments,
            "reasoning": "Round-robin across survivors ordered by queue_depth",
        }


# ===========================================================================
# OrchestratorAgent — unified facade for server.py
# ===========================================================================

class OrchestratorAgent:
    """
    Facade that wires all three agents to a shared Anthropic client.
    Pass api_key=None to force heuristic mode.
    """

    def __init__(self, api_key: str = None):
        self._client = None
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if key and STRATEGY == "llm":
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=key)
                logger.info("[Orchestrator] Anthropic client ready (strategy=llm)")
            except ImportError:
                logger.warning("[Orchestrator] anthropic not installed — strategy=heuristic")

        self.placement = PlacementAgent(self._client)
        self.replication = ReplicationAgent(self._client)
        self.recovery = RecoveryAgent(self._client)

    # Convenience pass-throughs
    def select_placement(self, job, node_metrics):
        return self.placement.select(job, node_metrics)

    def decide_replication(self, job, node_metrics, requested_replicas=1):
        return self.replication.plan(job, node_metrics, requested_replicas)

    def plan_recovery(self, failed_node_id, orphaned_jobs, surviving_nodes):
        return self.recovery.recover(failed_node_id, orphaned_jobs, surviving_nodes)
