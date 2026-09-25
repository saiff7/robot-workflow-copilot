"""app/services.py — orchestrates parser -> validator -> simulator -> logger."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from app.config import settings
from app.logging_ import EventRepository, EvaluationMetrics, RunSummary
from app.parser import ParseResult, parse_instruction
from app.parser.fallback_parser import UnparsableInstructionError
from app.simulator import FailureInjection, SimulationResult, WorkflowSimulator
from app.validator import ValidationReport, validate_workflow
from schemas.workflow import FailureMode, Workflow

@dataclass
class GenerateWorkflowOutcome:
    workflow: Workflow; used_fallback: bool; fallback_reason: Optional[str]; validation: ValidationReport

class WorkflowService:
    def __init__(self, repository: Optional[EventRepository] = None):
        self._repository = repository or EventRepository(settings.database_path)
        self._current_workflow: Optional[Workflow] = None
        self._current_used_fallback: bool = False

    @property
    def repository(self): return self._repository
    @property
    def current_workflow(self): return self._current_workflow

    def generate_workflow(self, instruction: str) -> GenerateWorkflowOutcome:
        result: ParseResult = parse_instruction(instruction)
        report = validate_workflow(result.workflow)
        self._current_workflow = result.workflow
        self._current_used_fallback = result.used_fallback
        return GenerateWorkflowOutcome(workflow=result.workflow, used_fallback=result.used_fallback, fallback_reason=result.fallback_reason, validation=report)

    def run_simulation(self, failure_node_id=None, failure_mode=None, rng_seed=None) -> SimulationResult:
        if self._current_workflow is None:
            raise NoActiveWorkflowError("No workflow has been generated yet.")
        injections = []
        if failure_node_id and failure_mode:
            injections.append(FailureInjection(node_id=failure_node_id, failure_mode=failure_mode, attempt=1))
        simulator = WorkflowSimulator(self._current_workflow, failure_injections=injections, rng_seed=rng_seed)
        result = simulator.run()
        self._repository.record_run(result, workflow_name=self._current_workflow.name, source_instruction=self._current_workflow.source_instruction, used_fallback_parser=self._current_used_fallback)
        return result

    def list_recent_runs(self, limit: int = 20): return self._repository.list_runs(limit=limit)
    def get_transitions(self, run_id: str): return self._repository.get_transitions(run_id)
    def get_metrics(self, limit: int = 100) -> EvaluationMetrics: return self._repository.compute_metrics(limit=limit)

class NoActiveWorkflowError(Exception):
    pass
