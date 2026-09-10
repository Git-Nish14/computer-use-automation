# Loads the committed example artifact and verifies it passes all contract checks.
# This catches schema regressions and the step-reference bug that previously
# blocked replay before any browser was opened.

from __future__ import annotations

from pathlib import Path

import pytest

from cua.artifact.store import ArtifactStore
from cua.replay.executor import _validate_inputs, _validate_outputs


ARTIFACT_PATH = Path(__file__).parent.parent / "capabilities" / "lookup_savings_balance.json"


@pytest.fixture
def saved_artifact():
    return ArtifactStore().load(str(ARTIFACT_PATH))


def test_artifact_loads(saved_artifact):
    assert saved_artifact.schema_version == "1.0"
    assert saved_artifact.name
    assert len(saved_artifact.steps) > 0
    assert len(saved_artifact.outputs) > 0
    assert len(saved_artifact.parameters) > 0


def test_output_source_references_exist(saved_artifact):
    step_ids = {s.id for s in saved_artifact.steps}
    for o in saved_artifact.outputs:
        if o.source_step_id != "unknown":
            assert o.source_step_id in step_ids, (
                f"Output '{o.name}' references step '{o.source_step_id}' "
                f"which does not exist. Available: {sorted(step_ids)}"
            )


def test_output_source_name_matches(saved_artifact):
    step_map = {s.id: s for s in saved_artifact.steps}
    for o in saved_artifact.outputs:
        if o.source_step_id == "unknown":
            continue
        step = step_map[o.source_step_id]
        if step.action.output_name:
            assert step.action.output_name == o.name, (
                f"Output '{o.name}' references step '{o.source_step_id}' "
                f"but that step extracts output_name='{step.action.output_name}'"
            )


VALID_PARAMS = {"member_id": "12345", "login_password": "demo123"}


def test_validate_inputs_all_required(saved_artifact):
    err = _validate_inputs(saved_artifact, VALID_PARAMS)
    assert err is None, f"Unexpected validation error: {err}"


def test_validate_inputs_missing_member_id(saved_artifact):
    err = _validate_inputs(saved_artifact, {"login_password": "demo123"})
    assert err is not None
    assert "member_id" in err


def test_validate_inputs_missing_password(saved_artifact):
    err = _validate_inputs(saved_artifact, {"member_id": "12345"})
    assert err is not None
    assert "login_password" in err


def test_validate_inputs_empty_member_id(saved_artifact):
    err = _validate_inputs(saved_artifact, {"member_id": "", "login_password": "demo123"})
    assert err is not None
    assert "empty" in err.lower()


def test_validate_inputs_non_string_rejected(saved_artifact):
    err = _validate_inputs(saved_artifact, {"member_id": 12345, "login_password": "demo123"})  # type: ignore[arg-type]
    assert err is not None
    assert "string" in err


def test_validate_outputs_success(saved_artifact):
    err = _validate_outputs(saved_artifact.outputs, {"savings_balance": "$8,750.00"})
    assert err is None


def test_validate_outputs_missing(saved_artifact):
    err = _validate_outputs(saved_artifact.outputs, {})
    assert err is not None
    assert "savings_balance" in err


def test_validate_outputs_bad_decimal(saved_artifact):
    err = _validate_outputs(saved_artifact.outputs, {"savings_balance": "not a number"})
    assert err is not None
    assert "decimal" in err


def test_unknown_source_step_rejected():
    """Artifacts with source_step_id='unknown' must fail validation before browser is opened."""
    from cua.artifact.schema import OutputSpec, ParameterSpec
    art = ArtifactStore().load(str(ARTIFACT_PATH))
    # Inject a bad output binding
    bad_output = OutputSpec(name="x", type="string", description="x", source_step_id="unknown")
    art2 = art.model_copy(update={"outputs": [bad_output]})
    err = _validate_inputs(art2, {"member_id": "12345", "login_password": "demo123"})
    assert err is not None
    assert "unknown" in err.lower()


def test_checkpoint_parameterized(saved_artifact):
    # The final checkpoint and step checkpoints should contain {member_id}
    # so they can be resolved for any member, not just the recorded one.
    assert "{member_id}" in saved_artifact.checkpoint.target, (
        f"Final checkpoint target '{saved_artifact.checkpoint.target}' is not parameterized. "
        "It must contain {{member_id}} so replay works for any member."
    )


def test_step_checkpoints_parameterized(saved_artifact):
    for step in saved_artifact.steps:
        if step.checkpoint and "/members/" in step.checkpoint.target:
            assert "{member_id}" in step.checkpoint.target, (
                f"Step '{step.id}' checkpoint target '{step.checkpoint.target}' "
                "contains a hardcoded member route — use {{member_id}} instead."
            )
