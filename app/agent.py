"""
Agent orchestration loop: a plain while-loop that calls Claude, runs
whatever tools it asks for, feeds the results back, and repeats until it
produces a final reply. Hand-rolled, not built on an agent framework.

Every message is classified first (see intent_router.py), so the agent
only ever sees requests it's built to handle. order_status, general_policy,
and small_talk go through the flexible loop below; returns_refunds is
routed to returns_flow.py's deterministic state machine instead, since
that's the one flow touching identity, eligibility, and money.

Memory is just the session transcript, replayed in full each turn --
nothing persists past this process.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .intent_router import (
    CLARIFYING_REPLY,
    CLARIFYING_SUGGESTIONS,
    OUT_OF_SCOPE_REPLY,
    classify_intent,
)
from .prompts import SYSTEM_PROMPT
from .returns_flow import ReturnFlow, advance_return_flow, looks_like_a_continuation
from .tools import FLEXIBLE_LOOP_TOOLS, TOOL_IMPLEMENTATIONS

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
MAX_TOKENS = 1024

# Hard cap on tool round-trips per turn, so a confused model can't loop
# forever burning API calls.
MAX_TOOL_ITERATIONS = 6

# Lazy so importing this module doesn't require ANTHROPIC_API_KEY. Tests
# monkeypatch _get_client() to inject a fake.
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


@dataclass
class Session:
    """Per-conversation state: the message transcript, plus a few derived
    fields. `verified_email` is set only once a tool call confirms an
    email against Snowflake. `return_flow` is returns_flow.py's state for
    an in-progress return, or None. `tool_calls` is reset at the start of
    every turn and collects every real tool call made during it (see
    _execute_tool below and returns_flow._call_tool), for the frontend's
    "dev: show tool calls" debug view."""

    session_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    verified_email: str | None = None
    return_flow: ReturnFlow | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


_sessions: dict[str, Session] = {}


@dataclass
class TurnResult:
    """`suggestions` is only set for the "unclear" outcome -- a list of
    {"label", "value", "category"} dicts for the frontend to render as
    buttons. `tool_calls` is every real tool call made this turn (name,
    input, result) -- always present, usually empty for turns that never
    touched a tool."""

    reply: str
    suggestions: list[dict[str, Any]] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def get_session(session_id: str) -> Session:
    session = _sessions.get(session_id)
    if session is None:
        session = Session(session_id=session_id)
        _sessions[session_id] = session
    return session


def _dispatch(session: Session, category: str, user_text: str) -> TurnResult:
    """Handle a category once it's known -- from classify_intent
    (run_turn) or a quick-reply button (run_turn_for_category)."""
    if category == "out_of_scope":
        session.messages.append({"role": "assistant", "content": OUT_OF_SCOPE_REPLY})
        return TurnResult(reply=OUT_OF_SCOPE_REPLY)

    if category == "returns_refunds":
        return TurnResult(reply=advance_return_flow(session, user_text))

    for _ in range(MAX_TOOL_ITERATIONS):
        response = _get_client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=FLEXIBLE_LOOP_TOOLS,
            messages=session.messages,
        )

        session.messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return TurnResult(reply=_extract_text(response.content))

        tool_results = [
            {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(_execute_tool(block.name, block.input, session)),
            }
            for block in response.content
            if block.type == "tool_use"
        ]
        session.messages.append({"role": "user", "content": tool_results})

    return TurnResult(
        reply=(
            "Sorry, I'm having trouble finishing that request. "
            "I'll flag this conversation for a human agent to pick up."
        )
    )


def run_turn(session: Session, user_text: str) -> TurnResult:
    """Run one user turn to completion, including any tool round-trips."""
    session.tool_calls = []

    if looks_like_a_continuation(session, user_text):
        # An in-progress return is still missing exactly what this message
        # supplies (an order ID or email) -- skip classification entirely.
        session.messages.append({"role": "user", "content": user_text})
        reply = advance_return_flow(session, user_text)
        return TurnResult(reply=reply, tool_calls=session.tool_calls)

    category = classify_intent(_get_client(), session.messages, user_text)
    session.messages.append({"role": "user", "content": user_text})

    if category == "unclear":
        session.messages.append({"role": "assistant", "content": CLARIFYING_REPLY})
        return TurnResult(reply=CLARIFYING_REPLY, suggestions=CLARIFYING_SUGGESTIONS)

    result = _dispatch(session, category, user_text)
    result.tool_calls = session.tool_calls
    return result


def run_turn_for_category(session: Session, user_text: str, category: str) -> TurnResult:
    """Run a turn whose category is already known -- used when a customer
    clicks a CLARIFYING_SUGGESTIONS button, skipping classify_intent since
    the click already answered the question. `category` is one of
    IN_SCOPE_CATEGORIES or "out_of_scope", never "unclear"."""
    session.tool_calls = []
    session.messages.append({"role": "user", "content": user_text})
    result = _dispatch(session, category, user_text)
    result.tool_calls = session.tool_calls
    return result


def _execute_tool(name: str, tool_input: dict[str, Any], session: Session) -> Any:
    impl = TOOL_IMPLEMENTATIONS.get(name)
    if impl is None:
        result = {"error": "unknown_tool", "message": f"No such tool: {name}"}
    else:
        try:
            result = impl(**tool_input)
        except Exception as exc:
            result = {"error": "tool_execution_error", "message": str(exc)}
        else:
            # A successful email-taking tool call just had that email
            # checked against Snowflake -- capture it as verified.
            email = tool_input.get("email")
            if email and "error" not in result:
                session.verified_email = email.strip().lower()

    session.tool_calls.append({"name": name, "input": tool_input, "result": result})
    return result


def _extract_text(content_blocks) -> str:
    return "".join(block.text for block in content_blocks if block.type == "text").strip()
