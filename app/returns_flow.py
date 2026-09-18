"""
Deterministic returns/refunds workflow.

Every other in-scope category runs through app/agent.py's flexible tool
loop, where Claude decides what to call next. Returns don't: agent.py
routes returns_refunds straight to advance_return_flow below instead --
a fixed state machine, because this is the one flow that touches
identity, a fixed eligibility rule, and eventually money. Order IDs and
emails are fixed formats (regex), and matching a reply to one of an
order's line items is a short substring/ordinal match, not real natural-
language understanding -- see _extract_order_id, _extract_email, and
_match_item.

The one genuinely open-ended piece is the reason for the return:
_summarize_reason asks a small, fast model for a one-sentence summary of
whatever the customer said. The model crafts that one argument; identity,
eligibility, and every other order/item/email value stay code-computed.

Tradeoff: a customer who phrases something the regex/matching doesn't
expect gets re-asked instead of understood. Worth it for three short,
well-known flows over a fixed set of orders and titles.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anthropic

from . import tools

if TYPE_CHECKING:
    from .agent import Session

_ORDER_ID_RE = re.compile(r"\bBK-\d{3,6}\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# The one model call this module makes -- see _summarize_reason. A narrow,
# single-turn judgment, so it gets the same small/fast default as
# intent_router.py's ROUTER_MODEL rather than the full agent model.
REASON_MODEL = os.environ.get("ANTHROPIC_REASON_MODEL", "claude-haiku-4-5-20251001")
REASON_MAX_TOKENS = 150
MAX_REASON_CONTEXT_MESSAGES = 6

_REASON_TOOL_NAME = "summarize_return_reason"
_REASON_TOOL_SCHEMA = {
    "name": _REASON_TOOL_NAME,
    "description": (
        "Record a one-sentence, factual summary of why the customer wants to return this item."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "A short, one-sentence summary of the reason the customer actually gave. "
                    "If they never gave one, say so plainly (e.g. 'No reason given.') rather "
                    "than inventing one."
                ),
            }
        },
        "required": ["reason"],
    },
}

_REASON_SYSTEM_PROMPT = """You write a short internal note summarizing why a customer wants to return an item at Bookly, an online bookstore -- you are not talking to the customer, and nothing you write is seen by them.

Read the conversation and record one short, factual sentence describing the reason they gave for the return. Use only what they actually said -- never invent a defect, a complaint, or a reason they didn't mention. If they didn't give any reason at all, say plainly that no reason was given, rather than guessing one."""

# Separate lazy client from app/agent.py's own -- keeps this module
# importable without ANTHROPIC_API_KEY, and advance_return_flow's
# signature stable regardless of whether a turn reaches Step 4.
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


# Ordinal fallback ("the second one") when a reply doesn't name the title
# directly -- this app never has more than two items on one order.
_ORDINAL_WORDS = {
    "first": 0, "1st": 0, "one": 0,
    "second": 1, "2nd": 1, "other": 1,
    "third": 2, "3rd": 2,
}

# Denylist, not an allowlist -- only a clear decline turns down the label
# offer; anything else (including "how do I send it back?") counts as yes.
_LABEL_DECLINE_WORDS = ("no", "nope", "nah", "not now", "later", "skip", "don't", "no thanks")


@dataclass
class ReturnFlow:
    """Per-session state for one return. A plain bag of fields, not a
    stage enum -- advance_return_flow infers what's next from which
    fields are still None. Reset to None once a return is filed (and the
    label question answered) or declined outright."""

    order_id: str | None = None
    email: str | None = None
    verified_order: dict[str, Any] | None = None  # cached get_order_status result
    order_item_id: str | None = None
    reason: str | None = None
    filed_return_id: str | None = None
    label_offered: bool = False


def _extract_order_id(text: str) -> str | None:
    """Bookly order IDs are a fixed format (BK-####) -- a plain regex, not
    an interpretation task."""
    match = _ORDER_ID_RE.search(text)
    return match.group(0).upper() if match else None


def _extract_email(text: str) -> str | None:
    match = _EMAIL_RE.search(text)
    return match.group(0) if match else None


def looks_like_a_continuation(session: "Session", user_text: str) -> bool:
    """True if user_text supplies an order ID or email that this
    session's in-progress return is still missing.

    Used by app/agent.py's run_turn as a cheap override, checked before
    classify_intent runs: a message that structurally matches what an
    active ReturnFlow is waiting on shouldn't need a model's opinion on
    its category. Deliberately narrow -- only the order-ID/email step
    gets this treatment; later steps (item, label) still go through
    normal classification.
    """
    flow = session.return_flow
    if flow is None:
        return False
    if flow.order_id is None and _extract_order_id(user_text):
        return True
    if flow.email is None and _extract_email(user_text):
        return True
    return False


def _match_item(text: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Best-effort match of a reply to one of an order's line items: an
    exact title substring first, then an ordinal word ("the second one").
    Returns None on no match or a tie, so the caller re-asks instead of
    guessing."""
    lowered = text.lower()

    title_matches = [item for item in items if item["book_title"].lower() in lowered]
    if len(title_matches) == 1:
        return title_matches[0]

    for word in lowered.replace(",", " ").split():
        idx = _ORDINAL_WORDS.get(word)
        if idx is not None and idx < len(items):
            return items[idx]

    return None


def _wants_label(text: str) -> bool:
    lowered = text.lower()
    return not any(decline in lowered for decline in _LABEL_DECLINE_WORDS)


def _flatten_text(content: Any) -> str | None:
    """Pull human-readable text out of a message's content, skipping
    tool_use/tool_result blocks. A small copy of intent_router.py's own
    helper -- not worth importing across for a few lines of logic."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        block_type = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if block_type != "text":
            continue
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if text:
            parts.append(text)
    joined = " ".join(parts)
    return joined or None


def _conversation_text(messages: list[dict[str, Any]]) -> str:
    lines = []
    for msg in messages[-MAX_REASON_CONTEXT_MESSAGES:]:
        text = _flatten_text(msg.get("content"))
        if not text:
            continue
        role = "Customer" if msg.get("role") == "user" else "Agent"
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _summarize_reason(session: "Session") -> str:
    """Ask REASON_MODEL for a one-sentence, honest summary of the return
    reason -- the one place this module leans on a model. Reads
    session.messages for context, since the reason may have been stated
    turns before the order ID/email were. Failures here are handled by
    the caller (Step 4 below), not this function."""
    context = _conversation_text(session.messages)
    prompt = (
        f"Conversation so far:\n{context}\n\nSummarize the customer's reason for this return."
        if context
        else "The customer gave no other context. Summarize their reason for this return."
    )

    response = _get_client().messages.create(
        model=REASON_MODEL,
        max_tokens=REASON_MAX_TOKENS,
        system=_REASON_SYSTEM_PROMPT,
        tools=[_REASON_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": _REASON_TOOL_NAME},
        messages=[{"role": "user", "content": prompt}],
    )

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == _REASON_TOOL_NAME:
            reason = block.input.get("reason")
            if reason:
                return reason

    return "No reason given."


def _call_tool(name: str, tool_input: dict[str, Any], session: "Session") -> dict[str, Any]:
    """Local mirror of app/agent.py's _execute_tool -- same error handling,
    verified_email capture, and tool_calls logging, kept separate so this
    module never needs to import app.agent."""
    impl = tools.TOOL_IMPLEMENTATIONS[name]
    try:
        result = impl(**tool_input)
    except Exception as exc:
        result = {"error": "tool_execution_error", "message": str(exc)}
    else:
        email = tool_input.get("email")
        if email and "error" not in result:
            session.verified_email = email.strip().lower()

    session.tool_calls.append({"name": name, "input": tool_input, "result": result})
    return result


def advance_return_flow(session: "Session", user_text: str) -> str:
    """Run one turn of the returns_refunds flow to completion and return
    the reply text. app/agent.py has already appended the customer's
    message to session.messages before calling this."""
    flow = session.return_flow
    if flow is None:
        flow = ReturnFlow()
        session.return_flow = flow

    def _finish(reply: str, *, reset: bool = False) -> str:
        session.messages.append({"role": "assistant", "content": reply})
        if reset:
            session.return_flow = None
        return reply

    # Step 1: collect order ID + email -- fixed formats, regex, no model call.
    flow.order_id = flow.order_id or _extract_order_id(user_text)
    flow.email = flow.email or _extract_email(user_text)

    if not flow.order_id or not flow.email:
        missing = [
            label
            for label, have in (("the order ID", flow.order_id), ("the email on the account", flow.email))
            if not have
        ]
        return _finish(f"Sure -- could you share {' and '.join(missing)}?")

    # Step 2: verify identity + fetch items.
    if flow.verified_order is None:
        result = _call_tool("get_order_status", {"order_id": flow.order_id, "email": flow.email}, session)
        if "error" in result:
            if result["error"] == "identity_mismatch":
                flow.order_id = None
                flow.email = None
                return _finish(
                    "I couldn't verify that email against that order, so I can't pull up its details. "
                    "Could you double-check the order ID and the email on the account?"
                )
            flow.order_id = None
            return _finish(f"{result['message']} Could you double-check the order ID?")
        flow.verified_order = result

    items = flow.verified_order["items"]

    # Step 3: which item, if the order has more than one and none is chosen yet.
    if flow.order_item_id is None:
        if len(items) == 1:
            flow.order_item_id = items[0]["order_item_id"]
        else:
            match = _match_item(user_text, items)
            if match is None:
                titles = ", ".join(item["book_title"] for item in items)
                return _finish(f"That order has {len(items)} books -- {titles}. Which one would you like to return?")
            flow.order_item_id = match["order_item_id"]

    # Step 4: reason -- the one model-backed step. A failed summarization
    # falls back plainly rather than blocking a real return.
    if flow.reason is None:
        try:
            flow.reason = _summarize_reason(session)
        except Exception:
            flow.reason = "Customer-requested return (via chat) -- reason unavailable."

    # Step 5: file the return -- the 30-day eligibility check is enforced
    # server-side in tools.py, not reasoned about here or by a model.
    if flow.filed_return_id is None:
        result = _call_tool(
            "initiate_return",
            {
                "order_id": flow.order_id,
                "order_item_id": flow.order_item_id,
                "email": flow.email,
                "reason": flow.reason,
            },
            session,
        )
        if "error" in result:
            return _finish(
                f"{result['message']} I'll note this for a human agent to review if you'd like -- "
                "just let me know.",
                reset=True,
            )
        flow.filed_return_id = result["return_id"]
        return _finish(
            f"Done -- I've filed a return for \"{result['book_title']}\" (return {result['return_id']}), "
            f"estimated refund ${result['estimated_refund']:.2f}. Want a prepaid shipping label to send it back?"
        )

    # Step 6: the shipping label, offered once per return.
    if not flow.label_offered:
        flow.label_offered = True
        if not _wants_label(user_text):
            return _finish("No problem -- anything else I can help with?", reset=True)
        result = _call_tool(
            "generate_return_label",
            {"order_id": flow.order_id, "order_item_id": flow.order_item_id, "email": flow.email},
            session,
        )
        if "error" in result:
            return _finish(result["message"], reset=True)
        return _finish(
            f"Here's your label: {result['carrier']} tracking {result['tracking_number']}, drop off at "
            f"{result['drop_off']}. {result['refund_note']}",
            reset=True,
        )

    return _finish("Anything else I can help with?", reset=True)
