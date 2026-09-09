# Design Report

## 1. Architecture

The system is a single-process Python application with four logical phases and clean module boundaries:

```
Goal + target URL
      ↓
[DiscoveryAgent]    GPT-4o drives a real browser via OpenAI async function calling.
      ↓ ActionRecord[]
[ArtifactBuilder]   Compiles the action log into a typed CapabilityArtifact (JSON).
      ↓ CapabilityArtifact
[ReplayExecutor]    Deterministic replay — no LLM, policy-checked at every step.
      ↓ ReplayResult
Caller (AI agent or CLI)
```

**Key decisions:**

- **Single process, no infrastructure.** No message queues, no services, no shared database. Justified: the task is a focused end-to-end vertical slice. The interfaces (`ArtifactStore`, `ReplayExecutor`, `EscalationHandler`) are clean enough to slot into a task-queue architecture later without changing callers.

- **OpenAI async function calling over screenshot-only CUA.** The agent receives the accessibility tree as structured text plus a screenshot. The a11y tree (`page.aria_snapshot()`) is the primary decision surface — it gives the model exact role/name pairs rather than requiring visual inference from pixels. This also generalises to desktop apps (UIA/AT-SPI expose the same concepts). The screenshot provides visual context for ambiguous layouts.

- **Playwright + Chromium.** Chosen for: full CDP access (needed for human handoff without opening a new session), async Python API, `aria_snapshot()`, and built-in Trace Viewer recording.

- **Flask mock target with intentionally legacy HTML.** Table-based layouts, no `data-testid`, label-based form associations — representative of real core banking UIs. The locator priority (ARIA role/name → label → placeholder → visible text) is optimised for this surface.

---

## 2. Artifact Schema

The `CapabilityArtifact` is the core data structure. Its design was the most deliberate decision.

```
CapabilityArtifact
  id, name, description, schema_version: "1.0"
  target: TargetSpec          (entry_url, surface_type, tenant_id)
  parameters: ParameterSpec[] (typed inputs; {param} placeholders substituted at replay time)
  outputs: OutputSpec[]       (typed extractions; each bound to a step_id)
  steps: Step[]
    id, description
    action: StepAction
      type, locator?, value?, url?, timeout_ms, risk_level, output_name
      error_handling: ErrorHandlingSpec
        expected_outcomes[]   — business results (not system failures)
        recoverable_patterns  — transient; wait/retry
        fail_patterns         — hard stop
    checkpoint?: CheckpointSpec   (optional per-step assertion)
  checkpoint: CheckpointSpec      (required final success assertion)
  safety: SafetySpec              (permitted domains, action types, sensitive param names)
  metadata: ArtifactMetadata      (discovery_run_id, model, timing)
```

**Schema design rationale:**

- **Typed, parameterized contract.** `{param_name}` placeholders in `value` and `url` fields are substituted at replay time. The artifact builder also parameterizes discovered URLs (e.g. `/members/12345` → `/members/{member_id}`) and the final checkpoint so the artifact is genuinely reusable across different parameter values.

- **Sensitive parameters.** Params declared with `sensitive=True` (e.g. `login_password`) have their `example` value cleared from the artifact. Only the `{placeholder}` appears in step values. The safety spec's `sensitive_param_names` list tells the logger to redact them from evidence.

- **Multi-strategy LocatorSpec.** Every element has a `primary` strategy and ordered `fallbacks`. Primary is always the most semantically stable (ARIA role+name). Fallbacks cover label text, placeholder, and visible text — all of which are stable on legacy apps that lack test IDs. The `rationale` field is reviewable by humans and policy tooling.

- **Three-tier error taxonomy per step.** `expected_outcomes` are legitimate business results (e.g. "member not found") returned as `BUSINESS_OUTCOME` — not failures. `recoverable_patterns` trigger a bounded wait/retry. `fail_patterns` are hard stops. This distinction is enforced structurally, not just documented.

- **Versioned (`schema_version: "1.0"`).** Future schema changes can be detected at load time and handled with a migration path.

---

## 3. Determinism & Error Handling

**Locator strategy (most stable → least stable):**

1. `ARIA_ROLE` — `get_by_role(role, name=...)` — survives visual redesigns
2. `ARIA_LABEL` — `get_by_label(text)` — reliable for labelled form fields
3. `PLACEHOLDER` — `get_by_placeholder(text)` — moderate stability
4. `TEXT` — `get_by_text(text)` — good for buttons/links in legacy apps without ARIA
5. `XPATH` / `CSS` — last resort fallbacks

All strategies are tried in order by `replay/locator.py`. `ElementNotFoundError` is raised only after all fallbacks fail — this is a hard failure with a screenshot, not a business outcome.

**Wait strategy:** After every click/submit the executor calls `wait_for_load_state("networkidle")`. For SPA-style transitions, step checkpoints act as natural synchronization points.

**Recovery:** When a recoverable pattern is detected (e.g. "Loading"), the executor waits `_RECOVERY_WAIT_S` seconds, calls `wait_for_load_state`, and retries the step up to `_RECOVERY_MAX_RETRIES` times. If the condition persists after all retries, it surfaces as `HARD_FAILURE` — not silently skipped.

**Output validation:** After all steps complete, the executor verifies that every declared output in `artifact.outputs` was actually collected. Missing outputs are a `HARD_FAILURE` with a descriptive error — the caller is never returned a partial result that looks like success.

**Three-tier result contract:**
- `SUCCESS` — all steps completed, checkpoint passed, all declared outputs present
- `BUSINESS_OUTCOME` — a legitimate business state detected (e.g. member not found); caller handles
- `HARD_FAILURE` — unexpected condition; `error` field has step ID, expected, observed, screenshot path
- `POLICY_VIOLATION` — action or domain blocked by safety spec
- `ESCALATED` — human intervention completed (discovery only)

**UI drift:** Because the target is stable enterprise software, drift handling is secondary. Fallback locators cover the common case. If all locators fail, it is a `HARD_FAILURE` with evidence — a human reviews, the artifact is re-recorded or the affected step's locators are overridden (see §4).

---

## 4. Heterogeneity & Multi-Tenant

**Surface abstraction (designed; one surface implemented):**

The seam between "how we perceive and act on a surface" and "the recorded flow" is clean:

- `BrowserSession` / `PageObserver` are web-specific (Playwright). Replace with a `DesktopSession` + UIA/ATK observer for Win32/Java — the rest of the system is unchanged.
- `LocatorMethod` values (`ARIA_ROLE`, `ARIA_LABEL`, `TEXT`, `XPATH`) have direct equivalents in UIA (`AutomationId`, `Name`, `ControlType`) and Java Access Bridge. Adding `UIA_AUTOMATION_ID` is a new enum member, not a schema change.
- `SurfaceType` in `TargetSpec` tells the replay engine which adapter to instantiate. Each adapter implements `resolve_locator(spec) → native element handle` and `observe(session) → (text, screenshot)`.

**Multi-tenant reuse (designed; not built):**

The key abstraction is `CapabilityArtifact.target.tenant_id = None` (shared/"base" artifact). Per-tenant specialization would work via an `OverrideSet`:

```
OverrideSet
  artifact_id, tenant_id
  step_overrides: dict[step_id, partial StepAction]  # Replace locator/value for this tenant
  param_defaults: dict[str, str]                      # Tenant-specific defaults
```

The replay executor would merge the base artifact with the tenant's overrides at load time. Only the affected steps' locators need overriding — not a full re-record — when a tenant runs the same vendor product with different branding (different button text, different nav labels).

Drift detection: each JSONL run log includes `steps_completed / total_steps`. A `StabilityTracker` would aggregate per-artifact-per-tenant and flag artifacts whose success rate falls below a threshold for review or re-recording.

---

## 5. Escalation & Handoff

**Detecting "stuck":** During discovery the agent calls `escalate()` when it has been on the same URL for multiple steps with no progress, encounters a state it cannot parse (CAPTCHA, unexpected modal), or is asked to perform an irreversible action not in the goal.

**Control-transfer mechanism (implemented):**

1. The agent loop exits after `escalate()` — no more Playwright calls are made.
2. `EscalationHandler` prints a structured `EscalationRequest` (run ID, goal, step, reason, CDP URL, screenshot path).
3. The human operator connects Chrome to `chrome://inspect/#devices` to access the same live browser session (not a new one) via the CDP remote debugging port (`--remote-debugging-port=9222`). The page state, cookies, and session are fully preserved.
4. The operator performs the manual steps and types 'resume' in the CLI.
5. The operator's free-text description is logged as a `human_action` event in the JSONL evidence file.
6. The agent loop resumes from the current page state.

**What is mocked:** the operator surface (CLI stdin instead of a browser-based co-browsing console). The mechanism — CDP session sharing, same browser instance, pause/resume state machine, audit logging — is real and end-to-end.

**Replay escalation:** Replay does not currently escalate to a human on failure — it returns a `HARD_FAILURE` result with evidence. Production replay would support a bounded single-step LLM recovery attempt before escalation (see §7 — Cuts).

---

## 6. Safety

**Domain allowlist:** Enforced before the initial `goto()` in both discovery and replay, and before every `NAVIGATE` step in replay. `PolicyEnforcer.check_url()` parses the hostname and rejects anything not in `permitted_domains`. The permitted list is derived from the domains actually visited during discovery — no extra configuration needed.

**Action type allowlist:** Every step's `action.type` is checked against `permitted_action_types` before execution. A read-only capability cannot call `SUBMIT` even if the artifact were modified, because `SUBMIT` is not in its declared type list.

**Risk classification (implemented):**
- `SAFE` — navigate (read), click (read-only), extract, wait, assert
- `MODERATE` — type, select (edit field before commit)
- `HIGH` — submit; click with description containing "confirm", "submit", "create"

`HIGH` actions are blocked by default unless `allow_high_risk=True` is passed to the executor, or the action is in `requires_human_approval_for` (which forces escalation before execution).

**PII / secrets:**
- Sensitive params (`sensitive=True`) never have their example values stored in artifacts.
- `safety.sensitive_param_names` tells the logger to redact those values in JSONL events.
- `redact_text()` strips SSNs and long account-number patterns from any logged text.
- Artifacts store only `{placeholder}` tokens for sensitive fields; actual credentials are supplied at replay time and never written to disk.

**Limits:** The domain allowlist catches obvious exfiltration. It does not prevent a crafted page from inducing the agent to POST sensitive data to an in-scope endpoint. Mitigations in production: screenshot-based content review before SUBMIT, Content Security Policy on the browser context, mandatory human approval for all HIGH-risk actions.

---

## 7. Cuts

| Cut | Reason | What to build next |
|---|---|---|
| Operator console (browser UI) | WebSocket CDP proxy + frontend is a significant separate project; out of scope per the brief | FastAPI WebSocket endpoint streaming CDP frames; operator browser connects to same session; resume via webhook |
| Multi-tenant `OverrideSet` | Design is described; artifact schema supports `tenant_id`; merge logic not implemented | `ArtifactStore.load_with_overrides(artifact_id, tenant_id)` merging from `overrides/` directory |
| Desktop surface adapter | Requires `pywinauto` (Win32) or `pyatspi` (Linux/Java) | `DesktopSession` with same `observe()` / `resolve_locator()` interface; `SurfaceType` drives adapter selection |
| Approval gating (`draft` → `approved`) | Replay runs any saved artifact | Add `approval_state` field; `cua approve <artifact>` CLI; block unattended replay of `draft` artifacts |
| Multi-run stability tracking | Per-run JSONL exists; aggregation not built | `cua stability <artifact> --runs N` replaying N times and reporting success rate + flaky-step heatmap |
| Assisted fallback on replay failure | Requires bounded LLM call in replay path | On `ElementNotFoundError`, call GPT-4o with screenshot + failed step spec; get revised locator; try once; record as evidence |
| Replay escalation | Replay returns `HARD_FAILURE` and stops | Hook `EscalationHandler` into replay on hard failure; human takes over live session; automation resumes |
| Agent-facing capability API | Artifacts already have the right shape for OpenAI function definitions | `GET /capabilities` list; `POST /capabilities/{name}/invoke` with typed params; `schema_version` field already present |
