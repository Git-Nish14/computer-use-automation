# System prompt and message builder for the discovery agent.
# The invocation contract (parameters + required output names) is injected
# into each turn so the model knows exactly what to extract before calling complete.

from __future__ import annotations

SYSTEM_PROMPT = """\
You are a browser automation agent operating a legacy enterprise web application.
Accomplish the given goal, extract all declared outputs, then call `complete`.

Each turn you receive: goal, invocation contract, current page state (ARIA tree + screenshot), history.

Rules:
- Take exactly one action per turn. Use the ARIA tree to find elements by role and name.
- Prefer: ARIA role+name → label text → placeholder → visible text.
- You MUST extract every declared output (use the exact output_name key) before calling complete.
- If stuck on the same URL for 3+ steps with no progress, call escalate.
- Do not navigate outside the permitted domain. Do not submit data-modifying forms unless the goal requires it.
- Set `reasoning` to one sentence explaining your action choice.
"""


def build_messages(
    goal: str,
    observation: str,
    screenshot_b64: str,
    history: list[dict],
    parameters=None,
    outputs=None,
) -> list[dict]:
    contract_lines: list[str] = []
    if parameters:
        for p in parameters:
            contract_lines.append(f"  Parameter: {p.name} ({p.type}) — {p.description}")
    if outputs:
        for o in outputs:
            contract_lines.append(
                f'  Required output: {o.name} ({o.type}) — {o.description}'
                f'  → extract_text with output_name="{o.name}"'
            )
    contract_block = ("\n\nInvocation contract:\n" + "\n".join(contract_lines)) if contract_lines else ""

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    for entry in history:
        messages.append({"role": "assistant", "content": None, "tool_calls": [entry["call"]]})
        messages.append({
            "role": "tool",
            "tool_call_id": entry["call"]["id"],
            "content": entry["result"],
        })

    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": f"Goal: {goal}{contract_block}\n\nCurrent page state:\n{observation}"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}", "detail": "high"}},
        ],
    })
    return messages
