"""
app/logging_/__init__.py

The event logger: a thin repository layer over SQLite that persists every
simulator Transition as an append-only, timestamped, queryable row.

Why this module exists and what it deliberately does NOT do
--------------------------------------------------------------
The simulator (app/simulator) already produces a complete, in-memory
Transition log per run. This module's only job is durability and
queryability: write that log to disk so it survives process restarts and
so the dashboard can query "show me the last 20 runs," "show me every
transition for run X," or "compute success/recovery rate over the last
N runs" without re-running anything.

This is a repository, not an ORM. It is raw `sqlite3` behind a small,
typed, single-purpose class (`EventRepository`) rather than SQLModel or
SQLAlchemy, for a concrete reason worth stating rather than leaving
implicit: this project has exactly two tables, no relationships beyond a
foreign-key-shaped `run_id` string, and no query more complex than
"filter and order by timestamp." An ORM would add an abstraction layer
that pays for itself on schema migrations and complex joins - neither of
which this project has. Raw sqlite3 with parameterized queries and a
narrow public interface (record_run, list_runs, get_run, get_transitions,
compute_metrics) is easier to read end-to-end in one sitting than an ORM
model definition plus session-management boilerplate would be, and that
legibility matters more here than future flexibility this project will
never need.

Append-only means exactly what it says: this module has no update_* or
delete_* methods. A run and its transitions, once written, are never
mutated - if you want to see what happened in a run, you query for it;
you cannot edit history. This is the same trust property real production
observability systems (event stores, audit logs) rely on.

Schema
------
runs(run_id TEXT PRIMARY KEY, workflow_id TEXT, workflow_name TEXT,
     source_instruction TEXT, status TEXT, started_at TEXT,
     total_simulated_latency_ms INTEGER, steps_to_recovery INTEGER,
     used_fallback_parser INTEGER)

transitions(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
            node_id TEXT, node_name TEXT, type TEXT, timestamp TEXT,
            detail TEXT, failure_mode TEXT, simulated_latency_ms INTEGER)

Verified: all tests in tests/test_logging.py pass, including schema
creation/idempotency, persisting a full run + transitions atomically,
querying runs and transitions back out, and aggregate metrics matching
hand-computed expected values across a small set of runs with a known mix
of completed/recovered/failed outcomes.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from app.simulator import RunStatus, SimulationResult, Transition

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    workflow_name TEXT NOT NULL,
    source_instruction TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    total_simulated_latency_ms INTEGER NOT NULL,
    steps_to_recovery INTEGER,
    used_fallback_parser INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    node_name TEXT NOT NULL,
    type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    detail TEXT NOT NULL,
    failure_mode TEXT,
    simulated_latency_ms INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (run_id) REFERENCES runs (run_id)
);

CREATE INDEX IF NOT EXISTS idx_transitions_run_id ON transitions (run_id);
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs (started_at);
"""


@dataclass
class RunSummary:
    """One row from `runs`, as returned by list_runs / get_run."""

    run_id: str
    workflow_id: str
    workflow_name: str
    source_instruction: str
    status: str
    started_at: str
    total_simulated_latency_ms: int
    steps_to_recovery: Optional[int]
    used_fallback_parser: bool


@dataclass
class EvaluationMetrics:
    """
    Aggregate metrics computed across a set of stored runs, matching the
    dashboard's evaluation panel exactly: success_rate, recovery_rate (of
    runs that hit at least one failure, what fraction still completed),
    mean_steps_to_recovery, mean_latency_ms, and total_runs for context.
    """

    total_runs: int
    completed_runs: int
    failed_runs: int
    runs_with_recovery_attempt: int
    runs_recovered_successfully: int
    success_rate: float
    recovery_rate: Optional[float]
    mean_steps_to_recovery: Optional[float]
    mean_latency_ms: float


class EventRepository:
    """
    Append-only SQLite-backed store for simulation runs and their
    transitions. One instance owns one database file; safe to construct a
    fresh instance per request in the FastAPI layer since sqlite3
    connections are cheap and this class does not hold a connection open
    between calls.
    """

    def __init__(self, database_path: str) -> None:
        self._database_path = database_path
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._database_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record_run(
        self,
        result: SimulationResult,
        workflow_name: str,
        source_instruction: str,
        used_fallback_parser: bool,
    ) -> None:
        """
        Persist a completed SimulationResult: one row in `runs` plus one row
        per Transition in `transitions`. Called once per simulator run, after
        `.run()` returns - this module never observes a run in progress, only
        finished results, keeping its contract simple (write the whole thing
        atomically in one call).
        """
        started_at = result.transitions[0].timestamp if result.transitions else ""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, workflow_id, workflow_name, source_instruction, status,
                    started_at, total_simulated_latency_ms, steps_to_recovery, used_fallback_parser
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.run_id, result.workflow_id, workflow_name, source_instruction,
                    result.status.value, started_at, result.total_simulated_latency_ms,
                    result.steps_to_recovery, int(used_fallback_parser),
                ),
            )
            conn.executemany(
                """
                INSERT INTO transitions (
                    run_id, node_id, node_name, type, timestamp, detail, failure_mode, simulated_latency_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        t.run_id, t.node_id, t.node_name, t.type.value, t.timestamp, t.detail,
                        t.failure_mode.value if t.failure_mode else None, t.simulated_latency_ms,
                    )
                    for t in result.transitions
                ],
            )

    def list_runs(self, limit: int = 20) -> list[RunSummary]:
        """Most recent runs first, for the dashboard's run-history panel."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_summary(row) for row in rows]

    def get_run(self, run_id: str) -> Optional[RunSummary]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._row_to_summary(row) if row else None

    def get_transitions(self, run_id: str) -> list[dict]:
        """
        Transitions for one run, ordered as they occurred (by autoincrement
        id, which is monotonic with insertion order / timestamp). Returned as
        plain dicts rather than Transition dataclasses since these came back
        out of SQLite (strings for enums) and are consumed by Jinja2
        templates / JSON API responses, not fed back into the simulator.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transitions WHERE run_id = ? ORDER BY id ASC", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def compute_metrics(self, limit: int = 100) -> EvaluationMetrics:
        """
        Aggregate metrics over the most recent `limit` runs. This is what the
        dashboard's evaluation panel calls directly - no separate
        materialized/cached metrics table, since SQLite can aggregate a few
        hundred rows fast enough to compute this on every dashboard refresh.
        """
        runs = self.list_runs(limit=limit)
        total_runs = len(runs)
        if total_runs == 0:
            return EvaluationMetrics(
                total_runs=0, completed_runs=0, failed_runs=0,
                runs_with_recovery_attempt=0, runs_recovered_successfully=0,
                success_rate=0.0, recovery_rate=None,
                mean_steps_to_recovery=None, mean_latency_ms=0.0,
            )

        completed_runs = sum(1 for r in runs if r.status == RunStatus.COMPLETED.value)
        failed_runs = total_runs - completed_runs
        runs_with_recovery_attempt = sum(1 for r in runs if r.steps_to_recovery is not None) + sum(
            1 for r in runs if r.status == RunStatus.FAILED.value and r.steps_to_recovery is None
            and self._run_had_recovery_attempt(r.run_id)
        )
        runs_recovered_successfully = sum(
            1 for r in runs if r.status == RunStatus.COMPLETED.value and r.steps_to_recovery is not None
        )
        recovery_denominator = runs_with_recovery_attempt
        recovery_rate = (
            runs_recovered_successfully / recovery_denominator if recovery_denominator > 0 else None
        )
        steps_values = [r.steps_to_recovery for r in runs if r.steps_to_recovery is not None]
        mean_steps_to_recovery = sum(steps_values) / len(steps_values) if steps_values else None
        mean_latency_ms = sum(r.total_simulated_latency_ms for r in runs) / total_runs

        return EvaluationMetrics(
            total_runs=total_runs,
            completed_runs=completed_runs,
            failed_runs=failed_runs,
            runs_with_recovery_attempt=runs_with_recovery_attempt,
            runs_recovered_successfully=runs_recovered_successfully,
            success_rate=completed_runs / total_runs,
            recovery_rate=recovery_rate,
            mean_steps_to_recovery=mean_steps_to_recovery,
            mean_latency_ms=mean_latency_ms,
        )

    def _run_had_recovery_attempt(self, run_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM transitions WHERE run_id = ? AND type = 'recovery_triggered'",
                (run_id,),
            ).fetchone()
        return bool(row["c"])

    @staticmethod
    def _row_to_summary(row: sqlite3.Row) -> RunSummary:
        return RunSummary(
            run_id=row["run_id"],
            workflow_id=row["workflow_id"],
            workflow_name=row["workflow_name"],
            source_instruction=row["source_instruction"],
            status=row["status"],
            started_at=row["started_at"],
            total_simulated_latency_ms=row["total_simulated_latency_ms"],
            steps_to_recovery=row["steps_to_recovery"],
            used_fallback_parser=bool(row["used_fallback_parser"]),
        )
