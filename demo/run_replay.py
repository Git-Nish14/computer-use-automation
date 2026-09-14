# Replay CLI — replays a saved artifact without calling the LLM.
# Exit codes: 0 success, 1 hard failure, 2 business outcome, 3 policy violation.

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console

load_dotenv()
console = Console()
sys.path.insert(0, str(Path(__file__).parent.parent))

from cua.artifact.store import ArtifactStore
from cua.browser.session import BrowserSession
from cua.escalation.handler import EscalationHandler
from cua.observability.logger import RunLogger
from cua.replay.executor import ReplayExecutor
from cua.replay.result import ReplayStatus
from cua.safety.policy import PolicyEnforcer
from cua.safety.redactor import redact_params


@click.command()
@click.option("--artifact", required=True, type=click.Path(exists=True))
@click.option("--params", "param_pairs", multiple=True, help="name=value")
@click.option("--params-json", default=None, help="Parameters as JSON object string")
@click.option("--headless", is_flag=True, default=False)
@click.option("--allow-high-risk", is_flag=True, default=False)
@click.option("--escalate-on-failure", is_flag=True, default=False,
              help="Pause on hard failure for human intervention via CDP")
@click.option("--evidence-dir", default="evidence", show_default=True)
def main(artifact, param_pairs, params_json, headless, allow_high_risk,
         escalate_on_failure, evidence_dir):
    """Replay a capability artifact deterministically."""
    store = ArtifactStore()
    cap = store.load(artifact)

    params: dict[str, str] = {}
    if params_json:
        params.update(json.loads(params_json))
    for pair in param_pairs:
        if "=" not in pair:
            console.print(f"[red]Invalid param (expected name=value): {pair!r}[/red]")
            sys.exit(1)
        k, v = pair.split("=", 1)
        params[k.strip()] = v.strip()

    headless = headless or os.environ.get("BROWSER_HEADLESS", "false").lower() == "true"
    cdp_port = int(os.environ.get("BROWSER_CDP_PORT", "9222"))

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    ev_dir = Path(evidence_dir) / f"replay_{run_ts}"
    ev_dir.mkdir(parents=True, exist_ok=True)

    safe_params = redact_params(params, cap.safety.sensitive_param_names)
    console.print(f"Replaying: [bold]{cap.name}[/bold]")
    console.print(f"Params: {safe_params}")

    result = asyncio.run(
        _run(cap, params, headless, cdp_port, allow_high_risk, escalate_on_failure, ev_dir)
    )

    console.print(f"\nEvidence: {ev_dir}")

    if result.status == ReplayStatus.SUCCESS:
        console.print("\n[bold green]SUCCESS[/bold green]")
        for k, v in (result.outputs or {}).items():
            console.print(f"  {k} = [bold]{v}[/bold]")
        sys.exit(0)
    elif result.status == ReplayStatus.BUSINESS_OUTCOME:
        console.print(f"\n[yellow]BUSINESS OUTCOME: {result.business_outcome}[/yellow]")
        console.print(f"  {result.business_outcome_description}")
        sys.exit(2)
    elif result.status == ReplayStatus.POLICY_VIOLATION:
        console.print("\n[red]POLICY VIOLATION[/red]")
        if result.error:
            console.print(f"  {result.error.observed}")
        sys.exit(3)
    else:
        console.print(f"\n[red]{result.status.upper()}[/red]")
        if result.error:
            console.print(f"  Step:     {result.error.step_id}")
            console.print(f"  Expected: {result.error.expected}")
            console.print(f"  Observed: {result.error.observed}")
            if result.error.screenshot_path:
                console.print(f"  Screenshot: {result.error.screenshot_path}")
        sys.exit(1)


async def _run(cap, params, headless, cdp_port, allow_high_risk, escalate_on_failure, ev_dir):
    log_path = ev_dir / "run.jsonl"
    # Replay logger needs both names and actual values for sensitive params,
    # so it can redact them if they appear in free-text fields like errors or descriptions.
    sensitive_values = [params[n] for n in cap.safety.sensitive_param_names if n in params]
    logger = RunLogger(
        log_path, run_id="replay", run_type="replay",
        sensitive_param_names=cap.safety.sensitive_param_names,
        sensitive_values=sensitive_values,
    )
    has_sensitive = bool(cap.safety.sensitive_param_names)
    session = BrowserSession(headless=headless, cdp_port=cdp_port, trace_dir=ev_dir,
                             capture_snapshots=not has_sensitive)
    await session.start()

    escalation = EscalationHandler(interactive=True, logger=logger) if escalate_on_failure else None
    policy = PolicyEnforcer(allow_high_risk=allow_high_risk)
    executor = ReplayExecutor(
        session=session, policy=policy, logger=logger,
        evidence_dir=ev_dir, escalation_handler=escalation,
    )

    try:
        result = await executor.replay(cap, params)
    finally:
        trace_path = ev_dir / "trace.zip"
        await session.stop(trace_path=trace_path)
        logger.close()

    return result


if __name__ == "__main__":
    main()
