# Computer-Use Automation System

Discover-once / replay-many browser automation for legacy back-office applications.

1. **Discovery** - `gpt-5.6-luna` drives a real browser to accomplish a goal, recording every action.
2. **Artifact** - The run is compiled into a typed, versioned `CapabilityArtifact` (JSON in `capabilities/`).
3. **Replay** - A deterministic executor re-runs the artifact without the LLM, validates all inputs and outputs, and reports structured results.
4. **Escalation** - When automation is blocked, a human takes control of the live session via CDP remote debugging, then hands back.

Target: a locally-hosted mock "Heritage Credit Union" portal with intentionally legacy HTML (table layouts, no test IDs) that represents a real core-banking back-office application.

---

## Setup

### Prerequisites

- Python 3.11+
- An OpenAI API key with access to `gpt-5.6-luna`. If unavailable, set `OPENAI_MODEL=gpt-4o` in `.env`.
- Chromium (installed automatically by Playwright)

### Install

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\playwright install chromium
```

**macOS / Linux:**
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

All commands below assume the venv is activated. On Windows without activation, prefix with `.venv\Scripts\`.

### Configure

```bash
cp .env.example .env
# Set OPENAI_API_KEY=sk-...
# Set OPENAI_MODEL=gpt-4o  (if gpt-5.6-luna is not available on your key)
```

The CLIs load `.env` automatically (not `.env.local`). If you use a different file, source it manually before running.

---

## Demo path

### Step 1 - Start the mock banking app

```bash
python -m demo.mock_app.app
# Serving on http://127.0.0.1:5000  (login: demo / demo123)
```

### Step 2 - Discovery (genuine LLM-driven run)

```bash
python -m demo.run_discovery \
  --goal "Log in with username demo, navigate to member search, look up member 12345, and read their savings account balance" \
  --url "http://127.0.0.1:5000" \
  --output "capabilities/lookup_savings_balance.json" \
  --param "member_id=12345:The member ID to look up" \
  --sensitive-param "login_password=demo123:Staff portal login password" \
  --output-spec "savings_balance:decimal:The savings account balance"
```

The `--sensitive-param` flag keeps the password out of all logs and artifacts. It is stored only as the placeholder `{login_password}` in the saved capability.

**Windows PowerShell** (use backtick for line continuation):
```powershell
python -m demo.run_discovery `
  --goal "Log in with username demo, navigate to member search, look up member 12345, and read their savings account balance" `
  --url "http://127.0.0.1:5000" `
  --output "capabilities/lookup_savings_balance.json" `
  --param "member_id=12345:The member ID to look up" `
  --sensitive-param "login_password=demo123:Staff portal login password" `
  --output-spec "savings_balance:decimal:The savings account balance"
```

Chrome opens, the model drives it step by step, and the artifact plus evidence (JSONL log, screenshots, Playwright trace) are saved.

### Step 3 - Replay (deterministic, no LLM)

```bash
# Happy path (supply the sensitive login_password at runtime - never stored in artifact)
python -m demo.run_replay \
  --artifact "capabilities/lookup_savings_balance.json" \
  --params "member_id=12345" \
  --params "login_password=demo123"

# Business outcome - member does not exist
python -m demo.run_replay \
  --artifact "capabilities/lookup_savings_balance.json" \
  --params "member_id=99999" \
  --params "login_password=demo123"

# Pause on hard failure for human intervention
python -m demo.run_replay \
  --artifact "capabilities/lookup_savings_balance.json" \
  --params "member_id=12345" \
  --params "login_password=demo123" \
  --escalate-on-failure
```

Exit codes: `0` success, `1` hard failure, `2` business outcome, `3` policy violation.

---

## Tests

```bash
# Unit tests - no browser, no API key needed (69 tests)
python -m pytest tests/ -m "not browser" -v

# Browser integration tests (requires mock app running on ports 5051-5053)
python -m pytest tests/ -m "browser" -v
```

---

## Evidence

`capabilities/` and `evidence/` contain both genuine run output and illustrative examples:

| Path | Contents |
|---|---|
| `capabilities/lookup_savings_balance.json` | Genuine artifact from the LLM discovery run |
| `evidence/discovery_20260914_204918/` | Genuine LLM-driven discovery run (SUCCESS, savings_balance = $8750.00) |
| `evidence/replay_20260914_205603/` | Genuine replay run (SUCCESS, member 12345) |
| `evidence/replay_20260914_205609/` | Genuine replay run (BUSINESS_OUTCOME, member 99999 not found) |
| `evidence/discovery_example/run.jsonl` | Illustrative example log showing expected format |
| `evidence/replay_success_example/run.jsonl` | Illustrative example SUCCESS log |
| `evidence/replay_notfound_example/run.jsonl` | Illustrative example BUSINESS_OUTCOME log |

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | (required) | Required for discovery |
| `OPENAI_MODEL` | `gpt-5.6-luna` | Model for discovery - set to `gpt-4o` if needed |
| `BROWSER_HEADLESS` | `false` | Set `true` for headless Chrome |
| `BROWSER_CDP_PORT` | `9222` | Remote debugging port used by `--escalate-on-failure` |
| `MOCK_APP_PORT` | `5000` | Port for the mock banking app |

`BROWSER_HEADLESS` and `BROWSER_CDP_PORT` are read from the environment by the CLIs. There is no `--cdp-port` CLI flag; use the environment variable instead.
