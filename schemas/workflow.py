"""schemas/workflow.py — versioned Pydantic contract for a robot workflow graph."""
from __future__ import annotations
import uuid
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"

class NodeType(str, Enum):
    ACTION = "action"; VERIFICATION = "verification"; RECOVERY = "recovery"

class FailureMode(str, Enum):
    OBJECT_MISSING = "object_missing"; OBJECT_MISPLACED = "object_misplaced"
    GRASP_FAILURE = "grasp_failure"; PLACEMENT_FAILURE = "placement_failure"
    TOOL_UNAVAILABLE = "tool_unavailable"; VERIFICATION_FAILURE = "verification_failure"
    TIMEOUT = "timeout"; UNKNOWN = "unknown"

class RecoveryStrategy(str, Enum):
    RESCAN = "rescan"; RETRY = "retry"; RETRY_WITH_BACKOFF = "retry_with_backoff"
    ESCALATE_TO_HUMAN = "escalate_to_human"; ABORT = "abort"

class ToolRequirement(BaseModel):
    name: str = Field(..., min_length=1)
    capability: Optional[str] = Field(default=None)

class FailureEdge(BaseModel):
    on: FailureMode
    recovery_node_id: str = Field(...)
    max_retries: int = Field(default=1, ge=0, le=10)

class WorkflowNode(BaseModel):
    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    type: NodeType
    tools_required: list[ToolRequirement] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)
    possible_failures: list[FailureMode] = Field(default_factory=list)
    on_failure: list[FailureEdge] = Field(default_factory=list)
    next: list[str] = Field(default_factory=list)
    recovery_strategy: Optional[RecoveryStrategy] = Field(default=None)
    returns_to: Optional[str] = Field(default=None)

    @field_validator("possible_failures")
    @classmethod
    def _failures_must_be_unique(cls, v):
        if len(v) != len(set(v)):
            raise ValueError("possible_failures must not contain duplicates")
        return v

    @model_validator(mode="after")
    def _recovery_shape(self):
        if self.type == NodeType.RECOVERY:
            if self.recovery_strategy is None:
                raise ValueError(f"recovery node '{self.id}' must declare a recovery_strategy")
        else:
            if self.on_failure and not self.possible_failures:
                raise ValueError(f"node '{self.id}' declares on_failure edges but no possible_failures")
        return self

class Workflow(BaseModel):
    schema_version: str = Field(default=SCHEMA_VERSION)
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_instruction: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    entry_node_id: str
    nodes: list[WorkflowNode] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _entry_node_exists(self):
        ids = {n.id for n in self.nodes}
        if self.entry_node_id not in ids:
            raise ValueError(f"entry_node_id '{self.entry_node_id}' does not match any node id")
        if len(ids) != len(self.nodes):
            raise ValueError("node ids must be unique within a workflow")
        return self

    def node_map(self):
        return {n.id: n for n in self.nodes}

Workflow.model_rebuild()
