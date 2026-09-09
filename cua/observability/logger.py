# Structured JSONL run logger. Writes one event per line and mirrors
# key events to the console. Sensitive values passed at construction
# are redacted everywhere — goals, reasoning, extracted text, errors.

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
        self._emit("run_start", goal=self._redact(goal), params=safe_params)
        console.rule(f"[bold blue]{self._run_type.upper()} RUN  {self._run_id}")
        console.print(f"[dim]Goal:[/dim] {self._redact(goal)}")
        if safe_params:
            console.print(f"[dim]Params:[/dim] {safe_params}")

    def step_start(self, step_id: str, description: str, action_type: str) -> None:
        self._emit("step_start", step_id=step_id,
                   description=self._redact(description), action=action_type)
        console.print(f"  [cyan]→[/cyan] [{step_id}] {description}")

    def step_success(self, step_id: str, extracted: str | None = None) -> None:
        safe = self._redact(extracted) if extracted else None
        self._emit("step_success", step_id=step_id, extracted=safe)
        suffix = f" → [italic]{safe}[/italic]" if safe else ""
        console.print(f"  [green]✓[/green] [{step_id}]{suffix}")

    def step_failure(self, step_id: str, expected: str, observed: str) -> None:
        self._emit("step_failure", step_id=step_id,
                   expected=self._redact(expected), observed=self._redact(observed))
        console.print(f"  [red]✗[/red] [{step_id}] expected={expected!r}")

    def step_error(self, step_id: str, error: str) -> None:
        self._emit("step_error", step_id=step_id, error=self._redact(error))
        console.print(f"  [red]![/red] [{step_id}] Error: {self._redact(error)}")

    def business_outcome(self, code: str, description: str) -> None:
        self._emit("business_outcome", code=code, description=description)
        console.print(f"[yellow]⚑  Business outcome:[/yellow] {code} — {description}")

    def recovered(self, condition: str) -> None:
        self._emit("recovered", condition=condition)
        console.print(f"[yellow]↺  Recovered:[/yellow] {condition}")

    def escalated(self, reason: str) -> None:
        self._emit("escalated", reason=self._redact(reason))
        console.print(f"[bold yellow]⚠  Escalated:[/bold yellow] {self._redact(reason)}")

    def human_action(self, description: str) -> None:
        safe = self._redact(description) if description else "(no description)"
        self._emit("human_action", description=safe)
        console.print(f"[magenta]👤 Human:[/magenta] {safe}")

    def run_success(self, outputs: dict[str, Any]) -> None:
        safe = {k: self._redact(str(v)) for k, v in outputs.items()}
        self._emit("run_success", outputs=safe)
        console.print(f"\n[bold green]✓ SUCCESS[/bold green]")
        for k, v in safe.items():
            console.print(f"  {k} = [bold]{v}[/bold]")

    def run_failure(self, status: str, error: dict | None = None) -> None:
        self._emit("run_failure", status=status, error=error)
        console.print(f"\n[bold red]✗ {status.upper()}[/bold red]")
        if error:
            console.print(f"  step={error.get('step_id')} expected={error.get('expected')!r}")

    def agent_reasoning(self, step_num: int, tool: str, reasoning: str | None) -> None:
        safe = self._redact(reasoning) if reasoning else None
        self._emit("agent_reasoning", step_num=step_num, tool=tool, reasoning=safe)
        console.print(f"  [dim][model] step {step_num}: {tool}[/dim]")

    def close(self) -> None:
        self._fh.close()

    def _redact(self, text: str | None) -> str | None:
        if text is None:
            return None
        for val in self._sensitive_values:
            text = text.replace(val, "[REDACTED]")
        return text

    def _redact_params(self, params: dict[str, str]) -> dict[str, str]:
        return {
            k: "[REDACTED]" if k in self._sensitive_names else self._redact(v) or v
            for k, v in params.items()
        }

    def _emit(self, event: str, **kwargs: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "event": event,
            **kwargs,
        }
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()
