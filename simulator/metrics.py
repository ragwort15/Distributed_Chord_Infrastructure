"""
Performance metrics collection framework.

Tracks metrics across simulation runs for evaluation and comparison.
"""

import time
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict
from statistics import mean, stdev


@dataclass
class PlacementMetric:
    """Single placement decision metric."""
    job_id: str
    placement_time_ms: float
    selected_node_id: int
    cpu_required: float
    memory_required: float
    success: bool
    reason: Optional[str] = None  # e.g., "no_capacity", "network_error"


@dataclass
class ExecutionMetric:
    """Single job execution metric."""
    job_id: str
    node_id: int
    execution_time_ms: float
    cpu_utilized: float
    memory_utilized: float


@dataclass
class RecoveryMetric:
    """Single recovery event metric."""
    fault_id: str
    failed_node_id: int
    time_to_recovery_start_ms: float
    recovery_duration_ms: Optional[float]
    recovery_success: bool


@dataclass
class RunMetrics:
    """Aggregated metrics for a single simulation run."""
    run_id: str
    strategy: str  # e.g., "random", "least_loaded"
    num_nodes: int
    num_jobs: int
    
    placements: List[PlacementMetric] = field(default_factory=list)
    executions: List[ExecutionMetric] = field(default_factory=list)
    recoveries: List[RecoveryMetric] = field(default_factory=list)
    
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    
    def add_placement(self, metric: PlacementMetric):
        """Record a placement metric."""
        self.placements.append(metric)
    
    def add_execution(self, metric: ExecutionMetric):
        """Record an execution metric."""
        self.executions.append(metric)
    
    def add_recovery(self, metric: RecoveryMetric):
        """Record a recovery metric."""
        self.recoveries.append(metric)
    
    def finalize(self):
        """Mark run as complete."""
        self.end_time = time.time()
    
    def get_summary(self) -> Dict:
        """Get summary statistics."""
        summary = {
            "run_id": self.run_id,
            "strategy": self.strategy,
            "num_nodes": self.num_nodes,
            "num_jobs": self.num_jobs,
            "total_runtime_seconds": self.end_time - self.start_time if self.end_time else None,
        }
        
        # Placement metrics
        if self.placements:
            placement_times = [p.placement_time_ms for p in self.placements]
            successful = sum(1 for p in self.placements if p.success)
            failed = len(self.placements) - successful
            
            summary["placement"] = {
                "total": len(self.placements),
                "successful": successful,
                "failed": failed,
                "success_rate": (successful / len(self.placements)) * 100 if self.placements else 0,
                "avg_placement_time_ms": mean(placement_times) if placement_times else 0,
                "max_placement_time_ms": max(placement_times) if placement_times else 0,
                "min_placement_time_ms": min(placement_times) if placement_times else 0,
            }
        
        # Execution metrics
        if self.executions:
            exec_times = [e.execution_time_ms for e in self.executions]
            cpu_utils = [e.cpu_utilized for e in self.executions]
            mem_utils = [e.memory_utilized for e in self.executions]
            
            summary["execution"] = {
                "total": len(self.executions),
                "avg_execution_time_ms": mean(exec_times),
                "max_execution_time_ms": max(exec_times),
                "min_execution_time_ms": min(exec_times),
                "avg_cpu_utilized": mean(cpu_utils),
                "avg_memory_utilized": mean(mem_utils),
            }
        
        # Recovery metrics
        if self.recoveries:
            recovery_times = [r.recovery_duration_ms for r in self.recoveries 
                            if r.recovery_duration_ms is not None]
            recovery_starts = [r.time_to_recovery_start_ms for r in self.recoveries]
            successful_recoveries = sum(1 for r in self.recoveries if r.recovery_success)
            
            summary["recovery"] = {
                "total_faults": len(self.recoveries),
                "successful_recoveries": successful_recoveries,
                "avg_time_to_recovery_start_ms": mean(recovery_starts) if recovery_starts else 0,
                "avg_recovery_duration_ms": mean(recovery_times) if recovery_times else None,
            }
        
        return summary


class MetricsCollector:
    """Collects and aggregates metrics across multiple runs."""
    
    def __init__(self):
        """Initialize metrics collector."""
        self.runs: Dict[str, RunMetrics] = {}
        self.active_run: Optional[RunMetrics] = None
    
    def start_run(self, run_id: str, strategy: str, num_nodes: int, num_jobs: int) -> RunMetrics:
        """Start a new metrics collection run."""
        run = RunMetrics(
            run_id=run_id,
            strategy=strategy,
            num_nodes=num_nodes,
            num_jobs=num_jobs,
        )
        self.runs[run_id] = run
        self.active_run = run
        return run
    
    def end_run(self, run_id: Optional[str] = None):
        """End the current or specified run."""
        target_run = self.runs.get(run_id) if run_id else self.active_run
        if target_run:
            target_run.finalize()
            self.active_run = None
    
    def record_placement(self, job_id: str, placement_time_ms: float, 
                        selected_node_id: int, cpu_req: float, mem_req: float,
                        success: bool, reason: Optional[str] = None):
        """Record a placement metric."""
        if self.active_run:
            metric = PlacementMetric(
                job_id=job_id,
                placement_time_ms=placement_time_ms,
                selected_node_id=selected_node_id,
                cpu_required=cpu_req,
                memory_required=mem_req,
                success=success,
                reason=reason,
            )
            self.active_run.add_placement(metric)
    
    def record_execution(self, job_id: str, node_id: int, 
                        execution_time_ms: float, cpu_util: float, mem_util: float):
        """Record an execution metric."""
        if self.active_run:
            metric = ExecutionMetric(
                job_id=job_id,
                node_id=node_id,
                execution_time_ms=execution_time_ms,
                cpu_utilized=cpu_util,
                memory_utilized=mem_util,
            )
            self.active_run.add_execution(metric)
    
    def record_recovery(self, fault_id: str, failed_node_id: int,
                       time_to_start_ms: float, duration_ms: Optional[float],
                       success: bool):
        """Record a recovery metric."""
        if self.active_run:
            metric = RecoveryMetric(
                fault_id=fault_id,
                failed_node_id=failed_node_id,
                time_to_recovery_start_ms=time_to_start_ms,
                recovery_duration_ms=duration_ms,
                recovery_success=success,
            )
            self.active_run.add_recovery(metric)
    
    def get_run_summary(self, run_id: str) -> Optional[Dict]:
        """Get summary for a specific run."""
        run = self.runs.get(run_id)
        return run.get_summary() if run else None
    
    def compare_strategies(self) -> Dict:
        """Compare metrics across different strategies."""
        strategies = {}
        
        for run_id, run in self.runs.items():
            if run.strategy not in strategies:
                strategies[run.strategy] = []
            strategies[run.strategy].append(run.get_summary())
        
        comparison = {}
        
        for strategy, summaries in strategies.items():
            if not summaries:
                continue
            
            # Average across runs with same strategy
            placement_success_rates = [s.get("placement", {}).get("success_rate", 0) 
                                       for s in summaries]
            exec_times = [s.get("execution", {}).get("avg_execution_time_ms", 0) 
                         for s in summaries if s.get("execution")]
            recovery_times = [s.get("recovery", {}).get("avg_recovery_duration_ms", 0) 
                             for s in summaries if s.get("recovery") and 
                             s.get("recovery", {}).get("avg_recovery_duration_ms")]
            
            avg_placement_success = mean(placement_success_rates) if placement_success_rates else 0
            avg_exec_time = mean(exec_times) if exec_times else 0
            avg_recovery_time = mean(recovery_times) if recovery_times else 0
            
            comparison[strategy] = {
                "runs": len(summaries),
                "avg_placement_success_rate": round(avg_placement_success, 2),
                "avg_execution_time_ms": round(avg_exec_time, 2) if avg_exec_time else None,
                "avg_recovery_time_ms": round(avg_recovery_time, 2) if avg_recovery_time else None,
            }
        
        return comparison
