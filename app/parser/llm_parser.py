"""
app/parser/llm_parser.py

Gemini-backed structured parser. Sends the raw instruction to the Gemini API
with a JSON-mode request asking for the same shape as `schemas.workflow.Workflow`,
then validates the returned JSON through the Pydantic model before it is
trusted anywhere else in the system.

This module is intentionally the *only* place that talks to an external LLM
API. Everything downstream (validator, simulator, dashboard) only ever sees
a `Workflow` object, never a raw LLM response - so swapping providers later
means touching this one file, not the rest of the codebase.

Failure handling: this module raises `LLMParseError` on any failure (missing
API key, network error, timeout, non-JSON response, schema validation
failure of the LLM's output). It never returns a partially-valid Workflow.
The caller (`app/parser/__init__.py`'s `parse_instruction`) is responsible
for catching this and falling back to the deterministic parser - that
decision does not belong inside this module, to keep it a clean, testable
"try the LLM, raise on any problem" unit.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.config import settings
from schemas.workflow import Workflow

_SYSTEM_PROMPT = """You convert a manufacturing worker's natural-language instruction into a \
structured robot workflow graph.

Output ONLY valid JSON matching this shape (no prose, no markdown fences):
{
  "source_instruction": "<the original instruction verbatim>",
  "name": "<short descriptive workflow name>",
  "entry_node_id": "<id of first node>",
  "nodes": [
    {
      "id": "<unique snake_case id>",
      "name": "<human readable step name>",
      "type": "action" | "verification" | "recovery",
      "tools_required": [{"name": "<tool>", "capability": "<optional capability>"}],
      "preconditions": ["<condition string>"],
      "postconditions": ["<condition string>"],
      "possible_failures": ["object_missing" | "object_misplaced" | "grasp_failure" | \
"placement_failure" | "tool_unavailable" | "verification_failure" | "timeout" | "unknown"],
      "on_failure": [{"on": "<failure_mode>", "recovery_node_id": "<id>", "max_retries": 1}],
      "next": ["<next node id>"],
      "recovery_strategy": "rescan" | "retry" | "retry_with_backoff" | "escalate_to_human" | "abort" | null,
      "returns_to": "<node id to resume at, only for recovery nodes>" | null
    }
  ]
}

Rules:
- Every declared failure mode in possible_failures MUST have a matching on_failure edge.
- Every on_failure edge MUST point to a node of type "recovery" that exists in "nodes".
- Recovery nodes MUST set recovery_strategy and SHOULD set returns_to.
- Break the instruction into the smallest sensible steps: locate, verify identity, \
pick/act, move, place/act, verify outcome, then recovery nodes for each declared failure.
- Use snake_case ids. Do not include any text outside the JSON object."""


class LLMParseError(Exception):
    """Raised for any failure in the LLM-backed parse path: missing key, network, timeout, or bad output."""


def _extract_json(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise LLMParseError(f"Gemini response was not valid JSON: {exc}") from exc


def parse_with_llm(instruction: str, client: Any = None) -> Workflow:
    """
    Parse `instruction` via the Gemini API into a validated Workflow.

    `client` is injectable for testing - tests pass a fake client with a
    canned `.generate(...)` response instead of hitting the network, so this
    function's control flow (prompt construction, JSON extraction, schema
    validation, error translation) is fully covered without any live API key.
    """
    if not settings.gemini_api_key and client is None:
        raise LLMParseError("GEMINI_API_KEY is not configured; skipping LLM parse path.")

    if client is None:
        try:
            import google.generativeai as genai
        except ImportError as exc:
            raise LLMParseError("google-generativeai package is not installed.") from exc

        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel(
            settings.gemini_model,
            system_instruction=_SYSTEM_PROMPT,
            generation_config={"response_mime_type": "application/json"},
        )
        try:
            response = model.generate_content(
                instruction,
                request_options={"timeout": settings.llm_timeout_seconds},
            )
            raw_text = response.text
        except Exception as exc:  # noqa: BLE001 - any SDK/network failure funnels into LLMParseError
            raise LLMParseError(f"Gemini API call failed: {exc}") from exc
    else:
        raw_text = client.generate(system_prompt=_SYSTEM_PROMPT, user_prompt=instruction)

    payload = _extract_json(raw_text)
    payload.setdefault("source_instruction", instruction)

    try:
        return Workflow.model_validate(payload)
    except ValidationError as exc:
        raise LLMParseError(f"Gemini output failed workflow schema validation: {exc}") from exc
