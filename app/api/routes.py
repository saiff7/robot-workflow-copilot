"""
app/api/routes.py

JSON-only API routes: programmatic access to the same pipeline the HTMX
dashboard uses, so the backend is testable/usable independently of the UI
(per the code-quality bar: "keep this distinction explicit in the route
module so the API is also usable/testable independently of the UI").

Every handler here is thin - it resolves the WorkflowService dependency,
calls exactly one service method, and translates the result/exception into
an HTTP response. No parsing, validation, or simulation logic lives here;
that all lives in app.services / app.parser / app.validator / app.simulator.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.dashboard.deps import get_workflow_service
from app.parser.fallback_parser import UnparsableInstructionError
from app.services import NoActiveWorkflowError, WorkflowService
from schemas.workflow import FailureMode

router = APIRouter(prefix="/api", tags=["api"])


@router.post("/workflows")
def generate_workflow(payload: dict, service: WorkflowService = Depends(get_workflow_service)) -> dict:
    """Body: {"instruction": "..."}. Returns the parsed Workflow + validation report as JSON."""
    instruction = payload.get("instruction", "")
    try:
        outcome = service.generate_workflow(instruction)
    except UnparsableInstructionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "workflow": outcome.workflow.model_dump(mode="json"),
        "used_fallback": outcome.used_fallback,
        "fallback_reason": outcome.fallback_reason,
        "validation": {
            "is_valid": outcome.validation.is_valid,
            "errors": [vars(i) | {"severity": i.severity.value} for i in outcome.validation.errors],
            "warnings": [vars(i) | {"severity": i.severity.value} for i in outcome.validation.warnings],
        },
    }


@router.post("/simulate")
def simulate(payload: dict, service: WorkflowService = Depends(get_workflow_service)) -> dict:
    """
    Body: {"failure_node_id": "...", "failure_mode": "...", "rng_seed": 1} (all optional).
    Returns the full SimulationResult (status + transitions) as JSON.
    """
    failure_node_id: Optional[str] = payload.get("failure_node_id") or None
    failure_mode_raw: Optional[str] = payload.get("failure_mode") or None
    rng_seed: Optional[int] = payload.get("rng_seed")

    failure_mode: Optional[FailureMode] = None
    if failure_mode_raw:
        try:
            failure_mode = FailureMode(failure_mode_raw)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Unknown failure_mode '{failure_mode_raw}'") from exc

    try:
        result = service.run_simulation(failure_node_id=failure_node_id, failure_mode=failure_mode, rng_seed=rng_seed)
    except NoActiveWorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "run_id": result.run_id,
        "status": result.status.value,
        "steps_to_recovery": result.steps_to_recovery,
        "total_simulated_latency_ms": result.total_simulated_latency_ms,
        "transitions": [
            {
                "node_id": t.node_id,
                "node_name": t.node_name,
                "type": t.type.value,
                "timestamp": t.timestamp,
                "detail": t.detail,
                "failure_mode": t.failure_mode.value if t.failure_mode else None,
                "simulated_latency_ms": t.simulated_latency_ms,
            }
            for t in result.transitions
        ],
    }


@router.get("/runs")
def list_runs(limit: int = 20, service: WorkflowService = Depends(get_workflow_service)) -> dict:
    runs = service.list_recent_runs(limit=limit)
    return {"runs": [vars(r) for r in runs]}


@router.get("/runs/{run_id}/transitions")
def get_run_transitions(run_id: str, service: WorkflowService = Depends(get_workflow_service)) -> dict:
    return {"run_id": run_id, "transitions": service.get_transitions(run_id)}


@router.get("/metrics")
def get_metrics(limit: int = 100, service: WorkflowService = Depends(get_workflow_service)) -> dict:
    return vars(service.get_metrics(limit=limit))
