"""tests/test_parser.py — parser fallback + LLM + orchestration coverage."""

from __future__ import annotations
import json
import pytest
from app.parser import parse_instruction
from app.parser.fallback_parser import UnparsableInstructionError, parse_fallback
from schemas.workflow import NodeType

PICK_PLACE_INSTRUCTION = (
    "Pick the blue box from shelf A, place it on workstation 2, "
    "verify placement, and retry if the object is missing."
)


class FakeLLMClient:
    def __init__(self, response_text: str):
        self._response_text = response_text
    def generate(self, system_prompt: str, user_prompt: str) -> str:
        return self._response_text


def _valid_llm_json(instruction: str) -> str:
    return json.dumps({
        "source_instruction": instruction, "name": "LLM-generated workflow", "entry_node_id": "n1",
        "nodes": [
            {"id": "n1", "name": "Locate object", "type": "action",
             "possible_failures": ["object_missing"],
             "on_failure": [{"on": "object_missing", "recovery_node_id": "r1", "max_retries": 2}], "next": []},
            {"id": "r1", "name": "Rescan", "type": "recovery", "recovery_strategy": "rescan", "returns_to": "n1"},
        ],
    })


class TestFallbackParser:
    def test_parses_canonical_pick_place_instruction(self):
        workflow = parse_fallback(PICK_PLACE_INSTRUCTION)
        assert workflow.entry_node_id
        node_types = {n.type for n in workflow.nodes}
        assert NodeType.ACTION in node_types
        assert NodeType.VERIFICATION in node_types
        assert NodeType.RECOVERY in node_types

    def test_produces_recovery_node_reachable_from_object_missing_failure(self):
        workflow = parse_fallback(PICK_PLACE_INSTRUCTION)
        nodes_by_id = workflow.node_map()
        locate_node = next(n for n in workflow.nodes if n.id == workflow.entry_node_id)
        assert locate_node.on_failure
        recovery_id = locate_node.on_failure[0].recovery_node_id
        assert nodes_by_id[recovery_id].type == NodeType.RECOVERY

    def test_every_declared_failure_has_an_on_failure_edge(self):
        workflow = parse_fallback(PICK_PLACE_INSTRUCTION)
        for node in workflow.nodes:
            declared = {f for f in node.possible_failures}
            handled = {e.on for e in node.on_failure}
            assert declared == handled

    def test_raises_on_unrecognized_instruction(self):
        with pytest.raises(UnparsableInstructionError):
            parse_fallback("The weather today is nice.")

    def test_raises_on_non_pick_instruction(self):
        with pytest.raises(UnparsableInstructionError):
            parse_fallback("Inspect the part on workstation 3 for defects.")


class TestLLMParserViaOrchestration:
    def test_uses_llm_path_when_client_returns_valid_json(self):
        client = FakeLLMClient(_valid_llm_json(PICK_PLACE_INSTRUCTION))
        result = parse_instruction(PICK_PLACE_INSTRUCTION, llm_client=client)
        assert result.used_fallback is False
        assert result.workflow.name == "LLM-generated workflow"

    def test_falls_back_when_llm_returns_invalid_json(self):
        client = FakeLLMClient("this is not json")
        result = parse_instruction(PICK_PLACE_INSTRUCTION, llm_client=client)
        assert result.used_fallback is True
        assert "not valid JSON" in result.fallback_reason

    def test_falls_back_when_llm_output_fails_schema_validation(self):
        bad_payload = json.dumps({"name": "missing required fields"})
        client = FakeLLMClient(bad_payload)
        result = parse_instruction(PICK_PLACE_INSTRUCTION, llm_client=client)
        assert result.used_fallback is True
        assert result.workflow is not None

    def test_no_api_key_and_no_client_falls_back_cleanly(self):
        result = parse_instruction(PICK_PLACE_INSTRUCTION)
        assert result.used_fallback is True
        assert "GEMINI_API_KEY" in result.fallback_reason

    def test_empty_instruction_raises(self):
        with pytest.raises(UnparsableInstructionError):
            parse_instruction("   ")
