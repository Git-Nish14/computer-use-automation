# LLM-driven discovery loop. Drives a real browser with gpt-5.6-luna,
# enforces the domain + action allowlist, verifies all declared outputs
# are present before accepting complete, then builds the artifact.

from __future__ import annotations

import json as _json
import re
import time
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from openai import AsyncOpenAI
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from cua.agent.prompts import build_messages
from cua.agent.tools import AGENT_TOOLS
from cua.artifact.schema import (
    ActionType,
    ArtifactMetadata,
    CapabilityArtifact,
    CheckpointSpec,
    ErrorHandlingSpec,
    ExpectedOutcome,
    LocatorMethod,
    LocatorSpec,
    LocatorStrategy,
    OutputSpec,
    ParameterSpec,
    RiskLevel,
    SafetySpec,
    Step,
    StepAction,
    SurfaceType,
    TargetSpec,
)
from cua.browser.observer import observe
from cua.browser.session import BrowserSession
from cua.escalation.handler import EscalationHandler, EscalationOutcome, EscalationRequest
from cua.observability.logger import RunLogger
from cua.safety.policy import PolicyEnforcer, PolicyViolation

DEFAULT_MODEL = "gpt-5.6-luna"


@dataclass
class ActionRecord:
    step_num: int
    tool_name: str
    tool_input: dict
    locator_spec: LocatorSpec | None
    extracted_value: str | None
    url_before: str
    url_after: str
    success: bool
    error: str | None = None
    output_name: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class DiscoveryResult:
    run_id: str
    status: str   # "success" | "escalated" | "failed" | "max_steps"
    artifact: CapabilityArtifact | None
    summary: str
    extracted_data: dict[str, str]
    actions: list[ActionRecord]
    duration_s: float
    evidence_dir: Path


class DiscoveryAgent:
    def __init__(
        self,
        session: BrowserSession,
        logger: RunLogger,
        escalation_handler: EscalationHandler,
        model: str = DEFAULT_MODEL,
        max_steps: int = 30,
        permitted_domains: list[str] | None = None,
    ):
        self._session = session
        self._logger = logger
        self._escalation = escalation_handler
        self._model = model
        self._max_steps = max_steps
        self._permitted_domains = list(permitted_domains or [])
        self._policy = PolicyEnforcer(allow_high_risk=False)
        self._client = AsyncOpenAI()

    async def run(
        self,
        goal: str,
        entry_url: str,
        parameters: list[ParameterSpec],
        outputs: list[OutputSpec],
        evidence_dir: Path,
    ) -> DiscoveryResult:
        run_id = str(_uuid.uuid4())[:8]
        start = time.monotonic()
        page = self._session.page

        self._logger.run_start(goal, {
            p.name: ("[sensitive]" if p.sensitive else (p.example or ""))
            for p in parameters
        })

        # Domain check for entry URL
        domain_err = self._domain_err(entry_url)
        if domain_err:
            return DiscoveryResult(
                run_id=run_id, status="failed", artifact=None, summary=domain_err,
                extracted_data={}, actions=[],
                duration_s=time.monotonic() - start, evidence_dir=evidence_dir,
            )

        try:
            await page.goto(entry_url, wait_until="networkidle", timeout=30_000)
        except Exception as exc:
            return DiscoveryResult(
                run_id=run_id, status="failed", artifact=None,
                summary=f"Failed to navigate to entry URL: {exc}",
                extracted_data={}, actions=[],
                duration_s=time.monotonic() - start, evidence_dir=evidence_dir,
            )

        # Build a temporary SafetySpec for enforcement during discovery
        discovery_safety = SafetySpec(
            permitted_domains=self._permitted_domains if self._permitted_domains else [urlparse(entry_url).netloc],
            permitted_action_types=[
                ActionType.NAVIGATE, ActionType.CLICK, ActionType.TYPE,
                ActionType.SELECT, ActionType.EXTRACT, ActionType.WAIT,
                ActionType.ASSERT, ActionType.DISMISS_DIALOG,
            ],
        )

        history: list[dict] = []
        actions: list[ActionRecord] = []

        for step_num in range(1, self._max_steps + 1):
            observation, screenshot_b64 = await observe(page)
            messages = build_messages(goal, observation, screenshot_b64, history,
                                      parameters=parameters, outputs=outputs)

            try:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    tools=AGENT_TOOLS,
                    tool_choice="required",
                )
            except Exception as exc:
                self._logger.step_error(f"step_{step_num:02d}", f"OpenAI API error: {exc}")
                return DiscoveryResult(
                    run_id=run_id, status="failed", artifact=None,
                    summary=f"OpenAI API error: {exc}",
                    extracted_data={}, actions=actions,
                    duration_s=time.monotonic() - start, evidence_dir=evidence_dir,
                )

            msg = response.choices[0].message
            tool_call = msg.tool_calls[0]
            tool_name = tool_call.function.name
            tool_input = _json.loads(tool_call.function.arguments)

            self._logger.agent_reasoning(step_num, tool_name, tool_input.get("reasoning"))

            if tool_name == "complete":
                extracted = tool_input.get("extracted_data") or {}
                summary = tool_input.get("summary", "")
                missing = [o.name for o in outputs if o.name not in extracted]
                if missing:
                    self._logger.step_error(
                        f"step_{step_num:02d}",
                        f"Model called complete but is missing required outputs: {missing}",
                    )
                    history.append({
                        "call": _tc_dict(tool_call),
                        "result": (
                            f"INCOMPLETE: The following declared outputs are missing from "
                            f"extracted_data: {missing}. Extract them with extract_text "
                            f"(use the exact output_name keys) and call complete again."
                        ),
                    })
                    continue

                self._logger.run_success(extracted)
                artifact = _build_artifact(
                    run_id=run_id, goal=goal, entry_url=entry_url,
                    parameters=parameters, outputs=outputs, actions=actions,
                    summary=summary, final_url=page.url, model=self._model,
                    duration_s=time.monotonic() - start,
                    permitted_domains=self._permitted_domains or [urlparse(entry_url).netloc],
                )
                return DiscoveryResult(
                    run_id=run_id, status="success", artifact=artifact,
                    summary=summary, extracted_data=extracted, actions=actions,
                    duration_s=time.monotonic() - start, evidence_dir=evidence_dir,
                )

            if tool_name == "escalate":
                reason = tool_input.get("reason", "unknown")
                current_state = tool_input.get("current_state", "")
                self._logger.escalated(reason)
                screenshot_path = evidence_dir / f"{run_id}_escalation_{step_num}.png"
                await self._session.screenshot(screenshot_path)
                outcome: EscalationOutcome = await self._escalation.handle(EscalationRequest(
                    run_id=run_id, reason=reason, current_state=current_state,
                    goal=goal, step_num=step_num,
                    cdp_url=self._session.cdp_url, screenshot_path=screenshot_path,
                ))
                if not outcome.resumed:
                    return DiscoveryResult(
                        run_id=run_id, status="escalated", artifact=None,
                        summary=reason, extracted_data={}, actions=actions,
                        duration_s=time.monotonic() - start, evidence_dir=evidence_dir,
                    )
                if outcome.human_action_description:
                    self._logger.human_action(outcome.human_action_description)
                history.append({
                    "call": _tc_dict(tool_call),
                    "result": "Human operator resolved the escalation and returned control.",
                })
                continue

            if tool_name == "navigate":
                nav_url = tool_input.get("url", "")
                err = self._domain_err(nav_url)
                if err:
                    self._logger.step_error(f"step_{step_num:02d}", err)
                    history.append({"call": _tc_dict(tool_call), "result": f"BLOCKED: {err}"})
                    continue

            action_type = _TOOL_TO_ACTION.get(tool_name)
            if action_type:
                check_action = StepAction(
                    type=action_type,
                    risk_level=_infer_risk(tool_name, tool_input),
                )
                try:
                    self._policy.check(discovery_safety, check_action)
                except PolicyViolation as exc:
                    self._logger.step_error(f"step_{step_num:02d}", f"Policy: {exc}")
                    history.append({"call": _tc_dict(tool_call), "result": f"POLICY_BLOCKED: {exc}"})
                    continue

            url_before = page.url
            try:
                result_text, record = await _execute(page, tool_name, tool_input, step_num)
                self._logger.step_success(f"step_{step_num:02d}", record.extracted_value if record else None)
                if record:
                    actions.append(record)
            except Exception as exc:
                err_msg = str(exc)
                self._logger.step_error(f"step_{step_num:02d}", err_msg)
                actions.append(ActionRecord(
                    step_num=step_num, tool_name=tool_name, tool_input=tool_input,
                    locator_spec=None, extracted_value=None,
                    url_before=url_before, url_after=page.url,
                    success=False, error=err_msg,
                ))
                result_text = f"ERROR: {err_msg}"

            history.append({"call": _tc_dict(tool_call), "result": result_text})

        self._logger.run_failure("max_steps_exceeded")
        return DiscoveryResult(
            run_id=run_id, status="max_steps", artifact=None,
            summary=f"Max steps ({self._max_steps}) reached without completing goal",
            extracted_data={}, actions=actions,
            duration_s=time.monotonic() - start, evidence_dir=evidence_dir,
        )

    def _domain_err(self, url: str) -> str | None:
        if not self._permitted_domains:
            return None
        from cua.safety.policy import _hostname
        url_host = _hostname(url)
        allowed = [_hostname(d) for d in self._permitted_domains]
        if url_host not in allowed:
            return (
                f"Domain '{url_host}' is not in the permitted list {allowed}. "
                "Navigation blocked."
            )
        return None


def _tc_dict(tc) -> dict:
    return {
        "id": tc.id, "type": "function",
        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
    }


async def _execute(page: Page, tool_name: str, tool_input: dict, step_num: int):
    url_before = page.url

    if tool_name == "navigate":
        url = tool_input["url"]
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        return f"Navigated to {url}", ActionRecord(
            step_num=step_num, tool_name="navigate", tool_input=tool_input,
            locator_spec=None, extracted_value=None,
            url_before=url_before, url_after=page.url, success=True,
        )

    if tool_name == "wait_for_text":
        text = tool_input["text"]
        await page.wait_for_selector(f"text={text}", timeout=10_000)
        return f"Found text: {text}", ActionRecord(
            step_num=step_num, tool_name="wait_for_text", tool_input=tool_input,
            locator_spec=None, extracted_value=None,
            url_before=url_before, url_after=page.url, success=True,
        )

    locator_spec, pw_loc = await _resolve_element(page, tool_name, tool_input)

    if tool_name == "click":
        await pw_loc.click(timeout=10_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeout:
            pass
        return f"Clicked: {tool_input['description']}", ActionRecord(
            step_num=step_num, tool_name="click", tool_input=tool_input,
            locator_spec=locator_spec, extracted_value=None,
            url_before=url_before, url_after=page.url, success=True,
        )

    if tool_name == "type_text":
        await pw_loc.fill(tool_input["text"], timeout=10_000)
        return f"Typed into: {tool_input['description']}", ActionRecord(
            step_num=step_num, tool_name="type_text", tool_input=tool_input,
            locator_spec=locator_spec, extracted_value=None,
            url_before=url_before, url_after=page.url, success=True,
        )

    if tool_name == "select_option":
        await pw_loc.select_option(label=tool_input["option_text"], timeout=10_000)
        return f"Selected: {tool_input['option_text']}", ActionRecord(
            step_num=step_num, tool_name="select_option", tool_input=tool_input,
            locator_spec=locator_spec, extracted_value=None,
            url_before=url_before, url_after=page.url, success=True,
        )

    if tool_name == "extract_text":
        text = (await pw_loc.text_content(timeout=10_000) or "").strip()
        return f"Extracted: {text}", ActionRecord(
            step_num=step_num, tool_name="extract_text", tool_input=tool_input,
            locator_spec=locator_spec, extracted_value=text,
            url_before=url_before, url_after=page.url, success=True,
            output_name=tool_input.get("output_name"),
        )

    raise ValueError(f"Unknown tool: {tool_name}")


async def _resolve_element(page: Page, tool_name: str, tool_input: dict):
    aria_role = tool_input.get("aria_role", "")
    aria_name = tool_input.get("aria_name", "")
    placeholder = tool_input.get("placeholder_fallback", "")
    text_fb = tool_input.get("text_fallback", "")

    if aria_role and aria_name:
        try:
            loc = page.get_by_role(aria_role, name=aria_name)  # type: ignore[arg-type]
            await loc.first.wait_for(state="visible", timeout=3_000)
            fallbacks = []
            if placeholder:
                fallbacks.append(LocatorStrategy(method=LocatorMethod.PLACEHOLDER, value=placeholder))
            if text_fb:
                fallbacks.append(LocatorStrategy(method=LocatorMethod.TEXT, value=text_fb))
            return LocatorSpec(
                primary=LocatorStrategy(method=LocatorMethod.ARIA_ROLE, value=aria_name, role=aria_role),
                fallbacks=fallbacks,
                rationale=f"ARIA role='{aria_role}' name='{aria_name}' — semantic, survives layout changes",
            ), loc.first
        except Exception:
            pass

    if aria_name:
        try:
            loc = page.get_by_label(aria_name, exact=False)
            await loc.first.wait_for(state="visible", timeout=3_000)
            return LocatorSpec(
                primary=LocatorStrategy(method=LocatorMethod.ARIA_LABEL, value=aria_name),
                fallbacks=[],
                rationale=f"Label text '{aria_name}' — stable if form labels don't change",
            ), loc.first
        except Exception:
            pass

    if placeholder:
        try:
            loc = page.get_by_placeholder(placeholder, exact=False)
            await loc.first.wait_for(state="visible", timeout=3_000)
            fbs = [LocatorStrategy(method=LocatorMethod.TEXT, value=text_fb)] if text_fb else []
            return LocatorSpec(
                primary=LocatorStrategy(method=LocatorMethod.PLACEHOLDER, value=placeholder),
                fallbacks=fbs,
                rationale=f"Placeholder '{placeholder}'",
            ), loc.first
        except Exception:
            pass

    if text_fb:
        try:
            loc = page.get_by_text(text_fb, exact=False)
            await loc.first.wait_for(state="visible", timeout=3_000)
            return LocatorSpec(
                primary=LocatorStrategy(method=LocatorMethod.TEXT, value=text_fb),
                fallbacks=[],
                rationale=f"Visible text '{text_fb}' — works on legacy apps without ARIA",
            ), loc.first
        except Exception:
            pass

    raise RuntimeError(
        f"Could not find element for '{tool_input.get('description', tool_name)}'"
    )


def _build_artifact(
    *, run_id, goal, entry_url, parameters, outputs, actions,
    summary, final_url, model, duration_s, permitted_domains,
) -> CapabilityArtifact:
    output_step_map: dict[str, str] = {}
    steps: list[Step] = []

    for action in actions:
        if not action.success:
            continue
        action_type = _TOOL_TO_ACTION[action.tool_name]
        value = action.tool_input.get("text") or action.tool_input.get("option_text")
        url_val = action.tool_input.get("url")

        if value:
            value = _parameterize(value, parameters)
        if url_val:
            url_val = _parameterize(url_val, parameters)

        step_id = f"step_{action.step_num:02d}_{action.tool_name}"
        if action.output_name:
            output_step_map[action.output_name] = step_id

        # Parameterize locator values too (e.g. ARIA name containing member ID)
        locator = action.locator_spec
        if locator:
            locator = _parameterize_locator(locator, parameters)

        steps.append(Step(
            id=step_id,
            description=(
                action.tool_input.get("description")
                or action.tool_input.get("reasoning")
                or action.tool_name
            ),
            action=StepAction(
                type=action_type,
                locator=locator,
                value=value,
                url=url_val,
                risk_level=_infer_risk(action.tool_name, action.tool_input),
                output_name=action.output_name,
                error_handling=_default_error_handling(action.tool_name),
            ),
        ))

    # Parameterize checkpoint (e.g. /members/12345 → /members/{member_id})
    final_url_param = _parameterize(final_url, parameters)
    url_fragment = urlparse(final_url_param).path
    checkpoint = CheckpointSpec(
        description=f"Goal accomplished: {summary[:80]}",
        type="url_contains" if url_fragment and url_fragment != "/" else "text_present",
        target=url_fragment if url_fragment and url_fragment != "/" else summary[:40],
    )

    # Use the explicitly configured permitted domains (not visited-domain reconstruction)
    from cua.safety.policy import _hostname as _ph
    safe_domains = list({_ph(d) for d in permitted_domains}) if permitted_domains else [
        urlparse(entry_url).netloc
    ]

    permitted_types = list({_TOOL_TO_ACTION[a.tool_name] for a in actions if a.success})

    # Clear sensitive examples before serializing
    clean_params = [
        p.model_copy(update={"example": None}) if p.sensitive else p
        for p in parameters
    ]
    resolved_outputs = [
        o.model_copy(update={"source_step_id": output_step_map.get(o.name, "unknown")})
        for o in outputs
    ]

    return CapabilityArtifact(
        id=str(_uuid.uuid4()),
        name=_slug(goal),
        description=summary,
        target=TargetSpec(
            entry_url=entry_url,
            surface_type=SurfaceType.WEB_LEGACY,
            description="Heritage Credit Union Member Services Portal (mock)",
        ),
        parameters=clean_params,
        outputs=resolved_outputs,
        steps=steps,
        checkpoint=checkpoint,
        safety=SafetySpec(
            permitted_domains=safe_domains,
            permitted_action_types=permitted_types,
            sensitive_param_names=[p.name for p in parameters if p.sensitive],
        ),
        metadata=ArtifactMetadata(
            created_at=datetime.now(timezone.utc),
            discovery_run_id=run_id,
            model_used=model,
            discovery_duration_s=round(duration_s, 2),
        ),
    )


_TOOL_TO_ACTION: dict[str, ActionType] = {
    "navigate": ActionType.NAVIGATE,
    "click": ActionType.CLICK,
    "type_text": ActionType.TYPE,
    "select_option": ActionType.SELECT,
    "wait_for_text": ActionType.WAIT,
    "extract_text": ActionType.EXTRACT,
}


def _infer_risk(tool_name: str, tool_input: dict) -> RiskLevel:
    if tool_name in ("navigate", "extract_text", "wait_for_text"):
        return RiskLevel.SAFE
    if tool_name == "click":
        desc = (tool_input.get("description") or "").lower()
        name = (tool_input.get("aria_name") or "").lower()
        if any(w in desc + name for w in ("confirm", "submit", "open account", "create", "delete")):
            return RiskLevel.HIGH
        return RiskLevel.SAFE
    if tool_name in ("type_text", "select_option"):
        return RiskLevel.MODERATE
    return RiskLevel.SAFE


def _default_error_handling(tool_name: str) -> ErrorHandlingSpec:
    if tool_name in ("click", "navigate"):
        return ErrorHandlingSpec(
            expected_outcomes=[
                ExpectedOutcome(
                    code="member_not_found",
                    description="The requested member was not found",
                    detection_pattern="No members found",
                ),
                ExpectedOutcome(
                    code="record_not_found",
                    description="Requested record does not exist",
                    detection_pattern="not found",
                ),
            ],
            recoverable_patterns=["Please wait", "Loading"],
            fail_patterns=["500 Internal Server Error", "Application Error"],
        )
    return ErrorHandlingSpec()


def _parameterize(text: str, params: list[ParameterSpec]) -> str:
    """Replace known parameter runtime values with {param_name} placeholders."""
    for p in params:
        if p.example and p.example in text:
            text = text.replace(p.example, f"{{{p.name}}}")
    return text


def _parameterize_locator(locator: LocatorSpec, params: list[ParameterSpec]) -> LocatorSpec:
    """Parameterize locator values that contain literal parameter examples."""
    def _ps(s: LocatorStrategy) -> LocatorStrategy:
        new_val = _parameterize(s.value, params)
        return s.model_copy(update={"value": new_val}) if new_val != s.value else s

    new_primary = _ps(locator.primary)
    new_fallbacks = [_ps(f) for f in locator.fallbacks]
    changed = new_primary != locator.primary or any(
        nf != f for nf, f in zip(new_fallbacks, locator.fallbacks)
    )
    return locator.model_copy(update={"primary": new_primary, "fallbacks": new_fallbacks}) if changed else locator


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60]
