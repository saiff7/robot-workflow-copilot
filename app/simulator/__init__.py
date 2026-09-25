"""
app/simulator/__init__.py

The execution simulator: walks a validated Workflow graph as a state
machine, entirely in-process, with no hardware or network I/O. This module
is the "make it observable, testable, and recoverable" half of the project -
the parser produces a plan, the validator proves the plan is structurally
sound, and this module is what actually *runs* the plan (in simulation) and
produces a complete, timestamped record of what happened.

Core design decisions
----------------------
1. Explicit state, not implicit control flow. Every step the simulator takes
   is represented as a `Transition` dataclass appended to a list - entering
   a node, succeeding, failing, entering recovery, retrying, or completing.
   There is no hidden mutable state that isn't visible in the transition
   log; replaying the log tells you exactly what the simulator did.

2. Failure is a first-class outcome, not an exception. `_execute_node`
   returns a `StepOutcome` (SUCCESS or FAILURE-with-a-FailureMode) rather
   than raising. This mirrors the schema's own philosophy: failure is data
   that flows through the graph via `on_failure` edges, not a Python
   exception that would need a try/except to route around - if this module
   used exceptions for expected failure conditions, it would be
   reintroducing exactly the pattern the schema is designed to avoid.

3. Two independent sources of "does this step fail": a deterministic
   failure injection (the demo lever - "force node X to fail with mode Y on
   attempt N") and a stochastic failure injection (a per-node probability,
   for running many simulated trials to compute success/recovery rates).
   Both funnel through the same `_execute_node` decision point so the rest
   of the state machine treats them identically.

4. Retry budgets are enforced per FailureEdge (`max_retries`), and are
   tracked per (node_id, failure_mode) pair in `_retry_counts` so a node
   that can fail two different ways gets independent retry budgets for
   each - exhausting retries on one failure mode does not affect the
   other's budget.

5. Termination is always one of exactly three states: COMPLETED (walked to
   a node with no `next` and no unresolved failure), FAILED (a failure mode
   had no declared recovery, or a recovery's strategy was ABORT, or retries
   were exhausted with no further recovery), or - never - an unhandled
   Python exception. Any bug that would otherwise raise is a defect in this
   module, not an acceptable simulator outcome.

Verified: all simulator tests in tests/test_simulator.py pass, including
happy-path completion, deterministic failure injection routing to the
correct recovery node, retry-budget enforcement, escalate-to-human and
abort recovery strategies, unhandled-failure-mode aborts, and reproducible
stochastic runs under a fixed rng_seed.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from schemas.workflow import FailureMode, NodeType, RecoveryStrategy, Workflow, WorkflowNode


class TransitionType(str, Enum):
    ENTER = "enter"
    SUCCESS = "success"
    FAILURE = "failure"
    RECOVERY_TRIGGERED = "recovery_triggered"
    RETRY = "retry"
    ESCALATED = "escalated"
    ABORTED = "aborted"
    COMPLETED = "completed"


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Transition:
    """One immutable record of something the simulator did. Timestamped in UTC."""

    run_id: str
    node_id: str
    node_name: str
    type: TransitionType
    timestamp: str
    detail: str
    failure_mode: Optional[FailureMode] = None
    simulated_latency_ms: int = 0


@dataclass
class FailureInjection:
    """
    A demo-facing lever: force a specific node to fail with a specific
    failure mode on a specific attempt number (1-indexed). Only fires once
    per configured injection; subsequent attempts at the same node succeed
    normally (or fail again only if a separate stochastic failure occurs).
    """

    node_id: str
    failure_mode: FailureMode
    attempt: int = 1


@dataclass
class SimulationResult:
    run_id: str
    workflow_id: str
    status: RunStatus
    transitions: list[Transition] = field(default_factory=list)
    steps_to_recovery: Optional[int] = None
    total_simulated_latency_ms: int = 0

    @property
    def recovered(self) -> bool:
        """True if at least one recovery was triggered and the run still completed."""
        triggered = any(t.type == TransitionType.RECOVERY_TRIGGERED for t in self.transitions)
        return triggered and self.status == RunStatus.COMPLETED


class WorkflowSimulator:
    """
    Walks a Workflow's graph starting at entry_node_id, applying deterministic
    failure injections and/or a stochastic per-node failure probability, and
    routing through declared recovery edges. Call `.run()` to execute one
    full simulated pass and get back a SimulationResult.
    """

    def __init__(
        self,
        workflow: Workflow,
        failure_injections: list[FailureInjection] | None = None,
        stochastic_failure_probability: float = 0.0,
        rng_seed: int | None = None,
        latency_range_ms: tuple[int, int] = (50, 400),
    ) -> None:
        self._workflow = workflow
        self._nodes = workflow.node_map()
        self._injections = {(inj.node_id, inj.attempt): inj.failure_mode for inj in (failure_injections or [])}
        self._stochastic_p = stochastic_failure_probability
        self._rng = random.Random(rng_seed)
        self._latency_range = latency_range_ms
        self._attempt_counts: dict[str, int] = {}
        self._retry_counts: dict[tuple[str, FailureMode], int] = {}

    def run(self, max_steps: int = 200) -> SimulationResult:
        run_id = str(uuid.uuid4())
        transitions: list[Transition] = []
        total_latency = 0
        steps_to_recovery: Optional[int] = None
        recovery_triggered_at_step: Optional[int] = None

        current_id: Optional[str] = self._workflow.entry_node_id
        step = 0
        status = RunStatus.RUNNING

        while current_id is not None and step < max_steps:
            step += 1
            node = self._nodes[current_id]
            transitions.append(self._transition(run_id, node, TransitionType.ENTER, f"Entering '{node.name}'"))

            if node.type == NodeType.RECOVERY:
                latency = self._simulated_latency()
                total_latency += latency
                transitions.append(
                    self._transition(
                        run_id, node, TransitionType.RETRY,
                        f"Recovery strategy '{node.recovery_strategy.value}' executing",
                        simulated_latency_ms=latency,
                    )
                )
                if node.recovery_strategy == RecoveryStrategy.ABORT:
                    transitions.append(
                        self._transition(run_id, node, TransitionType.ABORTED, "Recovery strategy is ABORT; ending run.")
                    )
                    status = RunStatus.FAILED
                    break
                if node.recovery_strategy == RecoveryStrategy.ESCALATE_TO_HUMAN:
                    transitions.append(
                        self._transition(run_id, node, TransitionType.ESCALATED, "Escalated to human operator; ending run.")
                    )
                    status = RunStatus.FAILED
                    break
                current_id = node.returns_to
                continue

            outcome, failure_mode = self._execute_node(node)
            latency = self._simulated_latency()
            total_latency += latency

            if outcome == "success":
                transitions.append(
                    self._transition(
                        run_id, node, TransitionType.SUCCESS, f"'{node.name}' succeeded",
                        simulated_latency_ms=latency,
                    )
                )
                if node.next:
                    current_id = node.next[0]
                    continue
                transitions.append(
                    self._transition(run_id, node, TransitionType.COMPLETED, "Workflow reached a terminal node.")
                )
                status = RunStatus.COMPLETED
                break

            transitions.append(
                self._transition(
                    run_id, node, TransitionType.FAILURE, f"'{node.name}' failed with {failure_mode.value}",
                    failure_mode=failure_mode, simulated_latency_ms=latency,
                )
            )

            edge = next((e for e in node.on_failure if e.on == failure_mode), None)
            if edge is None:
                transitions.append(
                    self._transition(
                        run_id, node, TransitionType.ABORTED,
                        f"No recovery declared for failure mode '{failure_mode.value}'.",
                    )
                )
                status = RunStatus.FAILED
                break

            retry_key = (node.id, failure_mode)
            self._retry_counts[retry_key] = self._retry_counts.get(retry_key, 0) + 1
            if self._retry_counts[retry_key] > edge.max_retries:
                transitions.append(
                    self._transition(
                        run_id, node, TransitionType.ABORTED,
                        f"Exceeded max_retries ({edge.max_retries}) for '{failure_mode.value}' on '{node.id}'.",
                    )
                )
                status = RunStatus.FAILED
                break

            if recovery_triggered_at_step is None:
                recovery_triggered_at_step = step
            transitions.append(
                self._transition(
                    run_id, node, TransitionType.RECOVERY_TRIGGERED,
                    f"Routing to recovery node '{edge.recovery_node_id}' for '{failure_mode.value}'.",
                    failure_mode=failure_mode,
                )
            )
            current_id = edge.recovery_node_id

        if status == RunStatus.RUNNING:
            status = RunStatus.FAILED  # ran out of max_steps without reaching a terminal state

        if status == RunStatus.COMPLETED and recovery_triggered_at_step is not None:
            steps_to_recovery = step - recovery_triggered_at_step

        return SimulationResult(
            run_id=run_id,
            workflow_id=self._workflow.id,
            status=status,
            transitions=transitions,
            steps_to_recovery=steps_to_recovery,
            total_simulated_latency_ms=total_latency,
        )

    def _execute_node(self, node: WorkflowNode) -> tuple[str, Optional[FailureMode]]:
        """
        Decide whether this attempt at `node` succeeds or fails, and with
        which failure mode. Deterministic injections take priority over the
        stochastic model so demo scenarios are exactly reproducible.
        """
        self._attempt_counts[node.id] = self._attempt_counts.get(node.id, 0) + 1
        attempt = self._attempt_counts[node.id]

        injected = self._injections.get((node.id, attempt))
        if injected is not None:
            return "failure", injected

        if node.possible_failures and self._stochastic_p > 0:
            if self._rng.random() < self._stochastic_p:
                failure_mode = self._rng.choice(node.possible_failures)
                return "failure", failure_mode

        return "success", None

    def _simulated_latency(self) -> int:
        return self._rng.randint(*self._latency_range)

    @staticmethod
    def _transition(
        run_id: str,
        node: WorkflowNode,
        ttype: TransitionType,
        detail: str,
        failure_mode: Optional[FailureMode] = None,
        simulated_latency_ms: int = 0,
    ) -> Transition:
        return Transition(
            run_id=run_id,
            node_id=node.id,
            node_name=node.name,
            type=ttype,
            timestamp=datetime.now(timezone.utc).isoformat(),
            detail=detail,
            failure_mode=failure_mode,
            simulated_latency_ms=simulated_latency_ms,
        )
