# LLM-driven discovery loop (gpt-5.6-luna).
# Key behaviors:
#   - Sensitive param values are substituted at type-time, never logged.
#   - After every navigation (initial, explicit, post-click), the resulting URL
#     is checked against the domain allowlist.
#   - Recorded extract_text values are the authoritative outputs, not the model's.
#   - Human handoff domain-checks the resumed session before continuing.

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
from cua.safety.policy import PolicyEnforcer, PolicyViolation, _hostname

DEFAULT_MODEL = "gpt-5.6-luna"


class _PolicyViolation(Exception):
    def __init__(self, message: str):
        self.message = message


@dataclass
class ActionRecord:
    step_num: int
    tool_name: str
    tool_input: dict  # stores templates ({param_name}), not resolved values
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
    status: str
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

        domain_err = self._domain_err(entry_url)
        if domain_err:
            return DiscoveryResult(
                run_id=run_id, status="policy_violation", artifact=None, summary=domain_err,
                extracted_data={}, actions=[],
                duration_s=time.monotonic() - start, evidence_dir=evidence_dir,
            )

        try:
            await page.goto(entry_url, wait_until="networkidle", timeout=30_000)
        except Exception as exc:
            return DiscoveryResult(
                run_id=run_id, status="failed", artifact=None,
                summary=f"Failed to load entry URL: {exc}",
                extracted_data={}, actions=[],
                duration_s=time.monotonic() - start, evidence_dir=evidence_dir,
            )

        # Check redirect destination — entry URL may redirect outside the allowlist
        if self._permitted_domains:
            err = self._domain_err(page.url)
            if err:
                return DiscoveryResult(
                    run_id=run_id, status="policy_violation", artifact=None,
                    summary=f"Initial navigation redirected to forbidden domain: {err}",
                    extracted_data={}, actions=[],
                    duration_s=time.monotonic() - start, evidence_dir=evidence_dir,
                )

        discovery_safety = SafetySpec(
            permitted_domains=self._permitted_domains if self._permitted_domains else [urlparse(entry_url).netloc],
            permitted_action_types=[
                ActionType.NAVIGATE, ActionType.CLICK, ActionType.TYPE,
                ActionType.SELECT, ActionType.EXTRACT, ActionType.WAIT,
                ActionType.ASSERT, ActionType.DISMISS_DIALOG,
            ],
        )

        # Runtime param map for local substitution at execution time.
        # Sensitive values stay here only; they never reach logs or the artifact.
        runtime_params = {p.name: p.example for p in parameters if p.example}

        # Install a continuous domain guard — aborts forbidden document navigations
        # at the browser level rather than only checking post-facto.
        if self._permitted_domains:
            await self._session.install_domain_guard(self._domain_err)

        history: list[dict] = []
        actions: list[ActionRecord] = []

        for step_num in range(1, self._max_steps + 1):
            try:
                observation, screenshot_b64 = await observe(page)
            except Exception as exc:
                self._logger.step_error(f"step_{step_num:02d}", f"Observation failed: {exc}")
                return DiscoveryResult(
                    run_id=run_id, status="failed", artifact=None,
                    summary=f"Page observation failed at step {step_num}: {exc}",
                    extracted_data={}, actions=actions,
                    duration_s=time.monotonic() - start, evidence_dir=evidence_dir,
                )

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
                    summary=f"OpenAI API error at step {step_num}: {exc}",
                    extracted_data={}, actions=actions,
                    duration_s=time.monotonic() - start, evidence_dir=evidence_dir,
                )

            msg = response.choices[0].message
            if not msg.tool_calls:
                self._logger.step_error(f"step_{step_num:02d}", "Model returned no tool calls")
                return DiscoveryResult(
                    run_id=run_id, status="failed", artifact=None,
                    summary=f"Model returned no tool call at step {step_num}",
                    extracted_data={}, actions=actions,
                    duration_s=time.monotonic() - start, evidence_dir=evidence_dir,
                )

            tool_call = msg.tool_calls[0]
            try:
                tool_input = _json.loads(tool_call.function.arguments)
            except _json.JSONDecodeError as exc:
                self._logger.step_error(f"step_{step_num:02d}", f"Malformed tool arguments: {exc}")
                return DiscoveryResult(
                    run_id=run_id, status="failed", artifact=None,
                    summary=f"Malformed tool arguments at step {step_num}: {exc}",
                    extracted_data={}, actions=actions,
                    duration_s=time.monotonic() - start, evidence_dir=evidence_dir,
                )

            tool_name = tool_call.function.name
            self._logger.agent_reasoning(step_num, tool_name, tool_input.get("reasoning"))

            if tool_name == "complete":
                extracted_by_model = tool_input.get("extracted_data") or {}
                summary = tool_input.get("summary", "")

                missing_keys = [o.name for o in outputs if o.name not in extracted_by_model]
                if missing_keys:
                    history.append({"call": _tc_dict(tool_call),
                                    "result": f"INCOMPLETE: missing output keys {missing_keys}. Use extract_text with the exact output_name."})
                    continue

                recorded = {
                    a.output_name: a.extracted_value
                    for a in actions
                    if a.tool_name == "extract_text" and a.output_name and a.success
                }
                missing_recorded = [o.name for o in outputs if o.name not in recorded]
                if missing_recorded:
                    history.append({"call": _tc_dict(tool_call),
                                    "result": f"INCOMPLETE: these were not extracted via extract_text: {missing_recorded}."})
                    continue

                # Type-validate all recorded values
                type_errors = []
                for o in outputs:
                    val = recorded.get(o.name, "")
                    if o.type == "decimal":
                        try:
                            float(re.sub(r"[$,\s]", "", val or ""))
                        except (ValueError, TypeError):
                            type_errors.append(f"'{o.name}' value '{val}' is not parseable as decimal.")
                    elif o.type == "integer":
                        try:
                            int(str(val).strip())
                        except (ValueError, TypeError):
                            type_errors.append(f"'{o.name}' value '{val}' is not parseable as integer.")
                    elif o.type == "boolean":
                        if str(val).lower() not in ("true", "false", "1", "0", "yes", "no"):
                            type_errors.append(f"'{o.name}' value '{val}' is not a valid boolean.")

                if type_errors:
                    history.append({"call": _tc_dict(tool_call),
                                    "result": f"INCOMPLETE: type errors: {type_errors}. Re-extract and call complete again."})
                    continue

                # Verify the intended record is reflected in the current URL.
                # For the member-lookup flow: non-sensitive param values should appear
                # in the URL (e.g. /members/12345). This catches "wrong member" scenarios
                # where text targeting picked the first of multiple results.
                current_url = page.url
                goal_state_issues = []
                for p in parameters:
                    if not p.sensitive and p.example and p.example not in current_url:
                        goal_state_issues.append(
                            f"Parameter '{p.name}' value '{p.example}' not found in current URL '{current_url}'"
                        )
                if goal_state_issues:
                    history.append({"call": _tc_dict(tool_call),
                                    "result": (
                                        f"INCOMPLETE: goal state cannot be verified — "
                                        f"{'; '.join(goal_state_issues)}. "
                                        f"Ensure you navigated to the correct record."
                                    )})
                    continue

                # Build and sanitize the artifact before declaring success
                authoritative_outputs = {o.name: recorded[o.name] for o in outputs}
                artifact = _build_artifact(
                    run_id=run_id, goal=goal, entry_url=entry_url,
                    parameters=parameters, outputs=outputs, actions=actions,
                    summary=summary, final_url=page.url, model=self._model,
                    duration_s=time.monotonic() - start,
                    permitted_domains=self._permitted_domains or [urlparse(entry_url).netloc],
                    sensitive_values=[p.example for p in parameters if p.sensitive and p.example],
                )
                self._logger.run_success(authoritative_outputs)
                return DiscoveryResult(
                    run_id=run_id, status="success", artifact=artifact,
                    summary=summary, extracted_data=authoritative_outputs, actions=actions,
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

                # Always record the handoff event (caller's responsibility, not handler's)
                self._logger.human_action(outcome.human_action_description or "(no description provided)")

                if not outcome.resumed:
                    return DiscoveryResult(
                        run_id=run_id, status="escalated", artifact=None,
                        summary=reason, extracted_data={}, actions=actions,
                        duration_s=time.monotonic() - start, evidence_dir=evidence_dir,
                    )

                # Domain check before resuming — human may have navigated elsewhere
                if self._permitted_domains:
                    err = self._domain_err(page.url)
                    if err:
                        return DiscoveryResult(
                            run_id=run_id, status="policy_violation", artifact=None,
                            summary=f"Handoff left browser on forbidden domain: {err}",
                            extracted_data={}, actions=actions,
                            duration_s=time.monotonic() - start, evidence_dir=evidence_dir,
                        )

                history.append({"call": _tc_dict(tool_call),
                                "result": "Human operator resolved the escalation and returned control."})
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
                check_action = StepAction(type=action_type, risk_level=_infer_risk(tool_name, tool_input))
                try:
                    self._policy.check(discovery_safety, check_action)
                except PolicyViolation as exc:
                    self._logger.step_error(f"step_{step_num:02d}", f"Policy: {exc}")
                    history.append({"call": _tc_dict(tool_call), "result": f"POLICY_BLOCKED: {exc}"})
                    continue

            url_before = page.url
            try:
                result_text, record = await _execute(
                    page, tool_name, tool_input, step_num,
                    domain_check=self._domain_err,
                    runtime_params=runtime_params,
                )
                self._logger.step_success(f"step_{step_num:02d}", record.extracted_value if record else None)
                if record:
                    actions.append(record)
            except _PolicyViolation as exc:
                self._logger.escalated(exc.message)
                return DiscoveryResult(
                    run_id=run_id, status="policy_violation", artifact=None,
                    summary=exc.message, extracted_data={}, actions=actions,
                    duration_s=time.monotonic() - start, evidence_dir=evidence_dir,
                )
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
        url_host = _hostname(url)
        allowed = [_hostname(d) for d in self._permitted_domains]
        if url_host not in allowed:
            return f"Domain '{url_host}' not in permitted list {allowed}."
        return None


def _tc_dict(tc) -> dict:
    return {
        "id": tc.id, "type": "function",
        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
    }


async def _execute(
    page: Page, tool_name: str, tool_input: dict, step_num: int,
    domain_check=None,
    runtime_params: dict[str, str] | None = None,
) -> tuple[str, ActionRecord | None]:
    url_before = page.url

    if tool_name == "navigate":
        url = tool_input["url"]
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        # Check redirect destination — explicit navigation can redirect outside the allowlist
        if domain_check:
            err = domain_check(page.url)
            if err:
                raise _PolicyViolation(err)
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
        if domain_check:
            err = domain_check(page.url)
            if err:
                raise _PolicyViolation(err)
        return f"Clicked: {tool_input['description']}", ActionRecord(
            step_num=step_num, tool_name="click", tool_input=tool_input,
            locator_spec=locator_spec, extracted_value=None,
            url_before=url_before, url_after=page.url, success=True,
        )

    if tool_name == "type_text":
        # Resolve {param_name} references locally — sensitive values are never logged
        text_template = tool_input["text"]
        text_to_type = text_template
        if runtime_params:
            for key, val in runtime_params.items():
                text_to_type = text_to_type.replace(f"{{{key}}}", val)
        await pw_loc.fill(text_to_type, timeout=10_000)
        # ActionRecord stores the template, not the resolved value — keeps secrets out of logs
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
    """
    Resolve an element using the same uniqueness rules as the replay locator:
    XPath/CSS/ARIA strategies reject multiple matches to keep discovery and replay consistent.
    """
    from cua.replay.locator import AmbiguousLocatorError

    aria_role = tool_input.get("aria_role", "")
    aria_name = tool_input.get("aria_name", "")
    placeholder = tool_input.get("placeholder_fallback", "")
    text_fb = tool_input.get("text_fallback", "")
    xpath = tool_input.get("xpath_selector", "")
    css = tool_input.get("css_selector", "")

    if xpath:
        try:
            loc = page.locator(f"xpath={xpath}")
            await loc.first.wait_for(state="visible", timeout=5_000)
            count = await loc.count()
            if count > 1:
                raise RuntimeError(f"XPath matched {count} elements — refine to be unique")
            fallbacks = [LocatorStrategy(method=LocatorMethod.CSS, value=css)] if css else []
            return LocatorSpec(
                primary=LocatorStrategy(method=LocatorMethod.XPATH, value=xpath),
                fallbacks=fallbacks,
                rationale=f"XPath for scoped extraction: {xpath}",
            ), loc.first
        except RuntimeError:
            raise
        except Exception:
            pass

    if css:
        try:
            loc = page.locator(css)
            await loc.first.wait_for(state="visible", timeout=5_000)
            count = await loc.count()
            if count > 1:
                raise RuntimeError(f"CSS matched {count} elements — refine to be unique")
            return LocatorSpec(
                primary=LocatorStrategy(method=LocatorMethod.CSS, value=css),
                fallbacks=[],
                rationale=f"CSS for scoped extraction: {css}",
            ), loc.first
        except RuntimeError:
            raise
        except Exception:
            pass

    if aria_role and aria_name:
        try:
            loc = page.get_by_role(aria_role, name=aria_name)  # type: ignore[arg-type]
            await loc.first.wait_for(state="visible", timeout=3_000)
            count = await loc.count()
            if count > 1:
                raise RuntimeError(f"ARIA role='{aria_role}' name='{aria_name}' matched {count} elements")
            fallbacks = []
            if placeholder:
                fallbacks.append(LocatorStrategy(method=LocatorMethod.PLACEHOLDER, value=placeholder))
            if text_fb:
                fallbacks.append(LocatorStrategy(method=LocatorMethod.TEXT, value=text_fb))
            return LocatorSpec(
                primary=LocatorStrategy(method=LocatorMethod.ARIA_ROLE, value=aria_name, role=aria_role),
                fallbacks=fallbacks,
                rationale=f"ARIA role='{aria_role}' name='{aria_name}'",
            ), loc.first
        except RuntimeError:
            raise
        except Exception:
            pass

    if aria_name:
        try:
            loc = page.get_by_label(aria_name, exact=False)
            await loc.first.wait_for(state="visible", timeout=3_000)
            count = await loc.count()
            if count > 1:
                raise RuntimeError(f"Label '{aria_name}' matched {count} elements")
            return LocatorSpec(
                primary=LocatorStrategy(method=LocatorMethod.ARIA_LABEL, value=aria_name),
                fallbacks=[],
                rationale=f"Label text '{aria_name}'",
            ), loc.first
        except RuntimeError:
            raise
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
            count = await loc.count()
            rationale = f"Visible text '{text_fb}'"
            if count > 1:
                # Multiple matches — first is used; step checkpoint must verify the right record
                rationale = f"Visible text '{text_fb}' (matched {count} elements — step checkpoint verifies correct selection)"
            return LocatorSpec(
                primary=LocatorStrategy(method=LocatorMethod.TEXT, value=text_fb),
                fallbacks=[],
                rationale=rationale,
            ), loc.first
        except Exception:
            pass

    raise RuntimeError(f"Could not find element for '{tool_input.get('description', tool_name)}'")


def _build_artifact(
    *, run_id, goal, entry_url, parameters, outputs, actions,
    summary, final_url, model, duration_s, permitted_domains,
    sensitive_values: list[str] | None = None,
) -> CapabilityArtifact:
    sensitive_values = sensitive_values or []

    def sanitize(text: str) -> str:
        """Remove sensitive runtime values from free text before persisting."""
        for val in sensitive_values:
            text = text.replace(val, "[REDACTED]")
        return text

    output_step_map: dict[str, str] = {}
    steps: list[Step] = []

    for action in actions:
        if not action.success:
            continue
        action_type = _TOOL_TO_ACTION[action.tool_name]
        value = action.tool_input.get("text") or action.tool_input.get("option_text")
        url_val = action.tool_input.get("url")

        # _parameterize substitutes literal discovered values → {placeholders}
        # For values already containing placeholders (sensitive params), no change needed
        if value:
            value = _parameterize(value, parameters)
        if url_val:
            url_val = _parameterize(url_val, parameters)

        step_id = f"step_{action.step_num:02d}_{action.tool_name}"
        if action.output_name:
            output_step_map[action.output_name] = step_id

        locator = action.locator_spec
        if locator:
            locator = _parameterize_locator(locator, parameters)
            # Sanitize rationale — it may contain the aria-name or text that includes a param value
            locator = locator.model_copy(update={"rationale": sanitize(locator.rationale)})

        # Sanitize free text fields that might contain sensitive values
        description = sanitize(
            action.tool_input.get("description")
            or action.tool_input.get("reasoning")
            or action.tool_name
        )

        steps.append(Step(
            id=step_id,
            description=description,
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

    final_url_param = _parameterize(final_url, parameters)
    url_fragment = urlparse(final_url_param).path
    checkpoint = CheckpointSpec(
        description=sanitize(f"Goal accomplished: {summary[:80]}"),
        type="url_contains" if url_fragment and url_fragment != "/" else "text_present",
        target=url_fragment if url_fragment and url_fragment != "/" else sanitize(summary[:40]),
    )

    safe_domains = list({_hostname(d) for d in permitted_domains}) if permitted_domains else [
        urlparse(entry_url).netloc
    ]
    permitted_types = list({_TOOL_TO_ACTION[a.tool_name] for a in actions if a.success})

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
        name=_slug(sanitize(goal)),
        description=sanitize(summary),
        target=TargetSpec(
            entry_url=sanitize(entry_url),
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
                ExpectedOutcome(code="member_not_found", description="Member not found",
                                detection_pattern="No members found"),
                ExpectedOutcome(code="record_not_found", description="Record does not exist",
                                detection_pattern="not found"),
            ],
            recoverable_patterns=["Please wait", "Loading"],
            fail_patterns=["500 Internal Server Error", "Application Error"],
        )
    return ErrorHandlingSpec()


def _parameterize(text: str, params: list[ParameterSpec]) -> str:
    for p in params:
        if p.example and p.example in text:
            text = text.replace(p.example, f"{{{p.name}}}")
    return text


def _parameterize_locator(locator: LocatorSpec, params: list[ParameterSpec]) -> LocatorSpec:
    def sub(s: LocatorStrategy) -> LocatorStrategy:
        new_val = _parameterize(s.value, params)
        return s.model_copy(update={"value": new_val}) if new_val != s.value else s
    new_primary = sub(locator.primary)
    new_fallbacks = [sub(f) for f in locator.fallbacks]
    return locator.model_copy(update={"primary": new_primary, "fallbacks": new_fallbacks})


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60]
