"""tests/test_validator.py — whole-graph structural checks."""

from __future__ import annotations
import json
from pathlib import Path
import pytest
from app.parser.fallback_parser import parse_fallback
from app.validator import Severity, validate_workflow
from schemas.workflow import FailureEdge, FailureMode, NodeType, RecoveryStrategy, Workflow, WorkflowNode

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def _minimal_valid_workflow() -> Workflow:
    return Workflow(
        source_instruction="pick the red widget from bin 1, place it on table 1",
        name="minimal valid workflow", entry_node_id="a",
        nodes=[
            WorkflowNode(id="a", name="Locate widget", type=NodeType.ACTION,
                         possible_failures=[FailureMode.OBJECT_MISSING],
                         on_failure=[FailureEdge(on=FailureMode.OBJECT_MISSING, recovery_node_id="r", max_retries=1)],
                         next=["b"]),
            WorkflowNode(id="b", name="Pick widget", type=NodeType.ACTION, next=[]),
            WorkflowNode(id="r", name="Rescan", type=NodeType.RECOVERY,
                         recovery_strategy=RecoveryStrategy.RESCAN, returns_to="a"),
        ],
    )


class TestValidWorkflows:
    def test_minimal_valid_workflow_has_no_errors(self):
        report = validate_workflow(_minimal_valid_workflow())
        assert report.is_valid
        assert report.errors == []

    @pytest.mark.parametrize("filename", ["pick_place.json", "inspection.json"])
    def test_hand_written_examples_pass_validation(self, filename):
        data = json.loads((EXAMPLES_DIR / filename).read_text())
        workflow = Workflow.model_validate(data)
        report = validate_workflow(workflow)
        assert report.is_valid, f"{filename} produced errors: {report.errors}"

    def test_fallback_parser_output_passes_validation(self):
        workflow = parse_fallback(
            "Pick the blue box from shelf A, place it on workstation 2, "
            "verify placement, and retry if the object is missing."
        )
        report = validate_workflow(workflow)
        assert report.is_valid, f"fallback parser output produced errors: {report.errors}"


class TestDanglingReferences:
    def test_dangling_next_reference_is_an_error(self):
        wf = _minimal_valid_workflow()
        wf.nodes[1].next = ["does_not_exist"]
        report = validate_workflow(wf)
        assert not report.is_valid
        assert any(i.code == "DANGLING_NEXT_REFERENCE" for i in report.errors)

    def test_dangling_recovery_reference_is_an_error(self):
        wf = _minimal_valid_workflow()
        wf.nodes[0].on_failure[0].recovery_node_id = "ghost_recovery_node"
        report = validate_workflow(wf)
        assert not report.is_valid
        assert any(i.code == "DANGLING_RECOVERY_REFERENCE" for i in report.errors)

    def test_dangling_returns_to_reference_is_an_error(self):
        wf = _minimal_valid_workflow()
        wf.nodes[2].returns_to = "nonexistent_node"
        report = validate_workflow(wf)
        assert not report.is_valid
        assert any(i.code == "DANGLING_RETURNS_TO_REFERENCE" for i in report.errors)


class TestRecoveryTargetType:
    def test_recovery_edge_pointing_to_non_recovery_node_is_an_error(self):
        wf = _minimal_valid_workflow()
        wf.nodes[0].on_failure[0].recovery_node_id = "b"
        report = validate_workflow(wf)
        assert not report.is_valid
        assert any(i.code == "RECOVERY_TARGET_WRONG_TYPE" for i in report.errors)


class TestReachability:
    def test_unreachable_node_is_a_warning(self):
        wf = _minimal_valid_workflow()
        wf.nodes.append(WorkflowNode(id="orphan", name="Never visited", type=NodeType.ACTION, next=[]))
        report = validate_workflow(wf)
        assert report.is_valid
        assert any(i.code == "UNREACHABLE_NODE" and i.node_id == "orphan" for i in report.warnings)


class TestIllegalCycles:
    def test_happy_path_cycle_with_no_recovery_node_is_an_error(self):
        wf = _minimal_valid_workflow()
        wf.nodes[1].next = ["a"]
        report = validate_workflow(wf)
        assert not report.is_valid
        assert any(i.code == "ILLEGAL_HAPPY_PATH_CYCLE" for i in report.errors)

    def test_recovery_retry_loop_via_returns_to_is_not_flagged_as_illegal_cycle(self):
        report = validate_workflow(_minimal_valid_workflow())
        assert not any(i.code == "ILLEGAL_HAPPY_PATH_CYCLE" for i in report.issues)


class TestOrphanedRecoveryNodes:
    def test_recovery_node_never_referenced_is_a_warning(self):
        wf = _minimal_valid_workflow()
        wf.nodes.append(WorkflowNode(id="unused_recovery", name="Never routed to", type=NodeType.RECOVERY,
                                      recovery_strategy=RecoveryStrategy.ABORT))
        report = validate_workflow(wf)
        assert report.is_valid
        assert any(i.code == "ORPHANED_RECOVERY_NODE" and i.node_id == "unused_recovery" for i in report.warnings)


class TestReportShape:
    def test_report_separates_errors_and_warnings(self):
        wf = _minimal_valid_workflow()
        wf.nodes[1].next = ["does_not_exist"]
        wf.nodes.append(WorkflowNode(id="orphan", name="x", type=NodeType.ACTION, next=[]))
        report = validate_workflow(wf)
        assert len(report.errors) >= 1
        assert len(report.warnings) >= 1
        assert all(i.severity == Severity.ERROR for i in report.errors)
        assert all(i.severity == Severity.WARNING for i in report.warnings)
