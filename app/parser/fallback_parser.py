"""
app/parser/fallback_parser.py

Deterministic, rule-based instruction parser. This is the safety net for the
whole pipeline: if the Gemini API is unavailable, unauthenticated, rate
limited, or simply times out, the demo must still produce a valid Workflow
rather than crashing or hanging. This module never makes a network call.

Approach
--------
Manufacturing pick/place/inspect instructions in this domain follow a small
number of recognizable verb patterns ("pick X from Y", "place it on Z",
"verify ...", "retry if ..."). Rather than trying to be a general NLU engine,
this parser recognizes those patterns with regular expressions and compiles
them directly into the same graph shape the LLM parser targets: an ordered
chain of action/verification nodes, each with a matching recovery node
wired through an `on_failure` edge keyed by FailureMode.

This is deliberately narrow. It exists to guarantee the demo never hard-fails,
not to rival a real LLM's language coverage. If the instruction doesn't match
a known pattern, `parse_fallback` raises `UnparsableInstructionError` with an
actionable message rather than silently returning a nonsensical workflow.

Known limitation (intentional, noted honestly rather than hidden): this
pattern set covers pick/place/verify/retry-if instructions only. It does not
attempt to handle inspection-style instructions ("inspect X for defects").
Extending coverage is listed in the README's "what I'd build next" section.
"""

from __future__ import annotations

import re
import uuid

from schemas.workflow import (
    FailureEdge,
    FailureMode,
    NodeType,
    RecoveryStrategy,
    ToolRequirement,
    Workflow,
    WorkflowNode,
)


class UnparsableInstructionError(Exception):
    """Raised when the fallback parser cannot match any known instruction pattern."""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


_PICK_RE = re.compile(
    r"pick(?:\s+up)?\s+(?:the\s+)?(?P<object>[\w\s]+?)\s+from\s+(?P<source>[\w\s]+?)(?=[,.]|\s+and\b|\s+place\b|$)",
    re.IGNORECASE,
)
_PLACE_RE = re.compile(
    r"place\s+(?:it|them)?\s*on\s+(?P<destination>[\w\s]+?)(?=[,.]|\s+and\b|\s+verify\b|$)",
    re.IGNORECASE,
)
_VERIFY_RE = re.compile(r"verify\s+(?P<target>[\w\s]+?)(?=[,.]|\s+and\b|$)", re.IGNORECASE)
_RETRY_RE = re.compile(
    r"retry\s+if\s+(?:the\s+)?(?P<condition>[\w\s]+?)(?:\s+is\s+missing|\s+fails|\s+missing)",
    re.IGNORECASE,
)


def parse_fallback(instruction: str) -> Workflow:
    """
    Deterministically compile a natural-language instruction into a Workflow
    using regex pattern matching. Raises UnparsableInstructionError if no
    recognizable pick/place/verify pattern is found.
    """
    pick_match = _PICK_RE.search(instruction)
    place_match = _PLACE_RE.search(instruction)
    verify_match = _VERIFY_RE.search(instruction)
    retry_match = _RETRY_RE.search(instruction)

    if not pick_match:
        raise UnparsableInstructionError(
            "Fallback parser could not find a recognizable 'pick <object> from <location>' "
            "clause in the instruction. Supported patterns: pick/place/verify/retry-if. "
            f"Instruction was: {instruction!r}"
        )

    obj = pick_match.group("object").strip()
    source = pick_match.group("source").strip()
    destination = place_match.group("destination").strip() if place_match else "the target location"
    verify_target = verify_match.group("target").strip() if verify_match else "placement"

    obj_slug = _slug(obj)
    nodes: list[WorkflowNode] = []

    locate_id = f"locate_{obj_slug}_{_short_id()}"
    verify_obj_id = f"verify_object_{_short_id()}"
    pick_id = f"pick_{obj_slug}_{_short_id()}"
    move_id = f"move_to_{_slug(destination)}_{_short_id()}"
    place_id = f"place_{obj_slug}_{_short_id()}"
    verify_place_id = f"verify_placement_{_short_id()}"
    rescan_id = f"rescan_{_short_id()}"
    retry_grasp_id = f"retry_grasp_{_short_id()}"
    retry_nav_id = f"retry_navigation_{_short_id()}"
    retry_place_id = f"retry_placement_{_short_id()}"

    triggers_object_missing_recovery = bool(retry_match and "missing" in instruction.lower())

    nodes.append(
        WorkflowNode(
            id=locate_id,
            name=f"Locate {obj} at {source}",
            type=NodeType.ACTION,
            tools_required=[ToolRequirement(name="vision_system", capability="rgbd_camera")],
            preconditions=[f"robot.at_location == {_slug(source)}"],
            postconditions=[f"object.{obj_slug}.location_known == true"],
            possible_failures=[FailureMode.OBJECT_MISSING],
            on_failure=[FailureEdge(on=FailureMode.OBJECT_MISSING, recovery_node_id=rescan_id, max_retries=2)]
            if triggers_object_missing_recovery
            else [],
            next=[verify_obj_id],
        )
    )
    nodes.append(
        WorkflowNode(
            id=verify_obj_id,
            name=f"Verify located object is {obj}",
            type=NodeType.VERIFICATION,
            tools_required=[ToolRequirement(name="vision_system", capability="object_classifier")],
            preconditions=[f"object.{obj_slug}.location_known == true"],
            postconditions=[f"object.{obj_slug}.identity_confirmed == true"],
            possible_failures=[FailureMode.VERIFICATION_FAILURE],
            on_failure=[
                FailureEdge(on=FailureMode.VERIFICATION_FAILURE, recovery_node_id=rescan_id, max_retries=2)
            ]
            if triggers_object_missing_recovery
            else [],
            next=[pick_id],
        )
    )
    nodes.append(
        WorkflowNode(
            id=pick_id,
            name=f"Pick {obj}",
            type=NodeType.ACTION,
            tools_required=[ToolRequirement(name="gripper", capability="parallel_jaw")],
            preconditions=[f"object.{obj_slug}.identity_confirmed == true"],
            postconditions=[f"object.{obj_slug}.grasped == true"],
            possible_failures=[FailureMode.GRASP_FAILURE],
            on_failure=[FailureEdge(on=FailureMode.GRASP_FAILURE, recovery_node_id=retry_grasp_id, max_retries=3)],
            next=[move_id],
        )
    )
    nodes.append(
        WorkflowNode(
            id=move_id,
            name=f"Move to {destination}",
            type=NodeType.ACTION,
            tools_required=[ToolRequirement(name="mobile_base")],
            preconditions=[f"object.{obj_slug}.grasped == true"],
            postconditions=[f"robot.at_location == {_slug(destination)}"],
            possible_failures=[FailureMode.TIMEOUT],
            on_failure=[FailureEdge(on=FailureMode.TIMEOUT, recovery_node_id=retry_nav_id, max_retries=2)],
            next=[place_id],
        )
    )
    nodes.append(
        WorkflowNode(
            id=place_id,
            name=f"Place {obj} on {destination}",
            type=NodeType.ACTION,
            tools_required=[ToolRequirement(name="gripper", capability="parallel_jaw")],
            preconditions=[f"robot.at_location == {_slug(destination)}", f"object.{obj_slug}.grasped == true"],
            postconditions=[f"object.{obj_slug}.placed == true"],
            possible_failures=[FailureMode.PLACEMENT_FAILURE],
            on_failure=[
                FailureEdge(on=FailureMode.PLACEMENT_FAILURE, recovery_node_id=retry_place_id, max_retries=2)
            ],
            next=[verify_place_id],
        )
    )
    nodes.append(
        WorkflowNode(
            id=verify_place_id,
            name=f"Verify {verify_target}",
            type=NodeType.VERIFICATION,
            tools_required=[ToolRequirement(name="vision_system", capability="pose_estimation")],
            preconditions=[f"object.{obj_slug}.placed == true"],
            postconditions=[f"object.{obj_slug}.placement_confirmed == true"],
            possible_failures=[FailureMode.PLACEMENT_FAILURE],
            on_failure=[
                FailureEdge(on=FailureMode.PLACEMENT_FAILURE, recovery_node_id=retry_place_id, max_retries=2)
            ],
            next=[],
        )
    )
    nodes.append(
        WorkflowNode(
            id=rescan_id,
            name=f"Recovery: rescan {source} for {obj}",
            type=NodeType.RECOVERY,
            recovery_strategy=RecoveryStrategy.RESCAN,
            returns_to=locate_id,
        )
    )
    nodes.append(
        WorkflowNode(
            id=retry_grasp_id,
            name=f"Recovery: retry grasp of {obj}",
            type=NodeType.RECOVERY,
            recovery_strategy=RecoveryStrategy.RETRY_WITH_BACKOFF,
            returns_to=pick_id,
        )
    )
    nodes.append(
        WorkflowNode(
            id=retry_nav_id,
            name=f"Recovery: retry navigation to {destination}",
            type=NodeType.RECOVERY,
            recovery_strategy=RecoveryStrategy.RETRY,
            returns_to=move_id,
        )
    )
    nodes.append(
        WorkflowNode(
            id=retry_place_id,
            name=f"Recovery: retry placement of {obj}",
            type=NodeType.RECOVERY,
            recovery_strategy=RecoveryStrategy.RETRY,
            returns_to=place_id,
        )
    )

    return Workflow(
        source_instruction=instruction,
        name=f"Pick and place: {obj} from {source} to {destination}",
        entry_node_id=locate_id,
        nodes=nodes,
    )
