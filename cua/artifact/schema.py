# Pydantic models for CapabilityArtifact — the core data structure.
# An artifact is a typed, versioned, parameterised automation flow that
# the replay engine can execute without the LLM.

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class SurfaceType(str, Enum):
    WEB_MODERN = "web_modern"
    WEB_LEGACY = "web_legacy"
    DESKTOP_WIN32 = "desktop_win32"
    DESKTOP_JAVA = "desktop_java"


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    WAIT = "wait"
    EXTRACT = "extract"
    ASSERT = "assert"
    DISMISS_DIALOG = "dismiss_dialog"
    SUBMIT = "submit"


class RiskLevel(str, Enum):
    SAFE = "safe"
    MODERATE = "moderate"
    HIGH = "high"


class LocatorMethod(str, Enum):
    ARIA_ROLE = "aria_role"
    ARIA_LABEL = "aria_label"
    TEXT = "text"
    PLACEHOLDER = "placeholder"
    XPATH = "xpath"
    CSS = "css"
    TITLE = "title"


class LocatorStrategy(BaseModel):
    method: LocatorMethod
    value: str
    role: str | None = None
    exact: bool = False


class LocatorSpec(BaseModel):
    # Try primary first, then fallbacks in order.
    # Primary should always be the most semantically stable locator available.
    primary: LocatorStrategy
    fallbacks: list[LocatorStrategy] = Field(default_factory=list)
    rationale: str


class ParameterSpec(BaseModel):
    name: str
    type: Literal["string", "integer", "decimal", "boolean"]
    description: str
    required: bool = True
    example: str | None = None
    validation_pattern: str | None = None
    sensitive: bool = False


class OutputSpec(BaseModel):
    name: str
    type: Literal["string", "decimal", "integer", "boolean"]
    description: str
    source_step_id: str


class ExpectedOutcome(BaseModel):
    # A known business result that is NOT a system failure (e.g. "member not found").
    code: str
    description: str
    detection_pattern: str


class ErrorHandlingSpec(BaseModel):
    # Three tiers: expected business results / auto-recoverable / hard stops.
    expected_outcomes: list[ExpectedOutcome] = Field(default_factory=list)
    recoverable_patterns: list[str] = Field(default_factory=list)
    fail_patterns: list[str] = Field(default_factory=list)


class CheckpointSpec(BaseModel):
    # Explicit assertion that the system reached the expected state.
    # Prevents silently proceeding after a missed click or unexpected redirect.
    description: str
    type: Literal["url_contains", "url_exact", "element_present", "text_present", "element_text"]
    target: str
    expected_value: str | None = None


class StepAction(BaseModel):
    type: ActionType
    locator: LocatorSpec | None = None
    value: str | None = None    # Supports {param_name} placeholders
    url: str | None = None      # Supports {param_name} placeholders
    timeout_ms: int = 10_000
    risk_level: RiskLevel = RiskLevel.SAFE
    output_name: str | None = None
    error_handling: ErrorHandlingSpec = Field(default_factory=ErrorHandlingSpec)


class Step(BaseModel):
    id: str
    description: str
    action: StepAction
    checkpoint: CheckpointSpec | None = None


class TargetSpec(BaseModel):
    entry_url: str
    surface_type: SurfaceType = SurfaceType.WEB_MODERN
    tenant_id: str | None = None
    description: str | None = None


class SafetySpec(BaseModel):
    permitted_domains: list[str]
    permitted_action_types: list[ActionType]
    max_steps: int = 50
    requires_human_approval_for: list[ActionType] = Field(default_factory=list)
    sensitive_param_names: list[str] = Field(default_factory=list)


class ArtifactMetadata(BaseModel):
    created_at: datetime
    discovery_run_id: str
    model_used: str
    discovery_duration_s: float
    schema_version: str = "1.0"


class CapabilityArtifact(BaseModel):
    schema_version: str = "1.0"
    id: str
    name: str
    description: str
    target: TargetSpec
    parameters: list[ParameterSpec]
    outputs: list[OutputSpec]
    steps: list[Step]
    checkpoint: CheckpointSpec
    safety: SafetySpec
    metadata: ArtifactMetadata
