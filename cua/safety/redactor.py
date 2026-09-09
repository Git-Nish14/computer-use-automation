# Strips PII patterns and known sensitive values from log text.

from __future__ import annotations

import re

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_ACCOUNT_RE = re.compile(r"\b\d{10,17}\b")


def redact_text(text: str, sensitive_values: list[str] | None = None) -> str:
    result = text
    if sensitive_values:
        for val in sensitive_values:
            if val:
                result = result.replace(val, "[REDACTED]")
    result = _SSN_RE.sub("[SSN-REDACTED]", result)
    result = _ACCOUNT_RE.sub("[ACCT-REDACTED]", result)
    return result


def redact_params(params: dict[str, str], sensitive_names: list[str]) -> dict[str, str]:
    return {
        k: "[REDACTED]" if k in sensitive_names else v
        for k, v in params.items()
    }
