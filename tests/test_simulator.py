"""tests/test_simulator.py — simulator state machine coverage."""

from __future__ import annotations
from app.parser.fallback_parser import parse_fallback
from app.simulator import FailureInjection, RunStatus, TransitionType, WorkflowSimulator
from schemas.workflow import FailureEdge, FailureMode, NodeType, RecoveryStrategy, Workflow, WorkflowNode

PICK_PLACE_INSTRUCTION = (
    "Pick the blue box from shelf A, place it on workstation 2, "
    "verify placement, and retry if the object is missing."
)


def _pick_place_workflow() -> Workflow:
    return parse_fallback(PICK_PLACE_INSTRUCTION)


def _workflow_with_escalation() -> Workflow:
    return Workflow(
        source_instruction="inspect the widget", name="escalation test workflow", entry_node_id="inspect",
        nodes=[
            WorkflowNode(id="inspect", name="Inspect widget", type=NodeType.VERIFICATION,
                         possible_failures=[FailureMode.TOOL_UNAVAILABLE],
                         on_failure=[FailureEdge(on=FailureMode.TOOL_UNAVAILABLE, recovery_node_id="escalate", max_retries=1)],
                         next=[]),
            WorkflowNode(id="escalate", name="Escalate to human", type=NodeType.RECOVERY,
                         recovery_strategy=RecoveryStrategy.ESCALATE_TO_HUMAN),
        ],
    )


def _workflow_with_abort() -> Workflow:
    return Workflow(
        source_instruction="critical safety check", name="abort test workflow", entry_node_id="check",
        nodes=[
            WorkflowNode(id="check", name="Safety check", type=NodeType.VERIFICATION,
                         possible_failures=[FailureMode.UNKNOWN],
                         on_failure=[FailureEdge(on=FailureMode.UNKNOWN, recovery_node_id="halt", max_retries=1)],
                         next=[]),
            WorkflowNode(id="halt", name="Halt", type=NodeType.RECOVERY, recovery_strategy=RecoveryStrategy.ABORT),
        ],
    )


class TestHappyPath:
    def test_no_injected_failures_completes_successfully(self):
        sim = WorkflowSimulator(_pick_place_workflow(), rng_seed=1)
        result = sim.run()
        assert result.status == RunStatus.COMPLETED
        assert result.recovered is False
        assert result.transitions[-1].type == TransitionType.COMPLETED

    def test_happy_path_visits_every_action_and_verification_node_exactly_once(self):
        workflow = _pick_place_workflow()
        sim = WorkflowSimulator(workflow, rng_seed=1)
        result = sim.run()
        entered_ids = [t.node_id for t in result.transitions if t.type == TransitionType.ENTER]
        non_recovery_ids = [n.id for n in workflow.nodes if n.type != NodeType.RECOVERY]
        assert entered_ids == non_recovery_ids


class TestFailureInjectionAndRecovery:
    def test_injected_failure_routes_to_declared_recovery_node(self):
        workflow = _pick_place_workflow()
        entry_id = workflow.entry_node_id
        injection = FailureInjection(node_id=entry_id, failure_mode=FailureMode.OBJECT_MISSING, attempt=1)
        sim = WorkflowSimulator(workflow, failure_injections=[injection], rng_seed=1)
        result = sim.run()
        assert result.status == RunStatus.COMPLETED
        assert result.recovered is True
        failure_transitions = [t for t in result.transitions if t.type == TransitionType.FAILURE]
        assert len(failure_transitions) == 1
        assert failure_transitions[0].failure_mode == FailureMode.OBJECT_MISSING
        recovery_transitions = [t for t in result.transitions if t.type == TransitionType.RECOVERY_TRIGGERED]
        assert len(recovery_transitions) == 1

    def test_steps_to_recovery_is_recorded_when_run_completes_after_recovery(self):
        workflow = _pick_place_workflow()
        injection = FailureInjection(node_id=workflow.entry_node_id, failure_mode=FailureMode.OBJECT_MISSING, attempt=1)
        sim = WorkflowSimulator(workflow, failure_injections=[injection], rng_seed=1)
        result = sim.run()
        assert result.steps_to_recovery is not None
        assert result.steps_to_recovery > 0

    def test_retries_are_bounded_by_max_retries_and_exhausting_them_fails_the_run(self):
        workflow = _pick_place_workflow()
        entry_id = workflow.entry_node_id
        entry_node = next(n for n in workflow.nodes if n.id == entry_id)
        max_retries = entry_node.on_failure[0].max_retries
        injections = [
            FailureInjection(node_id=entry_id, failure_mode=FailureMode.OBJECT_MISSING, attempt=i)
            for i in range(1, max_retries + 2)
        ]
        sim = WorkflowSimulator(workflow, failure_injections=injections, rng_seed=1)
        result = sim.run()
        assert result.status == RunStatus.FAILED
        assert result.transitions[-1].type == TransitionType.ABORTED
        failure_count = sum(1 for t in result.transitions if t.type == TransitionType.FAILURE)
        assert failure_count == max_retries + 1

    def test_recovering_fewer_times_than_max_retries_still_completes(self):
        workflow = _pick_place_workflow()
        entry_id = workflow.entry_node_id
        injection = FailureInjection(node_id=entry_id, failure_mode=FailureMode.OBJECT_MISSING, attempt=1)
        sim = WorkflowSimulator(workflow, failure_injections=[injection], rng_seed=7)
        result = sim.run()
        assert result.status == RunStatus.COMPLETED


class TestUnhandledFailureMode:
    def test_failure_mode_with_no_declared_recovery_aborts_immediately(self):
        workflow = Workflow(
            source_instruction="pick the widget from bin 1", name="unhandled failure test", entry_node_id="a",
            nodes=[WorkflowNode(id="a", name="Locate widget", type=NodeType.ACTION,
                                possible_failures=[], on_failure=[], next=[])],
        )
        sim = WorkflowSimulator(
            workflow, failure_injections=[FailureInjection(node_id="a", failure_mode=FailureMode.UNKNOWN, attempt=1)],
        )
        result = sim.run()
        assert result.status == RunStatus.FAILED
        assert result.transitions[-1].type == TransitionType.ABORTED


class TestRecoveryStrategies:
    def test_escalate_to_human_strategy_ends_run_as_failed(self):
        workflow = _workflow_with_escalation()
        sim = WorkflowSimulator(
            workflow,
            failure_injections=[FailureInjection(node_id="inspect", failure_mode=FailureMode.TOOL_UNAVAILABLE, attempt=1)],
        )
        result = sim.run()
        assert result.status == RunStatus.FAILED
        assert any(t.type == TransitionType.ESCALATED for t in result.transitions)

    def test_abort_strategy_ends_run_as_failed(self):
        workflow = _workflow_with_abort()
        sim = WorkflowSimulator(
            workflow,
            failure_injections=[FailureInjection(node_id="check", failure_mode=FailureMode.UNKNOWN, attempt=1)],
        )
        result = sim.run()
        assert result.status == RunStatus.FAILED
        assert any(t.type == TransitionType.ABORTED for t in result.transitions)


class TestStochasticFailureModel:
    def test_same_seed_produces_identical_run(self):
        workflow = _pick_place_workflow()
        result_a = WorkflowSimulator(workflow, stochastic_failure_probability=0.5, rng_seed=99).run()
        result_b = WorkflowSimulator(workflow, stochastic_failure_probability=0.5, rng_seed=99).run()
        assert [t.type for t in result_a.transitions] == [t.type for t in result_b.transitions]
        assert result_a.status == result_b.status

    def test_zero_probability_never_injects_stochastic_failures(self):
        workflow = _pick_place_workflow()
        result = WorkflowSimulator(workflow, stochastic_failure_probability=0.0, rng_seed=5).run()
        assert result.status == RunStatus.COMPLETED
        assert not any(t.type == TransitionType.FAILURE for t in result.transitions)


class TestTransitionLogIntegrity:
    def test_every_transition_has_a_timestamp_and_run_id(self):
        result = WorkflowSimulator(_pick_place_workflow(), rng_seed=1).run()
        run_ids = {t.run_id for t in result.transitions}
        assert run_ids == {result.run_id}
        assert all(t.timestamp for t in result.transitions)
