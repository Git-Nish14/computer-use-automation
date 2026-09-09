"""
Tests for the CapabilityArtifact schema: serialization, validation, and
parameterization logic.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from cua.artifact.schema import (
    ActionType,
    ArtifactMetadata,
    CapabilityArtifact,
    CheckpointSpec,
    ErrorHandlingSpec,
    ExpectedOutcome,
    LocatorMethod,
    LocatorSpec,
    LocatorStrategy,
    OutputSpec,
    ParameterSpec,
    RiskLevel,
    SafetySpec,
    Step,
    StepAction,
    SurfaceType,
    TargetSpec,
)
from cua.artifact.store import ArtifactStore


def _make_artifact(**overrides) -> CapabilityArtifact:
    defaults = dict(
        id=str(uuid.uuid4()),
        name="lookup_savings_balance",
        description="Log in and read member savings balance",
        target=TargetSpec(
            entry_url="http://127.0.0.1:5000",
            surface_type=SurfaceType.WEB_LEGACY,
        ),
        parameters=[
            ParameterSpec(name="member_id", type="string", description="Member ID", example="12345"),
        ],
        outputs=[
            OutputSpec(name="savings_balance", type="decimal", description="Savings balance", source_step_id="step_05"),
        ],
        steps=[
            Step(
                id="step_01_navigate",
                description="Navigate to the application",
                action=StepAction(type=ActionType.NAVIGATE, url="http://127.0.0.1:5000"),
            ),
            Step(
                id="step_02_click_login",
                description="Click the Log In button",
                action=StepAction(
                    type=ActionType.CLICK,
                    locator=LocatorSpec(
                        primary=LocatorStrategy(method=LocatorMethod.ARIA_ROLE, value="Log In", role="button"),
                        rationale="Submit button for login form",
                    ),
                    risk_level=RiskLevel.SAFE,
                ),
            ),
            Step(
                id="step_03_type_member_id",
                description="Enter member ID in search field",
                action=StepAction(
                    type=ActionType.TYPE,
                    locator=LocatorSpec(
                        primary=LocatorStrategy(method=LocatorMethod.ARIA_LABEL, value="Member ID:"),
                        rationale="Label text is stable on this form",
                    ),
                    value="{member_id}",
                ),
            ),
            Step(
                id="step_05_extract_balance",
                description="Extract savings account balance",
                action=StepAction(
                    type=ActionType.EXTRACT,
                    locator=LocatorSpec(
                        primary=LocatorStrategy(method=LocatorMethod.CSS, value="td.balance-cell"),
                        rationale="Balance cell in accounts table",
                    ),
                    output_name="savings_balance",
                    error_handling=ErrorHandlingSpec(
                        expected_outcomes=[
                            ExpectedOutcome(
                                code="member_not_found",
                                description="Member does not exist in the system",
                                detection_pattern="No members found",
                            )
                        ]
                    ),
                ),
            ),
        ],
        checkpoint=CheckpointSpec(
            description="Member detail page loaded",
            type="url_contains",
            target="/members/",
        ),
        safety=SafetySpec(
            permitted_domains=["127.0.0.1:5000"],
            permitted_action_types=[
                ActionType.NAVIGATE, ActionType.CLICK, ActionType.TYPE, ActionType.EXTRACT,
            ],
        ),
        metadata=ArtifactMetadata(
            created_at=datetime.now(timezone.utc),
            discovery_run_id="test-run-001",
            model_used="gpt-4o",
            discovery_duration_s=45.2,
        ),
    )
    defaults.update(overrides)
    return CapabilityArtifact(**defaults)


class TestArtifactSchema:
    def test_roundtrip_json(self):
        art = _make_artifact()
        serialized = art.model_dump_json()
        restored = CapabilityArtifact.model_validate_json(serialized)
        assert restored.id == art.id
        assert restored.name == art.name
        assert len(restored.steps) == len(art.steps)

    def test_parameter_placeholder_preserved(self):
        art = _make_artifact()
        type_step = next(s for s in art.steps if s.id == "step_03_type_member_id")
        assert type_step.action.value == "{member_id}"

    def test_output_spec_present(self):
        art = _make_artifact()
        assert any(o.name == "savings_balance" for o in art.outputs)

    def test_locator_spec_has_rationale(self):
        art = _make_artifact()
        click_step = next(s for s in art.steps if s.id == "step_02_click_login")
        assert click_step.action.locator.rationale

    def test_schema_version_present(self):
        art = _make_artifact()
        assert art.schema_version == "1.0"

    def test_safety_spec_has_permitted_domains(self):
        art = _make_artifact()
        assert "127.0.0.1:5000" in art.safety.permitted_domains

    def test_error_handling_expected_outcomes(self):
        art = _make_artifact()
        extract_step = next(s for s in art.steps if s.id == "step_05_extract_balance")
        outcomes = extract_step.action.error_handling.expected_outcomes
        assert any(o.code == "member_not_found" for o in outcomes)


class TestArtifactStore:
    def test_save_and_load(self, tmp_path):
        store = ArtifactStore(base_dir=tmp_path)
        art = _make_artifact()
        path = store.save(art)
        assert path.exists()
        loaded = store.load(path)
        assert loaded.id == art.id
        assert loaded.name == art.name

    def test_saved_file_is_valid_json(self, tmp_path):
        store = ArtifactStore(base_dir=tmp_path)
        art = _make_artifact()
        path = store.save(art)
        data = json.loads(path.read_text())
        assert data["schema_version"] == "1.0"
        assert data["name"] == "lookup_savings_balance"

    def test_list(self, tmp_path):
        store = ArtifactStore(base_dir=tmp_path)
        store.save(_make_artifact(name="cap_a", id=str(uuid.uuid4())))
        store.save(_make_artifact(name="cap_b", id=str(uuid.uuid4())))
        assert len(store.list()) == 2
