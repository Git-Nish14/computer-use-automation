"""Tests for the policy enforcer and redactor."""

from __future__ import annotations

import pytest

from cua.artifact.schema import ActionType, RiskLevel, SafetySpec, StepAction
from cua.safety.policy import PolicyEnforcer, PolicyViolation
from cua.safety.redactor import redact_params, redact_text


def _safety(
    domains=None, action_types=None, requires_approval_for=None,
) -> SafetySpec:
    return SafetySpec(
        permitted_domains=domains or ["127.0.0.1:5000"],
        permitted_action_types=action_types or [
            ActionType.NAVIGATE, ActionType.CLICK, ActionType.TYPE,
            ActionType.EXTRACT, ActionType.WAIT, ActionType.ASSERT,
        ],
        requires_human_approval_for=requires_approval_for or [],
    )


class TestPolicyEnforcer:
    def test_safe_action_allowed(self):
        policy = PolicyEnforcer()
        policy.check(_safety(), StepAction(type=ActionType.NAVIGATE))  # no raise

    def test_action_not_in_allowlist_raises(self):
        policy = PolicyEnforcer()
        with pytest.raises(PolicyViolation, match="not in the permitted list"):
            policy.check(_safety(), StepAction(type=ActionType.SUBMIT))

    def test_submit_effective_risk_is_high_even_with_default_safe(self):
        """
        SUBMIT type-defaults to HIGH risk.  Even when risk_level=SAFE (the field default),
        the effective risk must be HIGH and the action blocked without allow_high_risk.
        """
        policy = PolicyEnforcer(allow_high_risk=False)
        action = StepAction(type=ActionType.SUBMIT)  # risk_level default = SAFE
        safety = _safety(action_types=[ActionType.SUBMIT])
        with pytest.raises(PolicyViolation, match="effective risk=HIGH"):
            policy.check(safety, action)

    def test_submit_allowed_when_flag_set(self):
        policy = PolicyEnforcer(allow_high_risk=True)
        action = StepAction(type=ActionType.SUBMIT)
        policy.check(_safety(action_types=[ActionType.SUBMIT]), action)  # no raise

    def test_high_risk_click_blocked(self):
        policy = PolicyEnforcer(allow_high_risk=False)
        action = StepAction(type=ActionType.CLICK, risk_level=RiskLevel.HIGH)
        safety = _safety(action_types=[ActionType.CLICK])
        with pytest.raises(PolicyViolation, match="effective risk=HIGH"):
            policy.check(safety, action)

    def test_requires_approval_always_blocks(self):
        """requires_human_approval_for blocks regardless of allow_high_risk."""
        policy = PolicyEnforcer(allow_high_risk=True)
        action = StepAction(type=ActionType.SUBMIT, risk_level=RiskLevel.HIGH)
        safety = _safety(
            action_types=[ActionType.SUBMIT],
            requires_approval_for=[ActionType.SUBMIT],
        )
        with pytest.raises(PolicyViolation, match="human approval"):
            policy.check(safety, action)

    def test_check_url_allowed_domain(self):
        policy = PolicyEnforcer()
        policy.check_url(_safety(domains=["127.0.0.1"]), "http://127.0.0.1:5000/login")

    def test_check_url_forbidden_domain(self):
        policy = PolicyEnforcer()
        with pytest.raises(PolicyViolation, match="permitted_domains"):
            policy.check_url(_safety(domains=["127.0.0.1"]), "http://evil.com/exfiltrate")

    def test_check_url_strips_port(self):
        policy = PolicyEnforcer()
        policy.check_url(_safety(domains=["127.0.0.1"]), "http://127.0.0.1:9999/path")

    def test_moderate_risk_always_allowed(self):
        """TYPE is MODERATE — never blocked by allow_high_risk guard."""
        policy = PolicyEnforcer(allow_high_risk=False)
        action = StepAction(type=ActionType.TYPE, risk_level=RiskLevel.MODERATE)
        policy.check(_safety(action_types=[ActionType.TYPE]), action)  # no raise


class TestRedactor:
    def test_redact_explicit_values(self):
        result = redact_text("password is secret123", sensitive_values=["secret123"])
        assert "secret123" not in result
        assert "[REDACTED]" in result

    def test_redact_ssn(self):
        result = redact_text("SSN: 123-45-6789")
        assert "123-45-6789" not in result
        assert "[SSN-REDACTED]" in result

    def test_redact_params_sensitive(self):
        params = {"member_id": "12345", "login_password": "demo123"}
        safe = redact_params(params, sensitive_names=["login_password"])
        assert safe["member_id"] == "12345"
        assert safe["login_password"] == "[REDACTED]"

    def test_redact_params_non_sensitive_unchanged(self):
        params = {"member_id": "12345"}
        assert redact_params(params, sensitive_names=[]) == params

    def test_no_sensitive_values_unchanged(self):
        assert redact_text("hello world") == "hello world"
