# Discovery CLI — drives the browser with gpt-5.6-luna and saves the artifact.
# See README.md for the full demo command.

from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console

load_dotenv()
console = Console()
sys.path.insert(0, str(Path(__file__).parent.parent))

from cua.agent.loop import DEFAULT_MODEL, DiscoveryAgent
from cua.artifact.schema import OutputSpec, ParameterSpec
from cua.artifact.store import ArtifactStore
from cua.browser.session import BrowserSession
from cua.escalation.handler import EscalationHandler
from cua.observability.logger import RunLogger


def parse_param(value: str, sensitive: bool = False) -> ParameterSpec:
    """Parse name=value or name=value:description (quotes around desc optional)."""
    m = re.match(r'^(\w+)=([^:]+)(?::(?:"(.+?)"|(.+)))?$', value)
    if not m:
        raise click.BadParameter(
            f"Expected name=value or name=value:description, got: {value!r}"
        )
    name = m.group(1)
    example = m.group(2).strip()
    desc = (m.group(3) or m.group(4) or f"The {name} value").strip()
    return ParameterSpec(
        name=name, type="string", description=desc,
        # Always keep example for parameterization; _build_artifact clears it for sensitive
        example=example,
        sensitive=sensitive, required=True,
    )


def parse_output(value: str) -> OutputSpec:
    """Parse name:type or name:type:description."""
    parts = value.split(":", 2)
    name = parts[0]
    typ = parts[1] if len(parts) > 1 and parts[1] in ("string", "decimal", "integer", "boolean") else "string"
    desc = parts[2].strip() if len(parts) > 2 else f"The {name} value"
    return OutputSpec(name=name, type=typ, description=desc, source_step_id="tbd")


@click.command()
@click.option("--goal", required=True, help="Natural-language goal for the agent")
@click.option("--url", required=True, help="Entry URL for the target application")
@click.option("--output", default=None, help="Artifact save path (default: capabilities/<slug>.json)")
@click.option("--param", "params", multiple=True, help='Parameterized value: name=value[:desc]')
@click.option("--sensitive-param", "sensitive_params", multiple=True,
              help='Sensitive param (never logged): name=value[:desc]')
@click.option("--output-spec", "output_specs", multiple=True,
              help='Output binding: name:type[:desc]')
@click.option("--model", default=None, help=f"OpenAI model (default: $OPENAI_MODEL or {DEFAULT_MODEL})")
@click.option("--max-steps", default=30, show_default=True)
@click.option("--headless", is_flag=True, default=False)
@click.option("--evidence-dir", default="evidence", show_default=True)
@click.option("--no-interactive", is_flag=True, default=False,
              help="Disable interactive escalation (auto-abort)")
@click.option("--permitted-domain", "permitted_domains", multiple=True,
              help="Allowed domain (default: host from --url)")
def main(
    goal, url, output, params, sensitive_params, output_specs,
    model, max_steps, headless, evidence_dir, no_interactive, permitted_domains,
):
    """Run an LLM-driven discovery session and save the capability artifact."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        console.print("[red]OPENAI_API_KEY not set — copy .env.example → .env[/red]")
        sys.exit(1)

    model = model or os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
    headless = headless or os.environ.get("BROWSER_HEADLESS", "false").lower() == "true"
    cdp_port = int(os.environ.get("BROWSER_CDP_PORT", "9222"))

    param_specs = [parse_param(p) for p in params]
    param_specs += [parse_param(p, sensitive=True) for p in sensitive_params]
    output_spec_list = [parse_output(o) for o in output_specs]

    from urllib.parse import urlparse
    if not permitted_domains:
        permitted_domains = (urlparse(url).netloc,)

    run_ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    ev_dir = Path(evidence_dir) / f"discovery_{run_ts}"
    ev_dir.mkdir(parents=True, exist_ok=True)

    asyncio.run(_run(
        goal=goal, url=url, output=output, param_specs=param_specs,
        output_spec_list=output_spec_list, model=model, max_steps=max_steps,
        headless=headless, cdp_port=cdp_port, ev_dir=ev_dir,
        interactive=not no_interactive,
        permitted_domains=list(permitted_domains),
    ))


async def _run(goal, url, output, param_specs, output_spec_list,
               model, max_steps, headless, cdp_port, ev_dir, interactive, permitted_domains):
    log_path = ev_dir / "run.jsonl"

    # Pass actual sensitive values to logger so they're redacted in all log text
    sensitive_values = [p.example for p in param_specs if p.sensitive and p.example]
    logger = RunLogger(
        log_path, run_id="discovery", run_type="discovery",
        sensitive_param_names=[p.name for p in param_specs if p.sensitive],
        sensitive_values=sensitive_values,
    )

    has_sensitive = any(p.sensitive for p in param_specs)
    session = BrowserSession(headless=headless, cdp_port=cdp_port, trace_dir=ev_dir,
                             capture_snapshots=not has_sensitive)
    await session.start()

    escalation = EscalationHandler(interactive=interactive, logger=logger)
    agent = DiscoveryAgent(
        session=session, logger=logger, escalation_handler=escalation,
        model=model, max_steps=max_steps, permitted_domains=permitted_domains,
    )

    try:
        result = await agent.run(
            goal=goal, entry_url=url,
            parameters=param_specs, outputs=output_spec_list, evidence_dir=ev_dir,
        )
    finally:
        trace_path = ev_dir / "trace.zip"
        await session.stop(trace_path=trace_path)
        logger.close()

    if result.status != "success" or result.artifact is None:
        console.print(f"\n[red]Discovery failed: {result.status}[/red]")
        console.print(f"Summary: {result.summary}")
        sys.exit(1)

    artifact = result.artifact
    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
        actual_path = output
    else:
        actual_path = str(ArtifactStore().save(artifact))

    console.print(f"\n[bold green]Artifact saved:[/bold green] {actual_path}")
    console.print(f"Evidence:       {ev_dir}")
    console.print(f"Steps recorded: {len(result.actions)}")
    console.print(f"Duration:       {result.duration_s:.1f}s")


if __name__ == "__main__":
    main()
