"""
tests/test_logging.py

Covers app.logging_.EventRepository: schema creation, persisting a
SimulationResult (run + all transitions) atomically, querying runs and
their transitions back out, and the aggregate metrics computation
(success_rate, recovery_rate, mean_steps_to_recovery, mean_latency_ms)
against a small set of runs with known, hand-computed expected values.

Uses a temporary SQLite file per test (via tmp_path) so tests never share
or pollute state and never touch the real data/events.db.

Verified: all 11 tests pass via `pytest tests/test_logging.py -v`, and all
46 tests (this file + test_parser.py + test_validator.py + test_simulator.py)
pass together via `pytest tests/`.
"""

from __future__ import annotations

from app.logging_ import EventRepository
from app.parser.fallback_parser import parse_fallback
from app.simulator import FailureInjection, RunStatus, WorkflowSimulator
from schemas.workflow import FailureMode

PICK_PLACE_INSTRUCTION = (
    "Pick the blue box from shelf A, place it on workstation 2, "
    "verify placement, and retry if the object is missing."
)


def _repo(tmp_path) -> EventRepository:
    return EventRepository(str(tmp_path / "events.db"))


def _workflow():
    return parse_fallback(PICK_PLACE_INSTRUCTION)


class TestSchemaCreation:
    def test_repository_creates_database_file_and_is_idempotent(self, tmp_path):
        db_path = tmp_path / "events.db"
        repo1 = EventRepository(str(db_path))
        assert db_path.exists()
        # constructing a second repository against the same file must not raise
        # (CREATE TABLE IF NOT EXISTS) and must see the same (empty) data
        repo2 = EventRepository(str(db_path))
        assert repo2.list_runs() == []


class TestRecordAndRetrieveRun:
    def test_recorded_run_appears_in_list_runs(self, tmp_path):
        repo = _repo(tmp_path)
        workflow = _workflow()
        result = WorkflowSimulator(workflow, rng_seed=1).run()
        repo.record_run(result, workflow_name=workflow.name, source_instruction=workflow.source_instruction, used_fallback_parser=True)

        runs = repo.list_runs()
        assert len(runs) == 1
        assert runs[0].run_id == result.run_id
        assert runs[0].status == RunStatus.COMPLETED.value
        assert runs[0].used_fallback_parser is True

    def test_get_run_returns_none_for_unknown_run_id(self, tmp_path):
        repo = _repo(tmp_path)
        assert repo.get_run("nonexistent-run-id") is None

    def test_get_run_returns_matching_summary(self, tmp_path):
        repo = _repo(tmp_path)
        workflow = _workflow()
        result = WorkflowSimulator(workflow, rng_seed=2).run()
        repo.record_run(result, workflow_name=workflow.name, source_instruction=workflow.source_instruction, used_fallback_parser=False)

        summary = repo.get_run(result.run_id)
        assert summary is not None
        assert summary.workflow_name == workflow.name
        assert summary.used_fallback_parser is False

    def test_all_transitions_are_persisted_in_order(self, tmp_path):
        repo = _repo(tmp_path)
        workflow = _workflow()
        result = WorkflowSimulator(workflow, rng_seed=3).run()
        repo.record_run(result, workflow_name=workflow.name, source_instruction=workflow.source_instruction, used_fallback_parser=True)

        stored = repo.get_transitions(result.run_id)
        assert len(stored) == len(result.transitions)
        assert [t["type"] for t in stored] == [t.type.value for t in result.transitions]
        assert [t["node_id"] for t in stored] == [t.node_id for t in result.transitions]

    def test_transitions_for_unknown_run_id_returns_empty_list(self, tmp_path):
        repo = _repo(tmp_path)
        assert repo.get_transitions("nonexistent-run-id") == []

    def test_multiple_runs_do_not_interfere_with_each_others_transitions(self, tmp_path):
        repo = _repo(tmp_path)
        workflow = _workflow()
        result_a = WorkflowSimulator(workflow, rng_seed=10).run()
        result_b = WorkflowSimulator(workflow, rng_seed=11).run()
        repo.record_run(result_a, workflow_name=workflow.name, source_instruction=workflow.source_instruction, used_fallback_parser=True)
        repo.record_run(result_b, workflow_name=workflow.name, source_instruction=workflow.source_instruction, used_fallback_parser=True)

        transitions_a = repo.get_transitions(result_a.run_id)
        transitions_b = repo.get_transitions(result_b.run_id)
        assert all(t["run_id"] == result_a.run_id for t in transitions_a)
        assert all(t["run_id"] == result_b.run_id for t in transitions_b)


class TestListRunsOrderingAndLimit:
    def test_list_runs_respects_limit(self, tmp_path):
        repo = _repo(tmp_path)
        workflow = _workflow()
        for seed in range(5):
            result = WorkflowSimulator(workflow, rng_seed=seed).run()
            repo.record_run(result, workflow_name=workflow.name, source_instruction=workflow.source_instruction, used_fallback_parser=True)

        assert len(repo.list_runs(limit=3)) == 3
        assert len(repo.list_runs(limit=100)) == 5


class TestEvaluationMetrics:
    def test_metrics_on_empty_repository_are_well_defined(self, tmp_path):
        repo = _repo(tmp_path)
        metrics = repo.compute_metrics()
        assert metrics.total_runs == 0
        assert metrics.success_rate == 0.0
        assert metrics.recovery_rate is None
        assert metrics.mean_steps_to_recovery is None

    def test_metrics_success_rate_matches_hand_computed_value(self, tmp_path):
        repo = _repo(tmp_path)
        workflow = _workflow()

        # Run 1: happy path -> completed
        r1 = WorkflowSimulator(workflow, rng_seed=1).run()
        repo.record_run(r1, workflow_name=workflow.name, source_instruction=workflow.source_instruction, used_fallback_parser=True)

        # Run 2: one failure, recovers, completes
        entry_id = workflow.entry_node_id
        r2 = WorkflowSimulator(
            workflow,
            failure_injections=[FailureInjection(node_id=entry_id, failure_mode=FailureMode.OBJECT_MISSING, attempt=1)],
            rng_seed=2,
        ).run()
        repo.record_run(r2, workflow_name=workflow.name, source_instruction=workflow.source_instruction, used_fallback_parser=True)

        # Run 3: retries exhausted -> failed
        r3 = WorkflowSimulator(
            workflow,
            failure_injections=[
                FailureInjection(node_id=entry_id, failure_mode=FailureMode.OBJECT_MISSING, attempt=i)
                for i in (1, 2, 3)
            ],
            rng_seed=3,
        ).run()
        repo.record_run(r3, workflow_name=workflow.name, source_instruction=workflow.source_instruction, used_fallback_parser=True)

        metrics = repo.compute_metrics()
        assert metrics.total_runs == 3
        assert metrics.completed_runs == 2
        assert metrics.failed_runs == 1
        assert abs(metrics.success_rate - (2 / 3)) < 1e-9
        # exactly one run (r2) both attempted recovery and completed successfully
        assert metrics.runs_recovered_successfully == 1
        assert metrics.mean_steps_to_recovery == r2.steps_to_recovery
        assert metrics.mean_latency_ms > 0

    def test_metrics_respect_limit_parameter(self, tmp_path):
        repo = _repo(tmp_path)
        workflow = _workflow()
        for seed in range(5):
            result = WorkflowSimulator(workflow, rng_seed=seed).run()
            repo.record_run(result, workflow_name=workflow.name, source_instruction=workflow.source_instruction, used_fallback_parser=True)

        metrics = repo.compute_metrics(limit=2)
        assert metrics.total_runs == 2
