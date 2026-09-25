"""app/dashboard/routes.py — HTMX fragment routes returning rendered HTML partials."""
from __future__ import annotations
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from app.dashboard.deps import get_workflow_service
from app.parser.fallback_parser import UnparsableInstructionError
from app.services import NoActiveWorkflowError, WorkflowService
from schemas.workflow import FailureMode

router = APIRouter()
templates = Jinja2Templates(directory="app/dashboard/templates")

DEFAULT_INSTRUCTION = "Pick the blue box from shelf A, place it on workstation 2, verify placement, and retry if the object is missing."

@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {"default_instruction": DEFAULT_INSTRUCTION})

@router.post("/ui/generate", response_class=HTMLResponse)
def ui_generate(request: Request, instruction: str = Form(...), service: WorkflowService = Depends(get_workflow_service)) -> HTMLResponse:
    try:
        outcome = service.generate_workflow(instruction)
    except UnparsableInstructionError as exc:
        return templates.TemplateResponse(request, "_error_partial.html", {"title": "Could not parse instruction", "message": str(exc)}, status_code=422)
    return templates.TemplateResponse(request, "_workflow_panel.html", {"workflow": outcome.workflow, "used_fallback": outcome.used_fallback, "fallback_reason": outcome.fallback_reason, "validation": outcome.validation})

@router.post("/ui/simulate", response_class=HTMLResponse)
def ui_simulate(request: Request, failure_node_id: str = Form(""), service: WorkflowService = Depends(get_workflow_service)) -> HTMLResponse:
    node_id = None
    failure_mode = None
    if failure_node_id:
        node_id, _, mode_raw = failure_node_id.partition("::")
        try:
            failure_mode = FailureMode(mode_raw)
        except ValueError:
            failure_mode = None
    try:
        result = service.run_simulation(failure_node_id=node_id, failure_mode=failure_mode)
    except NoActiveWorkflowError as exc:
        return templates.TemplateResponse(request, "_error_partial.html", {"title": "No workflow to simulate", "message": str(exc)}, status_code=409)
    transitions = service.get_transitions(result.run_id)
    return templates.TemplateResponse(request, "_execution_trace.html", {"result": result, "transitions": transitions})

@router.get("/ui/metrics", response_class=HTMLResponse)
def ui_metrics(request: Request, service: WorkflowService = Depends(get_workflow_service)) -> HTMLResponse:
    metrics = service.get_metrics()
    return templates.TemplateResponse(request, "_metrics_panel.html", {"metrics": metrics})

@router.get("/ui/runs", response_class=HTMLResponse)
def ui_runs(request: Request, service: WorkflowService = Depends(get_workflow_service)) -> HTMLResponse:
    runs = service.list_recent_runs(limit=10)
    return templates.TemplateResponse(request, "_run_history.html", {"runs": runs})
