# Replays a CapabilityArtifact step-by-step without calling the LLM.
# Recovery waits and re-scans (never re-executes the action).
# All exception paths return a typed ReplayResult — nothing unhandled bubbles up.

from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from cua.artifact.schema import (
    ActionType,
    CapabilityArtifact,
    CheckpointSpec,
    ErrorHandlingSpec,
    OutputSpec,
    ParameterSpec,
    Step,
)
from cua.browser.session import BrowserSession
from cua.escalation.handler import EscalationHandler, EscalationRequest
from cua.observability.logger import RunLogger
from cua.replay.locator import AmbiguousLocatorError, ElementNotFoundError, resolve_locator
from cua.replay.result import ErrorDetail, ReplayResult, ReplayStatus
from cua.safety.policy import PolicyEnforcer, PolicyViolation, _hostname

_RECOVERY_WAIT_S = 3
_RECOVERY_MAX_RETRIES = 2


class _BusinessOutcome(Exception):
    def __init__(self, code: str, description: str):
        self.code = code
        self.description = description


class _Recoverable(Exception):
    def __init__(self, pattern: str):
        self.pattern = pattern


class _HardFailure(Exception):
    def __init__(self, expected: str, observed: str):
        self.expected = expected
        self.observed = observed


class _CheckpointFailed(Exception):
    def __init__(self, expected: str, observed: str):
        self.expected = expected
        self.observed = observed


class ReplayExecutor:
    def __init__(
        self,
        session: BrowserSession,
        policy: PolicyEnforcer,
        logger: RunLogger,
        evidence_dir: Path,
        escalation_handler: EscalationHandler | None = None,
    ):
        self._session = session
        self._policy = policy
        self._logger = logger
        self._evidence_dir = evidence_dir
        self._escalation = escalation_handler

    async def replay(
        self,
        artifact: CapabilityArtifact,
        params: dict[str, str],
        run_id: str | None = None,
    ) -> ReplayResult:
        run_id = run_id or str(uuid.uuid4())[:8]
        start = time.monotonic()
        page = self._session.page
        recovered: list[str] = []

        # ── Pre-flight validation ────────────────────────────────────────────
        if artifact.schema_version != "1.0":
            return ReplayResult(
                run_id=run_id, artifact_id=artifact.id, artifact_name=artifact.name,
                status=ReplayStatus.HARD_FAILURE,
                steps_completed=0, total_steps=len(artifact.steps), duration_s=0.0,
                error=ErrorDetail(
                    step_id="schema_check", step_description="Check schema version",
                    expected="schema_version == '1.0'",
                    observed=f"schema_version == '{artifact.schema_version}'",
                    timestamp=datetime.now(timezone.utc),
                ),
            )

        validation_error = _validate_inputs(artifact, params)
        if validation_error:
            return ReplayResult(
                run_id=run_id, artifact_id=artifact.id, artifact_name=artifact.name,
                status=ReplayStatus.HARD_FAILURE,
                steps_completed=0, total_steps=len(artifact.steps), duration_s=0.0,
                error=ErrorDetail(
                    step_id="input_validation", step_description="Validate input parameters",
                    expected="all required parameters present and correctly typed",
                    observed=validation_error, timestamp=datetime.now(timezone.utc),
                ),
            )

        self._logger.run_start(artifact.description, params)

        entry_url = _substitute(artifact.target.entry_url, params)
        try:
            self._policy.check_url(artifact.safety, entry_url)
        except PolicyViolation as exc:
            return self._pv(run_id, artifact, "entry_navigation", "Navigate to entry URL", str(exc), start)

        try:
            await page.goto(entry_url, wait_until="networkidle", timeout=30_000)
        except Exception as exc:
            return self._fail(run_id, artifact, "navigate_entry", 0,
                              len(artifact.steps), start, "navigation to entry URL", str(exc))

        # Check we didn't immediately redirect outside the allowlist
        try:
            self._policy.check_url(artifact.safety, page.url)
        except PolicyViolation as exc:
            return self._pv(run_id, artifact, "post_goto_domain", "Post-navigation domain check", str(exc), start)

        outputs: dict[str, str] = {}
        steps_completed = 0

        for i, step in enumerate(artifact.steps):
            self._logger.step_start(step.id, step.description, step.action.type.value)

            try:
                self._policy.check(artifact.safety, step.action)
            except PolicyViolation as exc:
                screenshot = await self._screenshot(run_id, f"policy_step{i}")
                return self._pv(run_id, artifact, step.id, step.description, str(exc), start,
                                steps_completed, screenshot)

            if step.action.type == ActionType.NAVIGATE:
                nav_url = _substitute(step.action.url or "", params)
                try:
                    self._policy.check_url(artifact.safety, nav_url)
                except PolicyViolation as exc:
                    screenshot = await self._screenshot(run_id, f"domain_step{i}")
                    return self._pv(run_id, artifact, step.id, step.description, str(exc), start,
                                    steps_completed, screenshot)

            resolved_value = _substitute(step.action.value, params) if step.action.value else None
            resolved_url = _substitute(step.action.url, params) if step.action.url else None

            exec_result = await self._execute_with_recovery(
                page, step, resolved_value, resolved_url, artifact, params,
                run_id, i, recovered, steps_completed, start,
            )

            if isinstance(exec_result, ReplayResult):
                if exec_result.status == ReplayStatus.HARD_FAILURE and self._escalation:
                    handoff = await self._try_escalation(
                        exec_result, page, step, params,
                        run_id, i, artifact, start, recovered, steps_completed,
                    )
                    if handoff == "continue":
                        steps_completed += 1
                        continue
                    return handoff  # type: ignore[return-value]
                return exec_result

            # exec_result is str | None (extracted value) or "recovered"
            is_recovered = exec_result == "recovered"
            extracted: str | None = None if is_recovered else exec_result  # type: ignore

            if not is_recovered and step.action.output_name and extracted is not None:
                outputs[step.action.output_name] = extracted
                self._logger.step_success(step.id, extracted)
            else:
                self._logger.step_success(step.id)

            # Verify checkpoint — including after recovery (proves the action succeeded)
            if step.checkpoint:
                resolved_cp = _substitute_checkpoint(step.checkpoint, params)
                try:
                    await _verify_checkpoint(page, resolved_cp)
                except _CheckpointFailed as exc:
                    screenshot = await self._screenshot(run_id, f"checkpoint_step{i}")
                    self._logger.step_failure(step.id, exc.expected, exc.observed)
                    self._logger.run_failure("hard_failure")
                    return ReplayResult(
                        run_id=run_id, artifact_id=artifact.id, artifact_name=artifact.name,
                        status=ReplayStatus.HARD_FAILURE, recovered_conditions=recovered,
                        steps_completed=steps_completed, total_steps=len(artifact.steps),
                        duration_s=time.monotonic() - start,
                        error=ErrorDetail(
                            step_id=step.id,
                            step_description=f"{'Post-recovery c' if is_recovered else 'C'}heckpoint: {step.checkpoint.description}",
                            expected=exc.expected, observed=exc.observed,
                            screenshot_path=screenshot, timestamp=datetime.now(timezone.utc),
                        ),
                    )

            steps_completed += 1

        try:
            await _verify_checkpoint(page, _substitute_checkpoint(artifact.checkpoint, params))
        except _CheckpointFailed as exc:
            screenshot = await self._screenshot(run_id, "final_checkpoint_fail")
            self._logger.step_failure("final_checkpoint", exc.expected, exc.observed)
            self._logger.run_failure("hard_failure")
            return ReplayResult(
                run_id=run_id, artifact_id=artifact.id, artifact_name=artifact.name,
                status=ReplayStatus.HARD_FAILURE, recovered_conditions=recovered,
                steps_completed=steps_completed, total_steps=len(artifact.steps),
                duration_s=time.monotonic() - start,
                error=ErrorDetail(
                    step_id="final_checkpoint",
                    step_description=f"Final: {artifact.checkpoint.description}",
                    expected=exc.expected, observed=exc.observed,
                    screenshot_path=screenshot, timestamp=datetime.now(timezone.utc),
                ),
            )

        # ── Output validation ────────────────────────────────────────────────
        output_error = _validate_outputs(artifact.outputs, outputs)
        if output_error:
            self._logger.run_failure("output_validation_failed")
            return ReplayResult(
                run_id=run_id, artifact_id=artifact.id, artifact_name=artifact.name,
                status=ReplayStatus.HARD_FAILURE, recovered_conditions=recovered,
                steps_completed=steps_completed, total_steps=len(artifact.steps),
                duration_s=time.monotonic() - start,
                error=ErrorDetail(
                    step_id="output_validation",
                    step_description="Validate collected outputs match declared contract",
                    expected="all declared outputs collected with correct types",
                    observed=output_error, timestamp=datetime.now(timezone.utc),
                ),
            )

        await self._screenshot(run_id, "success_final")
        self._logger.run_success(outputs)

        return ReplayResult(
            run_id=run_id, artifact_id=artifact.id, artifact_name=artifact.name,
            status=ReplayStatus.SUCCESS, outputs=outputs, recovered_conditions=recovered,
            steps_completed=steps_completed, total_steps=len(artifact.steps),
            duration_s=time.monotonic() - start, evidence_path=str(self._evidence_dir),
        )

    async def _execute_with_recovery(
        self, page, step, value, url, artifact, params,
        run_id, step_idx, recovered, steps_completed, start,
    ) -> ReplayResult | Literal["recovered"] | str | None:
        # On recoverable conditions: wait, re-scan, never re-execute the action.
        # Returns "recovered" when the condition clears so the caller can verify
        # the checkpoint. All exceptions become typed ReplayResult — nothing bubbles up.
        try:
            return await self._run_step(page, step, value, url, artifact.safety)
        except _BusinessOutcome as exc:
            screenshot = await self._screenshot(run_id, f"business_outcome_{step_idx}")
            self._logger.business_outcome(exc.code, exc.description)
            return ReplayResult(
                run_id=run_id, artifact_id=artifact.id, artifact_name=artifact.name,
                status=ReplayStatus.BUSINESS_OUTCOME,
                business_outcome=exc.code, business_outcome_description=exc.description,
                recovered_conditions=recovered,
                steps_completed=steps_completed, total_steps=len(artifact.steps),
                duration_s=time.monotonic() - start, evidence_path=str(self._evidence_dir),
            )
        except _Recoverable as exc:
            self._logger.recovered(f"{exc.pattern} — waiting {_RECOVERY_WAIT_S}s")
            recovered.append(exc.pattern)

            for attempt in range(_RECOVERY_MAX_RETRIES):
                await asyncio.sleep(_RECOVERY_WAIT_S)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except PlaywrightTimeout:
                    pass

                # Re-scan only — never re-execute the action
                try:
                    await self._scan_page_errors(page, step.action.error_handling)
                    # No exception: condition cleared
                    self._logger.recovered(f"{exc.pattern} cleared after {attempt + 1} retry")
                    return "recovered"
                except _Recoverable:
                    continue   # Still loading — try again
                except _BusinessOutcome as bout:
                    screenshot = await self._screenshot(run_id, f"recovery_outcome_{step_idx}")
                    self._logger.business_outcome(bout.code, bout.description)
                    return ReplayResult(
                        run_id=run_id, artifact_id=artifact.id, artifact_name=artifact.name,
                        status=ReplayStatus.BUSINESS_OUTCOME,
                        business_outcome=bout.code, business_outcome_description=bout.description,
                        recovered_conditions=recovered,
                        steps_completed=steps_completed, total_steps=len(artifact.steps),
                        duration_s=time.monotonic() - start, evidence_path=str(self._evidence_dir),
                    )
                except _HardFailure as hf:
                    screenshot = await self._screenshot(run_id, f"recovery_failure_{step_idx}")
                    self._logger.step_failure(step.id, hf.expected, hf.observed)
                    self._logger.run_failure("hard_failure")
                    return ReplayResult(
                        run_id=run_id, artifact_id=artifact.id, artifact_name=artifact.name,
                        status=ReplayStatus.HARD_FAILURE, recovered_conditions=recovered,
                        steps_completed=steps_completed, total_steps=len(artifact.steps),
                        duration_s=time.monotonic() - start,
                        error=ErrorDetail(
                            step_id=step.id,
                            step_description=f"Recovery rescan revealed failure: {hf.expected}",
                            expected=hf.expected, observed=hf.observed,
                            screenshot_path=screenshot, timestamp=datetime.now(timezone.utc),
                        ),
                    )
                except Exception as ex:
                    screenshot = await self._screenshot(run_id, f"recovery_error_{step_idx}")
                    self._logger.step_failure(step.id, "recovery scan to complete", str(ex))
                    self._logger.run_failure("hard_failure")
                    return ReplayResult(
                        run_id=run_id, artifact_id=artifact.id, artifact_name=artifact.name,
                        status=ReplayStatus.HARD_FAILURE, recovered_conditions=recovered,
                        steps_completed=steps_completed, total_steps=len(artifact.steps),
                        duration_s=time.monotonic() - start,
                        error=ErrorDetail(
                            step_id=step.id, step_description="Recovery scan error",
                            expected="clean page state", observed=str(ex),
                            screenshot_path=screenshot, timestamp=datetime.now(timezone.utc),
                        ),
                    )

            # Retries exhausted
            screenshot = await self._screenshot(run_id, f"recovery_exhausted_{step_idx}")
            self._logger.step_failure(step.id,
                                      "recoverable condition to clear",
                                      f"'{exc.pattern}' persisted after {_RECOVERY_MAX_RETRIES} retries")
            self._logger.run_failure("hard_failure")
            return ReplayResult(
                run_id=run_id, artifact_id=artifact.id, artifact_name=artifact.name,
                status=ReplayStatus.HARD_FAILURE, recovered_conditions=recovered,
                steps_completed=steps_completed, total_steps=len(artifact.steps),
                duration_s=time.monotonic() - start,
                error=ErrorDetail(
                    step_id=step.id,
                    step_description=f"Recovery exhausted: '{exc.pattern}'",
                    expected="condition to clear after retry",
                    observed=f"condition persisted after {_RECOVERY_MAX_RETRIES} retries",
                    screenshot_path=screenshot, timestamp=datetime.now(timezone.utc),
                ),
            )
        except (ElementNotFoundError, AmbiguousLocatorError, PlaywrightTimeout, _HardFailure) as exc:
            screenshot = await self._screenshot(run_id, f"failure_{step_idx}")
            expected = exc.expected if isinstance(exc, _HardFailure) else "element found and actionable"
            observed = exc.observed if isinstance(exc, _HardFailure) else str(exc)
            self._logger.step_failure(step.id, expected, observed)
            self._logger.run_failure("hard_failure")
            return ReplayResult(
                run_id=run_id, artifact_id=artifact.id, artifact_name=artifact.name,
                status=ReplayStatus.HARD_FAILURE, recovered_conditions=recovered,
                steps_completed=steps_completed, total_steps=len(artifact.steps),
                duration_s=time.monotonic() - start,
                error=ErrorDetail(
                    step_id=step.id, step_description=step.description,
                    expected=expected, observed=observed,
                    screenshot_path=screenshot, timestamp=datetime.now(timezone.utc),
                ),
            )
        except Exception as exc:
            screenshot = await self._screenshot(run_id, f"unexpected_{step_idx}")
            self._logger.step_failure(step.id, "step to complete", str(exc))
            self._logger.run_failure("hard_failure")
            return ReplayResult(
                run_id=run_id, artifact_id=artifact.id, artifact_name=artifact.name,
                status=ReplayStatus.HARD_FAILURE, recovered_conditions=recovered,
                steps_completed=steps_completed, total_steps=len(artifact.steps),
                duration_s=time.monotonic() - start,
                error=ErrorDetail(
                    step_id=step.id, step_description=step.description,
                    expected="step to complete without error", observed=str(exc),
                    screenshot_path=screenshot, timestamp=datetime.now(timezone.utc),
                ),
            )

    async def _run_step(self, page: Page, step: Step, value: str | None, url: str | None, safety) -> str | None:
        action = step.action
        eh = action.error_handling

        if action.type == ActionType.NAVIGATE:
            await page.goto(url or action.url or "", wait_until="networkidle", timeout=action.timeout_ms)
            await self._scan_page_errors(page, eh)
            return None

        if action.type == ActionType.WAIT:
            if value:
                await page.wait_for_selector(f"text={value}", timeout=action.timeout_ms)
            else:
                await page.wait_for_load_state("networkidle", timeout=action.timeout_ms)
            return None

        if action.type == ActionType.DISMISS_DIALOG:
            page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))
            return None

        if action.locator is None:
            raise _HardFailure(
                expected="locator spec in step action",
                observed=f"step {step.id} type={action.type.value} has no locator",
            )
        loc = await resolve_locator(page, action.locator)

        if action.type in (ActionType.CLICK, ActionType.SUBMIT):
            await loc.click(timeout=action.timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=action.timeout_ms)
            except PlaywrightTimeout:
                pass
            # Domain check after click-triggered navigation
            try:
                self._policy.check_url(safety, page.url)
            except PolicyViolation as exc:
                raise _HardFailure(
                    expected="post-click URL within permitted domains",
                    observed=f"redirected to {page.url}: {exc}",
                )
            await self._scan_page_errors(page, eh)
            return None

        if action.type == ActionType.TYPE:
            await loc.fill(value or "", timeout=action.timeout_ms)
            return None

        if action.type == ActionType.SELECT:
            await loc.select_option(label=value or "", timeout=action.timeout_ms)
            return None

        if action.type == ActionType.EXTRACT:
            text = (await loc.text_content(timeout=action.timeout_ms) or "").strip()
            return text

        if action.type == ActionType.ASSERT:
            text = (await loc.text_content(timeout=action.timeout_ms) or "").strip()
            if value and value not in text:
                raise _HardFailure(
                    expected=f"element contains '{value}'",
                    observed=f"element contains '{text}'",
                )
            return None

        raise _HardFailure(expected="known action type", observed=f"unknown: {action.type}")

    async def _scan_page_errors(self, page: Page, eh: ErrorHandlingSpec) -> None:
        try:
            body = await page.locator("body").text_content(timeout=2_000)
            body = body or ""
        except Exception:
            return  # Can't read body — don't silently assume no errors, but don't crash either
        for outcome in eh.expected_outcomes:
            if outcome.detection_pattern.lower() in body.lower():
                raise _BusinessOutcome(outcome.code, outcome.description)
        for pattern in eh.fail_patterns:
            if pattern.lower() in body.lower():
                raise _HardFailure(
                    expected=f"no failure pattern '{pattern}'",
                    observed=f"page body contains '{pattern}'",
                )
        for pattern in eh.recoverable_patterns:
            if pattern.lower() in body.lower():
                raise _Recoverable(pattern)

    async def _try_escalation(
        self, failed_result: ReplayResult, page: Page, step: Step,
        params: dict, run_id: str, step_idx: int, artifact: CapabilityArtifact,
        start: float, recovered: list[str], steps_completed: int,
    ) -> ReplayResult | Literal["continue"]:
        err = failed_result.error
        req = EscalationRequest(
            run_id=run_id,
            reason=f"Replay blocked at '{step.id}': {err.expected if err else 'unknown error'}",
            current_state=err.observed if err else "unknown state",
            goal=f"Complete step: {step.description}",
            step_num=step_idx,
            cdp_url=self._session.cdp_url,
            screenshot_path=Path(err.screenshot_path) if err and err.screenshot_path else None,
        )
        outcome = await self._escalation.handle(req)  # type: ignore[union-attr]

        # Record pause event regardless of description
        self._logger.human_action(
            outcome.human_action_description or f"Human took control at step '{step.id}' (no description given)"
        )

        if not outcome.resumed:
            return ReplayResult(
                run_id=run_id, artifact_id=artifact.id, artifact_name=artifact.name,
                status=ReplayStatus.ESCALATED, recovered_conditions=recovered,
                steps_completed=steps_completed, total_steps=len(artifact.steps),
                duration_s=time.monotonic() - start, evidence_path=str(self._evidence_dir),
                error=ErrorDetail(
                    step_id=step.id, step_description=step.description,
                    expected="human to resume after escalation",
                    observed="operator chose to abort",
                    timestamp=datetime.now(timezone.utc),
                ),
            )

        # Domain check on resumed browser state
        try:
            self._policy.check_url(artifact.safety, page.url)
        except PolicyViolation as exc:
            return self._pv(run_id, artifact, f"{step.id}_post_handoff",
                            "Domain check after human handoff", str(exc), start, steps_completed)

        # Verify step checkpoint to confirm human completed the action
        check_target = step.checkpoint
        if not check_target:
            # No declared checkpoint — use final checkpoint as proxy
            check_target = artifact.checkpoint
        resolved_cp = _substitute_checkpoint(check_target, params)
        try:
            await _verify_checkpoint(page, resolved_cp)
        except _CheckpointFailed as exc:
            screenshot = await self._screenshot(run_id, f"post_handoff_fail_{step_idx}")
            return ReplayResult(
                run_id=run_id, artifact_id=artifact.id, artifact_name=artifact.name,
                status=ReplayStatus.HARD_FAILURE, recovered_conditions=recovered,
                steps_completed=steps_completed, total_steps=len(artifact.steps),
                duration_s=time.monotonic() - start,
                error=ErrorDetail(
                    step_id=step.id,
                    step_description=f"Post-handoff verification: {check_target.description}",
                    expected=exc.expected, observed=exc.observed,
                    screenshot_path=screenshot, timestamp=datetime.now(timezone.utc),
                ),
            )

        # If the step was an extraction, try to collect the output now
        if step.action.type == ActionType.EXTRACT and step.action.output_name and step.action.locator:
            try:
                from cua.replay.locator import resolve_locator as _rl
                loc = await _rl(page, step.action.locator)
                text = (await loc.text_content(timeout=5_000) or "").strip()
                # Log the extracted value so it appears in evidence even though
                # we can't inject it into result.outputs from this call site.
                self._logger.human_action(
                    f"Post-handoff extraction of '{step.action.output_name}': {text}"
                )
            except Exception:
                pass

        return "continue"

    async def _screenshot(self, run_id: str, label: str) -> str:
        path = self._evidence_dir / f"{run_id}_{label}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self._session.page.screenshot(path=str(path))
        except Exception:
            pass
        return str(path)

    def _pv(self, run_id, artifact, step_id, description, observed, start,
             steps_completed=0, screenshot=None) -> ReplayResult:
        return ReplayResult(
            run_id=run_id, artifact_id=artifact.id, artifact_name=artifact.name,
            status=ReplayStatus.POLICY_VIOLATION,
            steps_completed=steps_completed, total_steps=len(artifact.steps),
            duration_s=time.monotonic() - start,
            error=ErrorDetail(
                step_id=step_id, step_description=description,
                expected="action within policy", observed=observed,
                screenshot_path=screenshot, timestamp=datetime.now(timezone.utc),
            ),
        )

    def _fail(self, run_id, artifact, step_id, completed, total, start, expected, observed) -> ReplayResult:
        return ReplayResult(
            run_id=run_id, artifact_id=artifact.id, artifact_name=artifact.name,
            status=ReplayStatus.HARD_FAILURE,
            steps_completed=completed, total_steps=total,
            duration_s=time.monotonic() - start,
            error=ErrorDetail(
                step_id=step_id, step_description=expected,
                expected=expected, observed=observed, timestamp=datetime.now(timezone.utc),
            ),
        )


def _substitute(template: str, params: dict[str, str]) -> str:
    for key, value in params.items():
        template = template.replace(f"{{{key}}}", str(value))
    return template


def _substitute_checkpoint(cp: CheckpointSpec, params: dict[str, str]) -> CheckpointSpec:
    return cp.model_copy(update={
        "target": _substitute(cp.target, params),
        "expected_value": _substitute(cp.expected_value, params) if cp.expected_value else None,
    })


async def _verify_checkpoint(page: Page, cp: CheckpointSpec) -> None:
    url = page.url

    if cp.type == "url_contains":
        if cp.target not in url:
            raise _CheckpointFailed(
                expected=f"URL contains '{cp.target}'", observed=f"URL is '{url}'",
            )
    elif cp.type == "url_exact":
        if url.rstrip("/") != cp.target.rstrip("/"):
            raise _CheckpointFailed(expected=f"URL == '{cp.target}'", observed=f"URL is '{url}'")
    elif cp.type == "element_present":
        try:
            await page.wait_for_selector(cp.target, timeout=5_000)
        except PlaywrightTimeout:
            raise _CheckpointFailed(
                expected=f"element '{cp.target}' present", observed="not found within timeout",
            )
    elif cp.type == "text_present":
        try:
            await page.wait_for_selector(f"text={cp.target}", timeout=5_000)
        except PlaywrightTimeout:
            raise _CheckpointFailed(
                expected=f"text '{cp.target}' on page", observed="not found within timeout",
            )
    elif cp.type == "element_text":
        try:
            el = await page.wait_for_selector(cp.target, timeout=5_000)
            actual = ((await el.text_content()) or "").strip()
            if cp.expected_value and cp.expected_value not in actual:
                raise _CheckpointFailed(
                    expected=f"element text contains '{cp.expected_value}'",
                    observed=f"element text is '{actual}'",
                )
        except PlaywrightTimeout:
            raise _CheckpointFailed(
                expected=f"element '{cp.target}' present", observed="not found within timeout",
            )


def _validate_inputs(artifact: CapabilityArtifact, params: dict[str, str]) -> str | None:
    """Returns an error string if inputs are invalid, None if OK."""
    step_ids = {s.id for s in artifact.steps}

    # Check params first so the error message is about the missing param, not the step ref
    for p in artifact.parameters:
        if not p.required:
            continue
        if p.name not in params:
            return f"Required parameter '{p.name}' ({p.description}) was not supplied."
        val = params[p.name]
        if p.type == "integer":
            try:
                int(val)
            except (ValueError, TypeError):
                return f"Parameter '{p.name}' must be an integer, got {val!r}."
        elif p.type == "decimal":
            try:
                float(val)
            except (ValueError, TypeError):
                return f"Parameter '{p.name}' must be a decimal number, got {val!r}."
        elif p.type == "boolean":
            if val.lower() not in ("true", "false", "1", "0", "yes", "no"):
                return f"Parameter '{p.name}' must be a boolean, got {val!r}."
        if p.validation_pattern:
            if not re.fullmatch(p.validation_pattern, val):
                return (
                    f"Parameter '{p.name}' value {val!r} does not match "
                    f"validation pattern '{p.validation_pattern}'."
                )

    for o in artifact.outputs:
        if o.source_step_id != "unknown" and o.source_step_id not in step_ids:
            return (
                f"Output '{o.name}' references step '{o.source_step_id}' "
                f"which does not exist in the artifact (steps: {sorted(step_ids)})."
            )

    for step in artifact.steps:
        for field_val in (step.action.value, step.action.url):
            if field_val and "{" in field_val:
                unresolved = re.findall(r"\{(\w+)\}", field_val)
                missing = [u for u in unresolved if u not in params]
                if missing:
                    return (
                        f"Step '{step.id}' contains unresolved placeholder(s) {missing}. "
                        f"Supply these as input parameters."
                    )

    return None


def _validate_outputs(declared: list[OutputSpec], collected: dict[str, str]) -> str | None:
    for spec in declared:
        if spec.name not in collected:
            return f"Declared output '{spec.name}' ({spec.description}) was not collected during replay."
        val = collected[spec.name]
        if spec.type in ("decimal",):
            clean = re.sub(r"[$,\s]", "", val)
            try:
                float(clean)
            except (ValueError, TypeError):
                return (
                    f"Output '{spec.name}' declared type=decimal but extracted value "
                    f"{val!r} cannot be parsed as a number."
                )
        elif spec.type == "integer":
            try:
                int(val.strip())
            except (ValueError, TypeError):
                return (
                    f"Output '{spec.name}' declared type=integer but extracted value "
                    f"{val!r} is not a valid integer."
                )
    return None
