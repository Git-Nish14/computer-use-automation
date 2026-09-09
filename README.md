# Computer-Use Automation System

Discover-once / replay-many browser automation for legacy back-office applications.

1. **Discovery** — `gpt-5.6-luna` drives a real browser to accomplish a goal, recording every action.
2. **Artifact** — The run is compiled into a typed, versioned `CapabilityArtifact` (JSON in `capabilities/`).
3. **Replay** — A deterministic executor re-runs the artifact without the LLM, validates all inputs and outputs, and reports structured results.
4. **Escalation** — When automation is stuck, a human takes control of the live session via CDP remote debugging, then hands back.

Target: a locally-hosted mock "Heritage Credit Union" portal with intentionally legacy HTML (table layouts, no test IDs) that stands in for a real core-banking application.

---

## Setup

### Prerequisites

- Python 3.11+
- An OpenAI API key with access to `gpt-5.6-luna` (falls back gracefully to `gpt-4o` via `OPENAI_MODEL`)
- Chromium (installed automatically by Playwright)

### Install

**Windows (PowerShell):**

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\playwright install chromium
```

**macOS / Linux (bash):**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

All commands below assume the venv is activated (so `python` resolves to `.venv\Scripts\python` / `.venv/bin/python`).  On Windows without activation, prefix commands with `.venv\Scripts\`.

### Configure

```bash
cp .env.example .env
# Edit .env — set OPENAI_API_KEY=sk-...
# Optionally set OPENAI_MODEL=gpt-4o if gpt-5.6-luna is unavailable
```

---

## Demo path

### Step 1 — Start the mock banking app

Open a terminal and keep it running:

```bash
python -m demo.mock_app.app
# → Serving on http://127.0.0.1:5000  (staff login: demo / demo123)
```

### Step 2 — Discovery (genuine LLM-driven run)

In a second terminal:

```bash
python -m demo.run_discovery \
  --goal "Log in with username demo and password demo123, navigate to member search, look up member 12345, and read their savings account balance" \
  --url "http://127.0.0.1:5000" \
  --output "capabilities/lookup_savings_balance.json" \
  --param "member_id=12345:The member ID to look up" \
  --output-spec "savings_balance:decimal:The savings account balance"
```

**Windows PowerShell** (no line continuation with `\`):

```powershell
python -m demo.run_discovery `
  --goal "Log in with username demo and password demo123, navigate to member search, look up member 12345, and read their savings account balance" `
  --url "http://127.0.0.1:5000" `
  --output "capabilities/lookup_savings_balance.json" `
  --param "member_id=12345:The member ID to look up" `
  --output-spec "savings_balance:decimal:The savings account balance"
```

Chrome opens, `gpt-5.6-luna` drives it, and the artifact + evidence (JSONL log, screenshots, Playwright trace) are saved.

### Step 3 — Replay (deterministic, no LLM)

```bash
# Happy path — member 12345 exists
python -m demo.run_replay \
  --artifact "capabilities/lookup_savings_balance.json" \
  --params "member_id=12345"

# Business outcome — member 99999 does not exist
python -m demo.run_replay \
  --artifact "capabilities/lookup_savings_balance.json" \
  --params "member_id=99999"

# With human escalation on hard failure
python -m demo.run_replay \
  --artifact "capabilities/lookup_savings_balance.json" \
  --params "member_id=12345" \
  --escalate-on-failure
```

Exit codes: `0` success · `1` hard failure · `2` business outcome · `3` policy violation.

---

## Tests

```bash
# Unit tests — no browser, no API key required
python -m pytest tests/ -m "not browser" -v

# Browser integration tests (requires mock app on ports 5051-5053)
python -m pytest tests/ -m "browser" -v
```

---

## Evidence

`evidence/` and `capabilities/` contain example output files committed to this repo:

| Path | Contents |
|---|---|
| `capabilities/lookup_savings_balance.json` | Example artifact produced by discovery |
| `evidence/discovery_example/run.jsonl` | Structured log from a discovery run |
| `evidence/replay_success_example/run.jsonl` | Replay log — SUCCESS result |
| `evidence/replay_notfound_example/run.jsonl` | Replay log — BUSINESS_OUTCOME member_not_found |

To produce genuine evidence with your API key, run Steps 1-3 above.

---

## Project layout

```
cua/
  agent/          LLM-driven discovery (gpt-5.6-luna async function calling + ARIA tree)
  artifact/       CapabilityArtifact schema (Pydantic v2) + JSON store
  browser/        Playwright session + aria_snapshot page observer
  escalation/     Human handoff (CDP remote debugging, pause/resume, audit log)
  observability/  Structured JSONL logger with sensitive-value redaction
  replay/         Deterministic executor, multi-strategy locator, typed result contract
  safety/         Policy enforcer + PII redactor

demo/
  mock_app/       Flask banking mock (legacy HTML — table layouts, no test IDs)
  run_discovery.py  Discovery CLI
  run_replay.py     Replay CLI

capabilities/     Saved CapabilityArtifact JSON files
evidence/         JSONL logs, screenshots, Playwright traces
tests/            Unit tests + browser integration tests
REPORT.md         Design write-up
```

---

## Key environment variables

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | — | Required for discovery |
| `OPENAI_MODEL` | `gpt-5.6-luna` | Model for the discovery agent |
| `BROWSER_HEADLESS` | `false` | Set `true` for headless Chrome |
| `BROWSER_CDP_PORT` | `9222` | Remote debugging port for human escalation handoff |
| `MOCK_APP_PORT` | `5000` | Port for the mock banking app |

The CLI reads `BROWSER_HEADLESS` and `BROWSER_CDP_PORT` in addition to the `--headless` / `--cdp-port` flags, so they can be set in `.env` without changing commands.
