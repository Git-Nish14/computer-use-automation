# Typed result returned by ReplayExecutor after every run.
# Callers branch on `status` — SUCCESS carries outputs,
# BUSINESS_OUTCOME is a known non-failure state, everything else is an error.

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel


class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERED = "recovered"
    HARD_FAILURE = "hard_failure"
    ESCALATED = "escalated"
    POLICY_VIOLATION = "policy_violation"


class ErrorDetail(BaseModel):
    step_id: str
    step_description: str
    expected: str
    observed: str
    screenshot_path: str | None = None
    timestamp: datetime


class ReplayResult(BaseModel):
    run_id: str
    artifact_id: str
    artifact_name: str
    status: ReplayStatus
    outputs: dict[str, Any] | None = None
    business_outcome: str | None = None
    business_outcome_description: str | None = None
    recovered_conditions: list[str] = []
    error: ErrorDetail | None = None
    steps_completed: int
    total_steps: int
    duration_s: float
    evidence_path: str | None = None

    def is_success(self) -> bool:
        return self.status == ReplayStatus.SUCCESS

    def is_business_outcome(self) -> bool:
        return self.status == ReplayStatus.BUSINESS_OUTCOME

    def summary_line(self) -> str:
        if self.status == ReplayStatus.SUCCESS:
            return f"SUCCESS — {self.outputs}"
        if self.status == ReplayStatus.BUSINESS_OUTCOME:
            return f"BUSINESS_OUTCOME [{self.business_outcome}] — {self.business_outcome_description}"
        if self.error:
            return (
                f"{self.status.upper()} at {self.error.step_id}: "
                f"expected={self.error.expected!r} observed={self.error.observed!r}"
            )
        return self.status.upper()
