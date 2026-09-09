"""Tests for escalation handler — non-interactive mode."""

from __future__ import annotations

from pathlib import Path

import pytest

from cua.escalation.handler import EscalationHandler, EscalationOutcome, EscalationRequest


@pytest.mark.asyncio
async def test_non_interactive_returns_false():
    handler = EscalationHandler(interactive=False)
    req = EscalationRequest(
        run_id="test-01",
        reason="Agent stuck on CAPTCHA",
        current_state="Page shows CAPTCHA challenge",
        goal="Look up member balance",
        step_num=5,
        cdp_url="http://127.0.0.1:9222",
    )
    outcome = await handler.handle(req)
    assert isinstance(outcome, EscalationOutcome)
    assert outcome.resumed is False
    assert outcome.human_action_description is None


@pytest.mark.asyncio
async def test_escalation_outcome_fields():
    outcome = EscalationOutcome(resumed=True, human_action_description="Dismissed the CAPTCHA manually")
    assert outcome.resumed is True
    assert "CAPTCHA" in outcome.human_action_description


def test_escalation_request_fields():
    req = EscalationRequest(
        run_id="run-42",
        reason="Permission denied",
        current_state="Page shows: Access Denied",
        goal="Open sub-account",
        step_num=3,
        cdp_url="http://127.0.0.1:9222",
        screenshot_path=Path("evidence/escalation.png"),
    )
    assert req.run_id == "run-42"
    assert req.step_num == 3
    assert req.screenshot_path == Path("evidence/escalation.png")
