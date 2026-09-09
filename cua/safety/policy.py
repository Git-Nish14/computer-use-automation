# Enforces domain + action allowlists before every browser action.
# Effective risk = max(declared, type-default) so SUBMIT is always HIGH
# even when the artifact left risk_level at its default of SAFE.

from __future__ import annotations

from urllib.parse import urlparse

from cua.artifact.schema import ActionType, RiskLevel, SafetySpec, StepAction

_ACTION_RISK: dict[ActionType, RiskLevel] = {
    ActionType.NAVIGATE: RiskLevel.SAFE,
    ActionType.CLICK: RiskLevel.SAFE,
    ActionType.EXTRACT: RiskLevel.SAFE,
    ActionType.WAIT: RiskLevel.SAFE,
    ActionType.ASSERT: RiskLevel.SAFE,
    ActionType.DISMISS_DIALOG: RiskLevel.SAFE,
    ActionType.TYPE: RiskLevel.MODERATE,
    ActionType.SELECT: RiskLevel.MODERATE,
    ActionType.SUBMIT: RiskLevel.HIGH,
}

_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.SAFE: 0,
    RiskLevel.MODERATE: 1,
    RiskLevel.HIGH: 2,
}


class PolicyViolation(Exception):
    pass


def _hostname(url_or_host: str) -> str:
    """Extracts hostname safely, handles IPv6 and auth-info in URLs."""
    if "://" not in url_or_host:
        url_or_host = f"http://{url_or_host}"
    return urlparse(url_or_host).hostname or url_or_host.split(":")[0]


class PolicyEnforcer:
    def __init__(self, allow_high_risk: bool = False):
        self._allow_high_risk = allow_high_risk

    def check(self, safety: SafetySpec, action: StepAction) -> None:
        if action.type not in safety.permitted_action_types:
            raise PolicyViolation(
                f"Action type '{action.type.value}' is not in the permitted list: "
                f"{[t.value for t in safety.permitted_action_types]}"
            )

        # Approval requirement is independent of risk — checked first so an
        # approval-required CLICK stops even when effective risk is SAFE.
        if action.type in safety.requires_human_approval_for:
            raise PolicyViolation(
                f"Action '{action.type.value}' requires human approval before execution "
                "(listed in safety.requires_human_approval_for)."
            )

        effective_risk = max(
            action.risk_level,
            _ACTION_RISK.get(action.type, RiskLevel.SAFE),
            key=lambda r: _RISK_ORDER[r],
        )

        if effective_risk == RiskLevel.HIGH and not self._allow_high_risk:
            raise PolicyViolation(
                f"Action '{action.type.value}' has effective risk=HIGH. "
                "Pass --allow-high-risk or add it to requires_human_approval_for."
            )

    def check_url(self, safety: SafetySpec, url: str) -> None:
        url_host = _hostname(url)
        allowed_hosts = [_hostname(d) for d in safety.permitted_domains]
        if url_host not in allowed_hosts:
            raise PolicyViolation(
                f"Domain '{url_host}' (from '{url}') is not in "
                f"permitted_domains: {safety.permitted_domains}"
            )
