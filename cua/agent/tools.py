# OpenAI function definitions for the discovery agent.
# extract_text supports xpath_selector/css_selector for scoped targeting
# (e.g. a balance cell within a specific table row).

from __future__ import annotations

AGENT_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigate the browser to a URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["url", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click an element. Identify it by ARIA role+name from the accessibility tree.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "What you are clicking and why."},
                    "aria_role": {"type": "string"},
                    "aria_name": {"type": "string"},
                    "text_fallback": {"type": "string", "description": "Visible text if ARIA is unavailable."},
                    "reasoning": {"type": "string"},
                },
                "required": ["description", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Clear a field and type text into it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "aria_role": {"type": "string"},
                    "aria_name": {"type": "string"},
                    "placeholder_fallback": {"type": "string"},
                    "text": {"type": "string", "description": "Text to type."},
                    "reasoning": {"type": "string"},
                },
                "required": ["description", "text", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_option",
            "description": "Select an option from a dropdown.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "aria_role": {"type": "string"},
                    "aria_name": {"type": "string"},
                    "option_text": {"type": "string", "description": "Visible text of the option."},
                    "reasoning": {"type": "string"},
                },
                "required": ["description", "option_text", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_text",
            "description": (
                "Read text from an element and bind it to a declared output. "
                "For scoped extraction (e.g. a cell within a specific table row), "
                "use xpath_selector or css_selector instead of role/label targeting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "aria_role": {"type": "string"},
                    "aria_name": {"type": "string"},
                    "text_fallback": {"type": "string"},
                    "xpath_selector": {
                        "type": "string",
                        "description": (
                            "XPath for scoped targeting, e.g. "
                            "//tr[td[normalize-space()='Savings']]/td[@class='balance-cell']"
                        ),
                    },
                    "css_selector": {
                        "type": "string",
                        "description": "CSS selector for scoped targeting, e.g. tr:has-text('Savings') .balance-cell",
                    },
                    "output_name": {
                        "type": "string",
                        "description": "The output_name key from the invocation contract to bind this value to.",
                    },
                    "reasoning": {"type": "string"},
                },
                "required": ["description", "output_name", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_for_text",
            "description": "Wait for specific text to appear on the page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["text", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete",
            "description": "Signal goal accomplished. Call only when ALL declared outputs are in extracted_data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "One sentence on what was accomplished."},
                    "extracted_data": {
                        "type": "object",
                        "description": "All extracted output values, keyed by output_name.",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["summary", "extracted_data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate",
            "description": "Signal that you cannot proceed safely — need human intervention.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "current_state": {"type": "string", "description": "What the page currently shows."},
                },
                "required": ["reason", "current_state"],
            },
        },
    },
]
