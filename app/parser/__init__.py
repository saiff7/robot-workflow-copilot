"""
app/parser/__init__.py

Public entrypoint for the parsing stage of the pipeline: `parse_instruction`.

This module owns exactly one decision that matters: try the Gemini-backed
parser first, and on ANY failure (missing key, network error, timeout, bad
JSON, schema validation failure) fall back to the deterministic rule-based
parser rather than propagating the error to the caller. This is what makes
the demo resilient - a dead API key or a rate limit never blocks the
pipeline, it just silently downgrades to the regex parser and says so in the
structured log and in the returned `ParseResult.used_fallback` flag so the
dashboard can surface it honestly instead of hiding it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.parser.fallback_parser import UnparsableInstructionError, parse_fallback
from app.parser.llm_parser import LLMParseError, parse_with_llm
from schemas.workflow import Workflow

logger = logging.getLogger(__name__)


@dataclass
class ParseResult:
    workflow: Workflow
    used_fallback: bool
    fallback_reason: str | None = None


def parse_instruction(instruction: str, llm_client: Any = None) -> ParseResult:
    """
    Parse a natural-language instruction into a validated Workflow.

    Tries the Gemini-backed parser first. On LLMParseError, logs the reason
    at WARNING level and retries with the deterministic fallback parser. If
    the fallback also cannot parse the instruction, UnparsableInstructionError
    propagates to the caller (there is no third fallback - at that point the
    instruction genuinely doesn't match any supported pattern, and the API
    layer is expected to surface that as an actionable 4xx, not a 500).
    """
    instruction = instruction.strip()
    if not instruction:
        raise UnparsableInstructionError("Instruction must not be empty.")

    try:
        workflow = parse_with_llm(instruction, client=llm_client)
        logger.info("parsed instruction via LLM", extra={"instruction": instruction})
        return ParseResult(workflow=workflow, used_fallback=False)
    except LLMParseError as exc:
        logger.warning(
            "LLM parse failed, using deterministic fallback",
            extra={"instruction": instruction, "reason": str(exc)},
        )
        workflow = parse_fallback(instruction)
        return ParseResult(workflow=workflow, used_fallback=True, fallback_reason=str(exc))
