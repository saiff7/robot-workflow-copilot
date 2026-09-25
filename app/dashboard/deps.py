"""
app/dashboard/deps.py

FastAPI dependency providers shared by both the JSON API routes
(app/api/routes.py) and the HTMX fragment routes (app/dashboard/routes.py).
Kept in one tiny module so both route modules depend on the exact same
WorkflowService singleton rather than constructing their own.
"""

from __future__ import annotations

from app.services import WorkflowService

_service_singleton: WorkflowService | None = None


def get_workflow_service() -> WorkflowService:
    """
    Returns the process-wide WorkflowService singleton, constructing it on
    first use. A singleton (rather than a fresh instance per request) is
    required here because the service holds the "current workflow" the
    dashboard is exploring - see app.services module docstring for why that
    single-workflow simplification is intentional for this project's scope.
    """
    global _service_singleton
    if _service_singleton is None:
        _service_singleton = WorkflowService()
    return _service_singleton
