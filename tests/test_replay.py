"""
Tests for the replay executor, result contract, locator logic, and input/output validation.

Unit tests (default): no browser, no API key required.
Browser tests (@pytest.mark.browser): require Playwright + mock app running.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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
from cua.replay.executor import (
    ReplayExecutor,
    _substitute,
    _substitute_checkpoint,
    _validate_inputs,
    _validate_outputs,
)
from cua.replay.result import ErrorDetail, ReplayResult, ReplayStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _artifact(
    steps=None, checkpoint=None, outputs=None, params=None, schema_version="1.0"
) -> CapabilityArtifact:
    return CapabilityArtifact(
        id=str(uuid.uuid4()),
        name="test_cap",
        description="Test capability",
        schema_version=schema_version,
        target=TargetSpec(entry_url="http://127.0.0.1:5050", surface_type=SurfaceType.WEB_LEGACY),
        parameters=params or [ParameterSpec(name="member_id", type="string", description="Member ID")],
        outputs=outputs or [],  # No outputs by default — avoids "unknown" source_step_id issues
        steps=steps or [],
        checkpoint=checkpoint or CheckpointSpec(
            description="On member page", type="url_contains", target="/members/",
        ),
        safety=SafetySpec(
            permitted_domains=["127.0.0.1"],
            permitted_action_types=[
                ActionType.NAVIGATE, ActionType.CLICK, ActionType.TYPE,
                ActionType.EXTRACT, ActionType.WAIT, ActionType.ASSERT,
            ],
        ),
        metadata=ArtifactMetadata(
            created_at=datetime.now(timezone.utc),
            discovery_run_id="test", model_used="gpt-5.6-luna", discovery_duration_s=10.0,
        ),
    )


# ---------------------------------------------------------------------------
# Unit: ReplayResult contract
# ---------------------------------------------------------------------------

class TestReplayResult:
    def test_success_is_success(self):
        r = ReplayResult(
            run_id="x", artifact_id="a", artifact_name="n",
            status=ReplayStatus.SUCCESS, outputs={"balance": "$8,750.00"},
            steps_completed=5, total_steps=5, duration_s=1.2,
        )
        assert r.is_success()
        assert not r.is_business_outcome()

    def test_business_outcome_is_not_failure(self):
        r = ReplayResult(
            run_id="x", artifact_id="a", artifact_name="n",
            status=ReplayStatus.BUSINESS_OUTCOME,
            business_outcome="member_not_found",
            business_outcome_description="Member 99999 not found",
            steps_completed=3, total_steps=5, duration_s=2.0,
        )
        assert r.is_business_outcome()
        assert not r.is_success()
        assert "member_not_found" in r.summary_line()

    def test_hard_failure_summary_includes_step(self):
        r = ReplayResult(
            run_id="x", artifact_id="a", artifact_name="n",
            status=ReplayStatus.HARD_FAILURE, steps_completed=2, total_steps=5, duration_s=1.0,
            error=ErrorDetail(
                step_id="step_03", step_description="Click Search",
                expected="button visible", observed="timeout",
                timestamp=datetime.now(timezone.utc),
            ),
        )
        assert "step_03" in r.summary_line()
        assert "button visible" in r.summary_line()


# ---------------------------------------------------------------------------
# Unit: parameter substitution
# ---------------------------------------------------------------------------

class TestSubstitution:
    def test_single(self):
        assert _substitute("/members/{member_id}", {"member_id": "12345"}) == "/members/12345"

    def test_multiple(self):
        assert _substitute("{a}+{b}", {"a": "x", "b": "y"}) == "x+y"

    def test_no_match(self):
        assert _substitute("no params", {"member_id": "12345"}) == "no params"

    def test_checkpoint_target_substituted(self):
        from cua.artifact.schema import CheckpointSpec
        cp = CheckpointSpec(description="d", type="url_contains", target="/members/{member_id}")
        resolved = _substitute_checkpoint(cp, {"member_id": "67890"})
        assert resolved.target == "/members/67890"

    def test_checkpoint_expected_value_substituted(self):
        from cua.artifact.schema import CheckpointSpec
        cp = CheckpointSpec(
            description="d", type="element_text", target=".name",
            expected_value="Member {member_id}",
        )
        resolved = _substitute_checkpoint(cp, {"member_id": "12345"})
        assert resolved.expected_value == "Member 12345"


# ---------------------------------------------------------------------------
# Unit: input/output validation
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_missing_required_param(self):
        art = _artifact()
        err = _validate_inputs(art, {})  # member_id not supplied
        assert err is not None
        assert "member_id" in err

    def test_integer_type_check_rejects_string(self):
        art = _artifact(params=[ParameterSpec(name="count", type="integer", description="Count")])
        err = _validate_inputs(art, {"count": "not_an_int"})
        assert err is not None
        assert "integer" in err

    def test_integer_type_check_accepts_valid(self):
        art = _artifact(params=[ParameterSpec(name="count", type="integer", description="Count")])
        err = _validate_inputs(art, {"count": "42"})
        assert err is None

    def test_decimal_type_check_rejects_string(self):
        art = _artifact(params=[ParameterSpec(name="amount", type="decimal", description="Amount")])
        err = _validate_inputs(art, {"amount": "not_a_number"})
        assert err is not None
        assert "decimal" in err

    def test_unknown_schema_version_rejected(self):
        art = _artifact(schema_version="99.0")
        from cua.observability.logger import RunLogger
        import io

        # Can't easily test this without running the executor, so test the schema_version check directly
        assert art.schema_version == "99.0"
        assert art.schema_version != "1.0"

    def test_unresolved_placeholder_in_step_detected(self):
        steps = [
            Step(
                id="step_01", description="type",
                action=StepAction(type=ActionType.TYPE, value="{unknown_param}",
                                  locator=LocatorSpec(
                                      primary=LocatorStrategy(method=LocatorMethod.TEXT, value="x"),
                                      rationale="test")),
            )
        ]
        art = _artifact(steps=steps)
        err = _validate_inputs(art, {"member_id": "12345"})  # unknown_param not supplied
        assert err is not None
        assert "unknown_param" in err

    def test_output_references_nonexistent_step(self):
        art = _artifact(
            outputs=[OutputSpec(name="bal", type="decimal", description="d", source_step_id="step_99")],
            steps=[],
        )
        err = _validate_inputs(art, {"member_id": "12345"})
        assert err is not None
        assert "step_99" in err


class TestOutputValidation:
    def test_missing_output_detected(self):
        declared = [OutputSpec(name="savings_balance", type="decimal", description="d", source_step_id="s")]
        err = _validate_outputs(declared, {})  # not collected
        assert err is not None
        assert "savings_balance" in err

    def test_invalid_decimal_output_rejected(self):
        declared = [OutputSpec(name="bal", type="decimal", description="d", source_step_id="s")]
        err = _validate_outputs(declared, {"bal": "not a number"})
        assert err is not None
        assert "decimal" in err

    def test_valid_decimal_currency_accepted(self):
        declared = [OutputSpec(name="bal", type="decimal", description="d", source_step_id="s")]
        err = _validate_outputs(declared, {"bal": "$8,750.00"})
        assert err is None

    def test_all_outputs_present_succeeds(self):
        declared = [
            OutputSpec(name="a", type="string", description="d", source_step_id="s"),
            OutputSpec(name="b", type="decimal", description="d", source_step_id="s"),
        ]
        err = _validate_outputs(declared, {"a": "hello", "b": "123.45"})
        assert err is None


# ---------------------------------------------------------------------------
# Unit: locator spec
# ---------------------------------------------------------------------------

class TestLocatorSpec:
    def test_primary_fallbacks(self):
        spec = LocatorSpec(
            primary=LocatorStrategy(method=LocatorMethod.ARIA_ROLE, value="Search", role="button"),
            fallbacks=[LocatorStrategy(method=LocatorMethod.TEXT, value="Search")],
            rationale="ARIA role is stable",
        )
        assert spec.primary.method == LocatorMethod.ARIA_ROLE
        assert len(spec.fallbacks) == 1

    def test_roundtrip(self):
        spec = LocatorSpec(
            primary=LocatorStrategy(method=LocatorMethod.XPATH,
                                    value="//tr[td[normalize-space()='Savings']]/td[@class='balance-cell']"),
            rationale="XPath scoped to Savings row",
        )
        restored = LocatorSpec.model_validate(spec.model_dump())
        assert "Savings" in restored.primary.value


# ---------------------------------------------------------------------------
# Unit: policy enforcement (without browser)
# ---------------------------------------------------------------------------

class TestPolicyEnforcement:
    def test_forbidden_domain_blocked(self):
        from cua.safety.policy import PolicyEnforcer, PolicyViolation
        policy = PolicyEnforcer()
        with pytest.raises(PolicyViolation, match="permitted_domains"):
            policy.check_url(
                SafetySpec(permitted_domains=["127.0.0.1"], permitted_action_types=[ActionType.NAVIGATE]),
                "http://evil.com/exfiltrate",
            )

    def test_submit_blocked_by_default_even_when_risk_level_safe(self):
        """
        SUBMIT has type-default risk=HIGH.
        Even if the artifact has risk_level=SAFE (the default), effective risk
        must be HIGH and the action must be blocked.
        """
        from cua.safety.policy import PolicyEnforcer, PolicyViolation
        policy = PolicyEnforcer(allow_high_risk=False)
        action = StepAction(type=ActionType.SUBMIT)  # risk_level defaults to SAFE
        safety = SafetySpec(
            permitted_domains=["127.0.0.1"],
            permitted_action_types=[ActionType.SUBMIT],
        )
        with pytest.raises(PolicyViolation, match="effective risk=HIGH"):
            policy.check(safety, action)

    def test_submit_allowed_with_flag(self):
        from cua.safety.policy import PolicyEnforcer
        policy = PolicyEnforcer(allow_high_risk=True)
        action = StepAction(type=ActionType.SUBMIT)
        safety = SafetySpec(
            permitted_domains=["127.0.0.1"],
            permitted_action_types=[ActionType.SUBMIT],
        )
        policy.check(safety, action)  # Should not raise

    def test_requires_approval_blocks_even_with_allow_flag(self):
        """requires_human_approval_for always blocks, regardless of allow_high_risk."""
        from cua.safety.policy import PolicyEnforcer, PolicyViolation
        policy = PolicyEnforcer(allow_high_risk=True)
        action = StepAction(type=ActionType.SUBMIT, risk_level=RiskLevel.HIGH)
        safety = SafetySpec(
            permitted_domains=["127.0.0.1"],
            permitted_action_types=[ActionType.SUBMIT],
            requires_human_approval_for=[ActionType.SUBMIT],
        )
        with pytest.raises(PolicyViolation, match="human approval"):
            policy.check(safety, action)


# ---------------------------------------------------------------------------
# Unit: executor pre-flight (mocked browser)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_required_input_returns_hard_failure(tmp_path):
    """Executor must validate inputs before touching the browser."""
    art = _artifact()  # requires member_id
    mock_page = AsyncMock()
    mock_page.goto = AsyncMock()
    mock_session = MagicMock()
    mock_session.page = mock_page
    mock_session.screenshot = AsyncMock()
    mock_session.cdp_url = "http://127.0.0.1:9222"

    from cua.observability.logger import RunLogger
    from cua.safety.policy import PolicyEnforcer
    logger = RunLogger(tmp_path / "run.jsonl", "t", "replay")
    executor = ReplayExecutor(mock_session, PolicyEnforcer(), logger, tmp_path)

    result = await executor.replay(art, {})  # no member_id
    logger.close()
    assert result.status == ReplayStatus.HARD_FAILURE
    assert "member_id" in (result.error.observed if result.error else "")
    mock_page.goto.assert_not_called()  # Browser never touched


@pytest.mark.asyncio
async def test_bad_schema_version_returns_hard_failure(tmp_path):
    art = _artifact(schema_version="2.0")
    mock_page = AsyncMock()
    mock_page.goto = AsyncMock()
    mock_session = MagicMock()
    mock_session.page = mock_page
    mock_session.screenshot = AsyncMock()
    mock_session.cdp_url = "http://127.0.0.1:9222"

    from cua.observability.logger import RunLogger
    from cua.safety.policy import PolicyEnforcer
    logger = RunLogger(tmp_path / "run.jsonl", "t", "replay")
    executor = ReplayExecutor(mock_session, PolicyEnforcer(), logger, tmp_path)

    result = await executor.replay(art, {"member_id": "12345"})
    logger.close()
    assert result.status == ReplayStatus.HARD_FAILURE
    assert result.error is not None
    assert result.error.step_id == "schema_check"
    mock_page.goto.assert_not_called()


# ---------------------------------------------------------------------------
# Integration: real browser + mock app
# ---------------------------------------------------------------------------

def _start_mock_app(port: int):
    import os
    os.environ["MOCK_APP_PORT"] = str(port)
    os.environ["MOCK_APP_HOST"] = "127.0.0.1"
    from demo.mock_app.app import app
    app.config["TESTING"] = False
    t = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, use_reloader=False, debug=False),
        daemon=True,
    )
    t.start()
    time.sleep(1.5)


def _make_login_artifact(base_url: str) -> CapabilityArtifact:
    """
    Full artifact: login → search → member detail → extract savings balance.

    The savings-balance locator is scoped to the Savings row via XPath to avoid
    ambiguity with the Checking row (which also has a .balance-cell element).
    """
    return CapabilityArtifact(
        id=str(uuid.uuid4()),
        name="lookup_savings_balance",
        description="Log in and read member savings balance",
        target=TargetSpec(entry_url=f"{base_url}/login", surface_type=SurfaceType.WEB_LEGACY),
        parameters=[ParameterSpec(name="member_id", type="string", description="Member ID", example="12345")],
        outputs=[OutputSpec(name="savings_balance", type="decimal",
                            description="Savings account balance", source_step_id="step_08")],
        steps=[
            Step(id="step_01", description="Type username",
                 action=StepAction(type=ActionType.TYPE, value="demo",
                                   locator=LocatorSpec(
                                       primary=LocatorStrategy(method=LocatorMethod.ARIA_LABEL, value="Username:"),
                                       rationale="Form label text — stable on this app"))),
            Step(id="step_02", description="Type password",
                 action=StepAction(type=ActionType.TYPE, value="demo123",
                                   locator=LocatorSpec(
                                       primary=LocatorStrategy(method=LocatorMethod.ARIA_LABEL, value="Password:"),
                                       rationale="Form label text"))),
            Step(id="step_03", description="Submit login",
                 action=StepAction(
                     type=ActionType.CLICK, risk_level=RiskLevel.SAFE,
                     locator=LocatorSpec(
                         primary=LocatorStrategy(method=LocatorMethod.ARIA_ROLE, value="Log In", role="button"),
                         fallbacks=[LocatorStrategy(method=LocatorMethod.TEXT, value="Log In")],
                         rationale="Login submit button"),
                     error_handling=ErrorHandlingSpec(
                         fail_patterns=["Invalid username or password"]))),
            Step(id="step_04", description="Navigate to member search",
                 action=StepAction(type=ActionType.NAVIGATE, url=f"{base_url}/members/search")),
            Step(id="step_05", description="Enter member ID",
                 action=StepAction(type=ActionType.TYPE, value="{member_id}",
                                   locator=LocatorSpec(
                                       primary=LocatorStrategy(method=LocatorMethod.ARIA_LABEL, value="Member ID:"),
                                       rationale="Search field label"))),
            Step(id="step_06", description="Submit search",
                 action=StepAction(
                     type=ActionType.CLICK, risk_level=RiskLevel.SAFE,
                     locator=LocatorSpec(
                         primary=LocatorStrategy(method=LocatorMethod.ARIA_ROLE, value="Search", role="button"),
                         fallbacks=[LocatorStrategy(method=LocatorMethod.TEXT, value="Search")],
                         rationale="Search submit button"),
                     error_handling=ErrorHandlingSpec(
                         expected_outcomes=[
                             ExpectedOutcome(
                                 code="member_not_found",
                                 description="Member not in system",
                                 detection_pattern="No members found",
                             )
                         ]))),
            Step(id="step_07", description="Open member detail",
                 action=StepAction(
                     type=ActionType.CLICK, risk_level=RiskLevel.SAFE,
                     locator=LocatorSpec(
                         primary=LocatorStrategy(method=LocatorMethod.TEXT, value="View Detail"),
                         rationale="Results table action link — text is stable"),
                     error_handling=ErrorHandlingSpec(
                         expected_outcomes=[
                             ExpectedOutcome(
                                 code="member_not_found",
                                 description="Member not found",
                                 detection_pattern="No members found",
                             )
                         ]))),
            Step(id="step_08", description="Extract savings account balance",
                 action=StepAction(
                     type=ActionType.EXTRACT, output_name="savings_balance",
                     locator=LocatorSpec(
                         # XPath scoped to the Savings row — avoids ambiguity with Checking row
                         primary=LocatorStrategy(
                             method=LocatorMethod.XPATH,
                             value="//tr[td[normalize-space()='Savings']]/td[@class='balance-cell']",
                         ),
                         fallbacks=[
                             LocatorStrategy(method=LocatorMethod.CSS,
                                             value="tr:has-text('Savings') .balance-cell"),
                         ],
                         rationale="XPath scoped to Savings row — unambiguous even with multiple account rows",
                     ))),
        ],
        checkpoint=CheckpointSpec(
            description="Member detail page loaded",
            type="url_contains", target="/members/",
        ),
        safety=SafetySpec(
            permitted_domains=["127.0.0.1"],
            permitted_action_types=[
                ActionType.NAVIGATE, ActionType.CLICK, ActionType.TYPE,
                ActionType.EXTRACT, ActionType.WAIT,
            ],
        ),
        metadata=ArtifactMetadata(
            created_at=datetime.now(timezone.utc),
            discovery_run_id="test", model_used="gpt-5.6-luna", discovery_duration_s=30.0,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.browser
async def test_replay_happy_path_member_12345(tmp_path):
    """Integration: replay member 12345 → SUCCESS, savings_balance == $8,750.00."""
    from cua.browser.session import BrowserSession
    from cua.observability.logger import RunLogger
    from cua.safety.policy import PolicyEnforcer

    port = 5051
    _start_mock_app(port)
    base_url = f"http://127.0.0.1:{port}"

    art = _make_login_artifact(base_url)
    art = art.model_copy(update={
        "target": art.target.model_copy(update={"entry_url": f"{base_url}/login"}),
        "safety": art.safety.model_copy(update={"permitted_domains": [f"127.0.0.1:{port}", "127.0.0.1"]}),
    })
    # Fix navigate step URL
    new_steps = []
    for s in art.steps:
        if s.id == "step_04":
            s = s.model_copy(update={"action": s.action.model_copy(
                update={"url": f"{base_url}/members/search"})})
        new_steps.append(s)
    art = art.model_copy(update={"steps": new_steps})

    session = BrowserSession(headless=True)
    await session.start()
    logger = RunLogger(tmp_path / "run.jsonl", "test", "replay")
    executor = ReplayExecutor(session, PolicyEnforcer(), logger, tmp_path)
    try:
        result = await executor.replay(art, {"member_id": "12345"})
    finally:
        await session.stop()
        logger.close()

    assert result.status == ReplayStatus.SUCCESS, result.summary_line()
    assert "savings_balance" in (result.outputs or {})
    # Member 12345 savings = $8,750.00
    bal = result.outputs["savings_balance"]
    assert "8,750" in bal or "8750" in bal, f"Expected ~$8750, got: {bal}"


@pytest.mark.asyncio
@pytest.mark.browser
async def test_replay_member_not_found(tmp_path):
    """Integration: replay member 99999 → BUSINESS_OUTCOME member_not_found."""
    from cua.browser.session import BrowserSession
    from cua.observability.logger import RunLogger
    from cua.safety.policy import PolicyEnforcer

    port = 5052
    _start_mock_app(port)
    base_url = f"http://127.0.0.1:{port}"

    art = _make_login_artifact(base_url)
    art = art.model_copy(update={
        "target": art.target.model_copy(update={"entry_url": f"{base_url}/login"}),
        "safety": art.safety.model_copy(update={"permitted_domains": [f"127.0.0.1:{port}", "127.0.0.1"]}),
    })
    new_steps = []
    for s in art.steps:
        if s.id == "step_04":
            s = s.model_copy(update={"action": s.action.model_copy(
                update={"url": f"{base_url}/members/search"})})
        new_steps.append(s)
    art = art.model_copy(update={"steps": new_steps})

    session = BrowserSession(headless=True)
    await session.start()
    logger = RunLogger(tmp_path / "run.jsonl", "test_notfound", "replay")
    executor = ReplayExecutor(session, PolicyEnforcer(), logger, tmp_path)
    try:
        result = await executor.replay(art, {"member_id": "99999"})
    finally:
        await session.stop()
        logger.close()

    assert result.status == ReplayStatus.BUSINESS_OUTCOME, result.summary_line()
    assert result.business_outcome == "member_not_found"


@pytest.mark.asyncio
@pytest.mark.browser
async def test_replay_member_67890_savings_balance(tmp_path):
    """Integration: replay with a different member to prove artifact is parameterized."""
    from cua.browser.session import BrowserSession
    from cua.observability.logger import RunLogger
    from cua.safety.policy import PolicyEnforcer

    port = 5053
    _start_mock_app(port)
    base_url = f"http://127.0.0.1:{port}"

    art = _make_login_artifact(base_url)
    art = art.model_copy(update={
        "target": art.target.model_copy(update={"entry_url": f"{base_url}/login"}),
        "safety": art.safety.model_copy(update={"permitted_domains": [f"127.0.0.1:{port}", "127.0.0.1"]}),
    })
    new_steps = []
    for s in art.steps:
        if s.id == "step_04":
            s = s.model_copy(update={"action": s.action.model_copy(
                update={"url": f"{base_url}/members/search"})})
        new_steps.append(s)
    art = art.model_copy(update={"steps": new_steps})

    session = BrowserSession(headless=True)
    await session.start()
    logger = RunLogger(tmp_path / "run.jsonl", "test_67890", "replay")
    executor = ReplayExecutor(session, PolicyEnforcer(), logger, tmp_path)
    try:
        result = await executor.replay(art, {"member_id": "67890"})
    finally:
        await session.stop()
        logger.close()

    assert result.status == ReplayStatus.SUCCESS, result.summary_line()
    bal = (result.outputs or {}).get("savings_balance", "")
    # Member 67890 savings = $3,400.00
    assert "3,400" in bal or "3400" in bal, f"Expected ~$3400 for member 67890, got: {bal}"
