# Robot Workflow Copilot

An independent prototype exploring how natural-language manufacturing instructions could be transformed into structured, validated, and observable robotic workflows.

Built using publicly available information about modern physical-AI workflow systems. No proprietary data was used.

> **Independent concept based solely on Strive Robotics' publicly described product direction and job description. No proprietary Strive information or data was used.**

---

## Problem Statement

Manufacturing workers describe tasks the way people talk, not the way robots execute: *"Pick the blue box from shelf A, place it on workstation 2, verify placement, and retry if the object is missing."* Turning that sentence into something a robot can safely run requires three things most demos skip:

1. **Structure** — the instruction has to become a typed graph of steps, tools, and failure modes, not a flat script.
2. **Validation** — before anything runs, the graph needs to be checked for dangling references, missing recovery paths, and cycles that would loop forever.
3. **Observability** — every execution, success, failure, and recovery needs to be a logged, queryable event — not a print statement or a silent crash.

This project builds all three, plus a dashboard to see them working together, entirely in simulation.

## Architecture

```
User instruction (natural language)
        |
        v
LLM / structured parser   <- Gemini API primary, deterministic regex fallback on any failure
        |
        v
Workflow JSON schema (Pydantic, versioned, documented)
        |
        v
Validator (structural + semantic checks -> actionable issue list, not pass/fail)
        |
        v
Execution simulator (state machine, deterministic + injectable stochastic failures)
        |
        v
Event logger (SQLite, append-only, timestamped, queryable)
        |
        v
Evaluation dashboard (FastAPI + HTMX: live graph, execution trace, logs, metrics)
```

Each pipeline stage is its own Python package (`app/parser`, `app/validator`, `app/simulator`, `app/logging_`) with a single public entrypoint. `app/services.py` is the only place that wires them together — route handlers never import the pipeline stages directly.

### The recovery graph, not try/except

The detail that matters most in this codebase: a node's failure modes are declared in `possible_failures`, and each one maps to a recovery node through an `on_failure` edge — `{on: "object_missing", recovery_node_id: "rescan_shelf_a", max_retries: 2}`. The simulator resolves "what do I do when this fails" by looking up that edge, not by branching on a caught exception. That means the validator can *prove* every declared failure has a real recovery path before the workflow ever runs — a guarantee a scattered `except` block can't give you.

## Stack and Why

| Choice | Why |
|---|---|
| **Pydantic v2** | Every boundary (schema, config, API request/response) is a typed model. Catches malformed workflows at construction time, not at simulation time. |
| **Raw `sqlite3` + a thin repository class** | Two tables, no joins, no migrations needed at this scope. An ORM (SQLModel/SQLAlchemy) would add abstraction that pays for itself on complexity this project doesn't have. |
| **FastAPI** | Async-ready, typed request/response models, and a templating integration that makes serving both JSON and HTML from the same app clean. |
| **Gemini + deterministic fallback** | The parser never hard-fails. Missing API key, network error, rate limit, malformed JSON — all funnel into the same regex-based fallback parser, so the demo runs identically with or without a live key. |

### Why HTMX instead of React

This was a deliberate, considered choice, not a shortcut. A dashboard whose job is to render a graph, an execution trace, and a metrics panel doesn't need a client-side state management layer — it needs a backend that's correct and a UI that's legible. A React/Vite toolchain buys build tooling and an API-contract layer that pays for itself when a UI has complex client-side state; this one doesn't. Every HTMX interaction here — generating a workflow, injecting a failure, polling the metrics panel — is a plain HTTP request that swaps in a server-rendered fragment. That's also a signal worth being honest about: a physical-AI/robotics company cares far more about whether the state machine and recovery logic are correct than about frontend framework choice. Spending the polish budget on CSS and copy instead of a component tree is where this project's time was better spent. And practically: `docker compose up` starts everything with zero npm install, zero build step, and zero JS toolchain to go stale.

## Running Locally (Under 2 Minutes, Zero JS Tooling)

```bash
git clone <this-repo>
cd robot-workflow-copilot
docker compose up --build
```

Then open `http://localhost:8000`. That's the whole setup. No `.env` file is required — every setting has a safe default (see `app/config.py`), and an unset `GEMINI_API_KEY` simply means the parser always uses the deterministic fallback path. If you want the Gemini-backed parser, `cp .env.example .env` and fill in `GEMINI_API_KEY` first.

Without Docker:

```bash
pip install -r requirements.txt
make demo
# or directly: uvicorn app.main:app --reload
```

## Example Input/Output

**Input** (pasted into the dashboard's instruction box):
```
Pick the blue box from shelf A, place it on workstation 2, verify placement, and retry if the object is missing.
```

**Output** — a 10-node workflow graph:

```
locate_blue_box  (action)
  -> verify_object  (verification)
    -> pick_blue_box  (action)
      -> move_to_workstation_2  (action)
        -> place_blue_box  (action)
          -> verify_placement  (verification, terminal)

Recovery nodes (routed to via on_failure edges):
  rescan_shelf_a          <- object_missing / verification_failure
  retry_grasp             <- grasp_failure
  retry_navigation        <- timeout
  retry_placement         <- placement_failure
  escalate_placement_check <- verification_failure (on final check)
```

Injecting `object_missing` at `locate_blue_box` on the dashboard produces a live execution trace: `enter -> failure -> recovery_triggered -> enter (rescan) -> retry -> enter (locate, retried) -> success -> ... -> completed`, with `steps_to_recovery` and total simulated latency recorded automatically.

## Test Coverage

46 tests across 4 files, all passing via `pytest tests/`:

- `test_parser.py` (10) — fallback parser correctness, LLM path via an injectable fake client (no real network calls), and the orchestration layer's fallback-on-any-failure guarantee.
- `test_validator.py` (13) — every structural check (dangling references, wrong-type recovery targets, unhandled failure modes, unreachable nodes, illegal happy-path cycles, orphaned recovery nodes) plus both hand-written example workflows passing cleanly.
- `test_simulator.py` (12) — happy-path completion, failure injection routing to the correct recovery node, retry-budget enforcement, escalate/abort strategies, and reproducible stochastic runs under a fixed seed.
- `test_logging.py` (11) — schema creation/idempotency, run+transition persistence, and aggregate metrics matching hand-computed expected values.

No test depends on a live LLM call — the Gemini client is always injected as a fake in tests.

## What I'd Build Next

- **Broader fallback parser coverage.** The deterministic parser currently only handles pick/place/verify/retry-if instructions; inspection-style instructions ("inspect X for defects") only work through the LLM path today. Extending the regex grammar is bounded, known work.
- **Multi-workflow sessions.** The current `WorkflowService` deliberately holds one "current workflow" at a time, matching the single-operator demo scope. A real tool would need per-session state.
- **A ROS2-shaped export.** Emitting one example workflow as a ROS2 message-shaped JSON (still simulated, never touching real hardware) would demonstrate the schema maps cleanly onto a real robotics message format without expanding this project's actual scope.

These are boundaries I drew on purpose, not gaps I didn't notice — the goal was a credible proof-of-value at a defensible depth, not a robotics research project.
