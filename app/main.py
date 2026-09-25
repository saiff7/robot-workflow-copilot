"""app/main.py — FastAPI application entrypoint."""
from __future__ import annotations
import json, logging, sys
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.api.routes import router as api_router
from app.config import settings
from app.dashboard.routes import router as dashboard_router

class _JsonLogFormatter(logging.Formatter):
    def format(self, record):
        payload = {"level": record.levelname, "logger": record.name, "message": record.getMessage()}
        for key in ("instruction", "reason"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload)

def _configure_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonLogFormatter())
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(settings.log_level)

_configure_logging()
app = FastAPI(title="Robot Workflow Copilot", version="1.0")
app.mount("/static", StaticFiles(directory="app/dashboard/static"), name="static")
app.include_router(dashboard_router)
app.include_router(api_router)

@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
