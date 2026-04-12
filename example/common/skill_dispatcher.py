"""
Simulated skill responses shared by every framework runner.

In a real system each skill would carry an invocation endpoint in its metadata
(or be wired to an MCP tool adapter). For demo purposes, we return hard-coded
fixtures so the examples run end-to-end without additional infrastructure.
"""

from __future__ import annotations


def _infer_intent(message: str) -> str:
    """Keyword-based intent classifier used by the `classify-intent` skill."""
    msg = message.lower()
    if any(w in msg for w in ("bill", "invoice", "charge", "payment", "credit")):
        return "billing"
    if any(w in msg for w in (
        "internet", "wifi", "connection", "slow", "outage",
        "signal", "technical", "not working",
    )):
        return "technical-support"
    if any(w in msg for w in ("account", "password", "login", "profile")):
        return "account"
    return "general"


def dispatch_skill(tool_name: str, tool_input: dict) -> dict:
    """
    Return a canned response for a skill invocation.

    `tool_name` is the skill id with hyphens converted to underscores, which
    is what every framework's tool calling layer emits (tool names usually
    must match [a-zA-Z0-9_]).
    """
    if tool_name == "classify_intent":
        return {
            "intent": _infer_intent(tool_input.get("message", "")),
            "confidence": 0.91,
        }
    if tool_name == "lookup_invoice":
        return {
            "invoices": [
                {"invoice_id": "INV-1234", "amount": 49.99,
                 "status": "paid", "date": "2026-03-15"},
                {"invoice_id": "INV-1198", "amount": 12.00,
                 "status": "pending", "date": "2026-04-01"},
            ]
        }
    if tool_name == "apply_credit":
        return {
            "success": True,
            "credit_applied": tool_input.get("amount", 0),
            "new_balance": 0.00,
        }
    if tool_name == "run_diagnostic":
        return {
            "status": "degraded",
            "issues": ["packet-loss-detected", "signal-below-threshold"],
            "recommendation": (
                "Reboot the modem; if issue persists, schedule a technician."
            ),
        }
    if tool_name == "schedule_technician":
        return {
            "confirmation_id": "TECH-20260415-001",
            "scheduled_date": tool_input.get("preferred_date", "TBD"),
            "technician": "Field Team B",
        }
    return {"result": "ok", "tool": tool_name}
