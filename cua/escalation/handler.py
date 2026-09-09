# Human-in-the-loop handoff.
# Pauses automation, exposes the live browser via CDP remote debugging,
# waits for the operator to finish, then resumes. The operator surface
# here is a CLI prompt — in production this would be a WebSocket console.

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

console = Console()


@dataclass
class EscalationRequest:
    run_id: str
    reason: str
    current_state: str
    goal: str
    step_num: int
    cdp_url: str
    screenshot_path: Path | None = None


@dataclass
class EscalationOutcome:
    resumed: bool
    human_action_description: str | None = None


class EscalationHandler:
    def __init__(self, interactive: bool = True, logger=None):
        self._interactive = interactive
        self._logger = logger

    async def handle(self, req: EscalationRequest) -> EscalationOutcome:
        self._print_escalation(req)

        if not self._interactive:
            console.print("[yellow]Non-interactive mode: auto-aborting escalation[/yellow]")
            return EscalationOutcome(resumed=False)

        console.print(
            "\n[bold]Automation is paused.[/bold] Connect to the live browser:\n\n"
            f"  1. Open Chrome → [cyan]chrome://inspect/#devices[/cyan]\n"
            f"  2. Find the tab at [cyan]{req.cdp_url}[/cyan] and click Inspect\n"
            f"  3. Perform the required steps, then return here\n"
        )

        loop = asyncio.get_event_loop()
        try:
            choice = await loop.run_in_executor(
                None, lambda: input("\nType 'resume' to continue or 'abort' to stop: ")
            )
            choice = choice.strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "abort"

        if choice.startswith("r"):
            try:
                description = await loop.run_in_executor(
                    None, lambda: input("Briefly describe what you did (Enter to skip): "),
                )
                description = description.strip() or None
            except (EOFError, KeyboardInterrupt):
                description = None

            if self._logger and description:
                self._logger.human_action(description)

            console.print("[green]Resuming automation.[/green]")
            return EscalationOutcome(resumed=True, human_action_description=description)

        console.print("[red]Run aborted by operator.[/red]")
        return EscalationOutcome(resumed=False)

    def _print_escalation(self, req: EscalationRequest) -> None:
        content = (
            f"[bold red]⚠  ESCALATION REQUIRED[/bold red]\n\n"
            f"Run ID  : {req.run_id}\n"
            f"Goal    : {req.goal}\n"
            f"Step    : {req.step_num}\n"
            f"Reason  : {req.reason}\n\n"
            f"State   : {req.current_state}\n"
        )
        if req.screenshot_path and req.screenshot_path.exists():
            content += f"\nScreenshot: {req.screenshot_path}\n"
        console.print(Panel(content, title="Human Intervention Required", border_style="red"))
