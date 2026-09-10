# Design Report

## 1. Architecture

```
Goal + target URL
      ↓
[DiscoveryAgent]    gpt-5.6-luna drives a real browser via OpenAI async function calling.
      ↓ ActionRecord[]
[ArtifactBuilder]   Compiles the action log into a typed, parameterized CapabilityArtifact (JSON).
      ↓ CapabilityArtifact
[ReplayExecutor]    Deterministic replay — no LLM, policy-checked at every step.
      ↓ ReplayResult
Caller (AI agent or CLI)
```

**Key decisions:**

- **Single process, no infrastructure.** No queues or shared databases. `ArtifactStore`, `ReplayExecutor`, and `EscalationHandler` are clean interfaces that slot into a task-queue architecture later without changing callers.

- **OpenAI async function calling with ARIA snapshot.** The agent receives the accessibility tree (`page.aria_snapshot()`) plus a screenshot. The ARIA tree gives the model exact role/name pairs for element targeting without requiring visual coordinate inference. Screenshots provide visual context for ambiguous layouts.

- **Playwright + Chromium.** Chosen for CDP access (same-session human handoff), `aria_snapshot()` support, and built-in trace recording.

- **Flask mock target with legacy HTML.** Table layouts, no `data-testid`, label-based form associations — representative of real core banking UIs without requiring access to one.

---

## 2. Artifact Schema

```
CapabilityArtifact
  id, name, description, schema_version: "1.0"
  target: TargetSpec          (entry_url, surface_type, tenant_id)
  parameters: ParameterSpec[] (typed inputs; {param} placeholders substituted at replay time)
  outputs: OutputSpec[]       (typed extractions; each bound to a source step_id)
  steps: Step[]
    id, description
    action: StepAction
      type, locator?, value?, url?, timeout_ms, risk_level, output_name
      error_handling: ErrorHandlingSpec
        expected_outcomes[]   — business results (not system failures)
        recoverable_patterns  — transient; wait/retry
        fail_patterns         — hard stop
    checkpoint?: CheckpointSpec   (optional per-step assertion)
  checkpoint: CheckpointSpec      (required final assertion)
  safety: SafetySpec              (permitted domains, action types, sensitive param names)
  metadata: ArtifactMetadata      (discovery_run_id, model, timing)
```

**Design rationale:**

- **Parameterized throughout.** `{param_name}` placeholders appear in step `value`, `url`, locator values, and checkpoint `target`. The artifact builder substitutes discovered literal values (e.g. `/members/12345`) with placeholders (`/members/{member_id}`) so the artifact works for any member ID, not just the one recorded. Replay substitutes back before every interaction.

- **Multi-strategy LocatorSpec.** `primary` is always the most stable strategy (ARIA role+name). `fallbacks` cover label text, placeholder, and visible text. Rationale is recorded for reviewers. Non-text locators that match multiple elements raise `AmbiguousLocatorError` rather than silently selecting the first.

- **Three-tier error taxonomy per step.** `expected_outcomes` are legitimate business results returned as `BUSINESS_OUTCOME` — not failures. `recoverable_patterns` trigger bounded wait/rescan (never re-execute). `fail_patterns` are hard stops. The taxonomy is structurally enforced in the result contract, not just documented.

- **Sensitive parameters.** Params declared `sensitive=True` have their `example` cleared before the artifact is saved. The `safety.sensitive_param_names` list tells the logger to redact those values in all log text. The runtime value is kept in memory only during the run.

- **Versioned.** `schema_version: "1.0"` is checked at replay load time. Future schema changes can be detected and migrated.

---

## 3. Determinism & Error Handling

**Locator resolution (most stable → least stable):**
1. `ARIA_ROLE` — `get_by_role(role, name=...)` — survives visual redesigns
2. `ARIA_LABEL` — `get_by_label(text)` — reliable for labeled form fields
3. `PLACEHOLDER` — `get_by_placeholder(text)` — moderate stability
4. `TEXT` — `get_by_text(text)` — good for buttons in legacy apps without ARIA
5. `XPATH` / `CSS` — structural fallbacks

All strategies wait for visibility before counting elements (slow-loading elements aren't missed). Non-text strategies with multiple matches raise `AmbiguousLocatorError`.

**Checkpoint verification.** The executor substitutes params into checkpoint targets before verifying, so `/members/{member_id}` becomes `/members/12345` at replay time. Per-step checkpoints run after every step including recovered ones (proves the action succeeded, not just that loading cleared).

**Recovery model.** When a recoverable pattern is detected after a click:
1. Log "attempting recovery" (separate event from successful recovery).
2. Wait `_RECOVERY_WAIT_S` seconds, then `wait_for_load_state` (any exception during wait is ignored; state is determined by the rescan).
3. Re-scan only — never re-execute the original action (prevents double-submission).
4. If cleared → append to `recovered_conditions` and log "recovered" AFTER verification.
5. If rescan reveals a business outcome or hard failure → return that structured result.
6. If still loading after `_RECOVERY_MAX_RETRIES` → `HARD_FAILURE`.

If the page body is unreadable during recovery scan, it raises `_Recoverable("page content unreadable")` so the loop retries rather than falsely assuming the condition cleared.

**Result contract:** `SUCCESS` · `BUSINESS_OUTCOME` · `HARD_FAILURE` · `POLICY_VIOLATION` · `ESCALATED`. All major replay paths return a typed `ReplayResult`. Unexpected browser/Playwright errors during checkpoint verification are caught and converted to `HARD_FAILURE` with an `ErrorDetail`. Catastrophic process-level failures (OOM, OS signal) are outside scope.

**UI drift.** Because the target is stable enterprise software, drift handling is secondary. Fallback locators cover the common case. If all locators fail, it is `HARD_FAILURE` with evidence.

---

## 4. Heterogeneity & Multi-Tenant

**Surface abstraction (designed; one surface implemented):**

The current executor directly calls Playwright for observation and interaction. Extending to a desktop surface requires a well-defined adapter interface, not just swapping `BrowserSession`. The interface would cover four operations:

- `observe(session) → (aria_tree: str, screenshot_b64: str)` — reads current state
- `resolve_locator(session, spec: LocatorSpec) → native_element` — finds a control
- `act(session, element, action: StepAction, value: str | None) → None` — performs the action
- `check_url(session, safety: SafetySpec) → None` — enforces domain policy

`SurfaceType` in `TargetSpec` selects which adapter to instantiate at replay time. For Win32 desktop apps, the mapping is: `ARIA_ROLE` → UIA `ControlType` + `Name` property (not `AutomationId`, which is a separate developer-assigned ID analogous to a web test ID); `ARIA_LABEL` → UIA `Name`; `TEXT` → UIA `Name` substring search. A `DesktopAdapter` would implement the same four-operation interface using `pywinauto` (Win32) or `pyatspi` (Linux/Java AT-SPI). The artifact schema and step definitions remain unchanged.

**Multi-tenant reuse (designed; not built):**

The key abstraction is `CapabilityArtifact.target.tenant_id = None` for shared/base artifacts. Per-tenant specialization works via an `OverrideSet`:

```
OverrideSet
  artifact_id, tenant_id, vendor_version
  step_overrides: dict[step_id, partial StepAction]  # Replace locator for this tenant
  param_defaults: dict[str, str]                      # Tenant-specific default values
```

The replay executor merges the base artifact with tenant overrides at load time. Only the divergent steps need overrides — not a full re-record — when a tenant runs the same vendor product with different button labels or navigation structure. Override precedence: tenant-specific overrides replace base step fields; unmentioned fields inherit from the base.

Drift detection: each JSONL run log contains `steps_completed / total_steps`. A `StabilityTracker` would aggregate per-artifact-per-tenant and flag falling success rates for review or re-recording.

---

## 5. Escalation & Handoff

**Detecting "stuck":** During discovery the model calls `escalate()` when on the same URL for multiple steps without progress, or when it encounters an unhandled state (CAPTCHA, permission denial, unexpected modal).

During replay, `HARD_FAILURE` triggers escalation when `--escalate-on-failure` is passed to the CLI. Policy violations and business outcomes do not trigger escalation — they are handled programmatically.

**Control-transfer mechanism (implemented):**

1. The executor pauses after the failed step — no further Playwright calls.
2. `EscalationHandler` prints an `EscalationRequest` (run ID, goal, step, reason, CDP URL, screenshot).
3. The human connects Chrome to `chrome://inspect/#devices` and accesses the **same live browser session** via CDP — no new window or session is created. Page state, cookies, and session are preserved.
4. Operator performs the manual steps, then types `resume` in the CLI.
5. If the failed step was an extraction, the executor re-runs it after handoff and adds the value to the returned outputs, so required-output validation passes.
6. If the step has a declared checkpoint, it is verified after handoff. If not, verification is deferred to the final checkpoint — this avoids incorrectly failing resume when the current page state is intermediate (e.g. just completed login, not yet on the member detail page).
7. The operator's description is logged as a `human_action` evidence event. When no description is given, a default event is still recorded so the audit trail is consistent.

**Audit trail.** Three events are written per handoff: `handoff_start` (reason for blocking), `handoff_end` (resumed: true/false), and `human_action` (operator's description, only when provided). Automated extraction after handoff is logged as `step_success`, not `human_action`. Control transfer, operator outcome, and automated continuation are recorded as distinct event types.

**What is mocked:** the operator surface (CLI stdin instead of a browser-based console). The mechanism — CDP session sharing, same browser instance, pause/resume state machine, audit logging — is real.

---

## 6. Safety

**Domain allowlist.** Enforced before the initial navigation, after click-triggered navigation, and before and after explicit `NAVIGATE` steps in replay. Discovery stops immediately (status `policy_violation`) when a click navigates outside the allowlist. Replay uses the allowlist recorded during discovery (preserved from CLI `--permitted-domain` flags), not reconstructed from visited URLs. `PolicyEnforcer.check_url()` uses `urlparse().hostname` to correctly handle IPv6 and user-info in URLs.

**Action allowlist.** Every step's `action.type` is checked against `permitted_action_types` before execution. Discovery enforces the same policy as replay — a recorded action that would be blocked in replay is also blocked during discovery.

**Risk classification.** Effective risk = `max(action.risk_level, type_default_risk)`. Type defaults: `SUBMIT → HIGH`, `TYPE/SELECT → MODERATE`, everything else `SAFE`. This ensures `SUBMIT` is always treated as `HIGH` even when `risk_level` is at its field default of `SAFE`.

**`requires_human_approval_for` is independent of risk.** A `CLICK` listed for approval is blocked before execution regardless of its computed effective risk. This allows explicit approval gates on specific controls without relying on risk labels.

**PII / secrets.** Sensitive params (`sensitive=True`) have their `example` cleared before artifact serialization — the artifact stores `{login_password}`, never the literal value. The logger receives both param names and runtime values, redacting them from all text fields (goals, reasoning, extracted values, errors, business-outcome descriptions). `redact_text()` additionally masks SSN and long account-number patterns in all logged text.

**Trace capture.** When sensitive params are declared in the artifact's `safety.sensitive_param_names`, both discovery and replay run Playwright tracing with `snapshots=False`, excluding DOM snapshots (which expose form field values). Screenshots are still captured for debugging; they are not masked. For both discovery and replay, a continuous navigation guard (`page.route`) blocks document navigations to forbidden domains at the browser level, rather than only checking post-facto.

**Limits.** The domain allowlist prevents navigation outside the permitted set but does not prevent an in-domain page from inducing the agent to POST sensitive data to an in-scope endpoint. Mitigations in production: mandatory human approval for all `HIGH`-risk actions, Content Security Policy on the browser context.

---

## 7. Cuts

| Cut | Why | What to build next |
|---|---|---|
| Operator web console | WebSocket CDP proxy + frontend is a separate project; out of scope per the brief | FastAPI WebSocket endpoint streaming CDP frames; operator browser connects to same session; resume via webhook |
| Multi-tenant `OverrideSet` | Design is described; schema supports `tenant_id` | `ArtifactStore.load_with_overrides(artifact_id, tenant_id)` merging from an `overrides/` directory |
| Desktop surface adapter | Requires `pywinauto` (Win32) or `pyatspi` (Linux/Java) | `DesktopAdapter` implementing the four-operation interface described in §4; `SurfaceType` drives selection |
| Approval gating (`draft` → `approved`) | Replay runs any saved artifact | `approval_state` field; `cua approve <artifact>` CLI; block unattended replay of `draft` artifacts |
| Multi-run stability tracking | Per-run JSONL exists; aggregation not built | `cua stability <artifact> --runs N` replaying N times and reporting per-step flakiness |
| Assisted fallback on single-step failure | Requires bounded LLM call in replay path | On `ElementNotFoundError`, call model with screenshot + failed step spec; try revised locator once; record as evidence |
| Genuine discovery evidence | Requires a real OpenAI API key; example logs show the expected output format | Run Steps 1–3 in the README with your API key; evidence is saved to `evidence/` automatically |
