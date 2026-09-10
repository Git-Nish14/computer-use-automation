# Structured JSONL logger.
# _emit applies recursive sanitization to every value before writing,
# so no sensitive literal can survive in the log regardless of which method calls it.

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console

console = Console()


class RunLogger:
    def __init__(
        self,
        log_path: Path,
        run_id: str,
        run_type: str,
        sensitive_param_names: list[str] | None = None,
        sensitive_values: list[str] | None = None,
    ):
        self._path = log_path
        self._run_id = run_id
        self._run_type = run_type
        self._sensitive_names = set(sensitive_param_names or [])
        self._sensitive_values = [v for v in (sensitive_values or []) if v]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = log_path.open("w", encoding="utf-8")

    def run_start(self, goal: str, params: dict[str, str]) -> None:
        safe_params = self._redact_params(params)
        self._emit("run_start", goal=goal, params=safe_params)
        console.rule(f"[bold blue]{self._run_type.upper()} RUN  {self._run_id}")
        console.print(f"[dim]Goal:[/dim] {self._redact(goal)}")
        if safe_params:
            console.print(f"[dim]Params:[/dim] {safe_params}")

    def step_start(self, step_id: str, description: str, action_type: str) -> None:
        self._emit("step_start", step_id=step_id, description=description, action=action_type)
        console.print(f"  [cyan]→[/cyan] [{step_id}] {description}")

    def step_success(self, step_id: str, extracted: str | None = None) -> None:
        self._emit("step_success", step_id=step_id, extracted=extracted)
        suffix = f" → [italic]{self._redact(extracted)}[/italic]" if extracted else ""
        console.print(f"  [green]✓[/green] [{step_id}]{suffix}")

    def step_failure(self, step_id: str, expected: str, observed: str) -> None:
        self._emit("step_failure", step_id=step_id, expected=expected, observed=observed)
        console.print(f"  [red]✗[/red] [{step_id}] expected={self._redact(expected)!r}")

    def step_error(self, step_id: str, error: str) -> None:
        self._emit("step_error", step_id=step_id, error=error)
        console.print(f"  [red]![/red] [{step_id}] Error: {self._redact(error)}")

    def business_outcome(self, code: str, description: str) -> None:
        self._emit("business_outcome", code=code, description=description)
        console.print(f"[yellow]⚑  Business outcome:[/yellow] {code} — {description}")

    def recovered(self, condition: str) -> None:
        # Called only AFTER verification confirms the condition cleared.
        self._emit("recovered", condition=condition)
        console.print(f"[yellow]↺  Recovered:[/yellow] {condition}")

    def escalated(self, reason: str) -> None:
        self._emit("escalated", reason=reason)
        console.print(f"[bold yellow]⚠  Escalated:[/bold yellow] {self._redact(reason)}")

    def handoff_start(self, step_id: str, reason: str) -> None:
        self._emit("handoff_start", step_id=step_id, reason=reason)
        console.print(f"[magenta]⏸  Handoff started:[/magenta] [{step_id}] {self._redact(reason)}")

    def handoff_end(self, step_id: str, resumed: bool) -> None:
        outcome = "resumed" if resumed else "aborted"
        self._emit("handoff_end", step_id=step_id, resumed=resumed, outcome=outcome)
        color = "green" if resumed else "red"
        console.print(f"[{color}]▶  Handoff {outcome}:[/{color}] [{step_id}]")

    def human_action(self, description: str) -> None:
        # Human-provided description only. Automated steps use step_success.
        self._emit("human_action", description=description)
        console.print(f"[magenta]👤 Human:[/magenta] {self._redact(description)}")

    def run_success(self, outputs: dict[str, Any]) -> None:
        self._emit("run_success", outputs=outputs)
        console.print(f"\n[bold green]✓ SUCCESS[/bold green]")
        for k, v in outputs.items():
            console.print(f"  {k} = [bold]{self._redact(str(v))}[/bold]")

    def run_failure(self, status: str, error: dict | None = None) -> None:
        self._emit("run_failure", status=status, error=error)
        console.print(f"\n[bold red]✗ {status.upper()}[/bold red]")
        if error:
            step = error.get("step_id", "?")
            expected = self._redact(str(error.get("expected", "")))
            console.print(f"  step={step} expected={expected!r}")

    def agent_reasoning(self, step_num: int, tool: str, reasoning: str | None) -> None:
        self._emit("agent_reasoning", step_num=step_num, tool=tool, reasoning=reasoning)
        console.print(f"  [dim][model] step {step_num}: {tool}[/dim]")

    def close(self) -> None:
        self._fh.close()

    def _redact(self, text: str | None) -> str | None:
        if text is None:
            return None
        for val in self._sensitive_values:
            text = text.replace(val, "[REDACTED]")
        from cua.safety.redactor import redact_text
        return redact_text(text)

    def _redact_params(self, params: dict[str, str]) -> dict[str, str]:
        return {
            k: "[REDACTED]" if k in self._sensitive_names else (self._redact(v) or v)
            for k, v in params.items()
        }

    def _emit(self, event: str, **kwargs: Any) -> None:
        # Apply recursive sanitization to all string values before writing.
        # This is the single persistence boundary — no sensitive literal can escape it.
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "event": event,
            **{k: self._clean(v) for k, v in kwargs.items()},
        }
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()

    def _clean(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact(value) or value
        if isinstance(value, dict):
            return {k: self._clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        return value
