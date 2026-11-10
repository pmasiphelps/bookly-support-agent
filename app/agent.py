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
import re
import sys
from dataclasses import dataclass, field
from typing import Any

import anthropic

from . import telemetry
from . import tools as tools_module
from .agent_config import DEFAULT_AGENT_MODEL, DEFAULT_MAX_TOOL_ITERATIONS, USE_AOP_TOOL, AgentConfig
from .intent_router import (
    CLARIFYING_REPLY,
    CLARIFYING_SUGGESTIONS,
    classify_intent,
)
from .returns_flow import ReturnFlow, advance_return_flow, looks_like_a_continuation
from .tools import TOOL_IMPLEMENTATIONS

# Defaults only -- the prompts, models, tools, and limits a turn actually
# uses come from the Session's AgentConfig (app/agent_config.py), which
# the Agent Studio versions and deploys.
MODEL = DEFAULT_AGENT_MODEL
MAX_TOKENS = 1024

# Hard cap on tool round-trips per turn, so a confused model can't loop
# forever burning API calls. Per-config override: max_tool_iterations.
MAX_TOOL_ITERATIONS = DEFAULT_MAX_TOOL_ITERATIONS

# Models that rejected a mid-conversation {"role": "system"} message; for
# these, session context goes back to riding on the top-level system
# prompt (safe there: they don't bind thinking blocks to the prompt).
_NO_SYSTEM_MESSAGE_MODELS: set[str] = set()

# The deterministic returns flow, as it appears in the AOP audit trail
# (and in the Studio as a locked, coded AOP).
RETURNS_FLOW_AOP = "Returns & refunds (coded)"

ESCALATION_REPLY = (
    "Sorry, I'm having trouble finishing that request. "
    "I'll flag this conversation for a human agent to pick up."
)

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
    fields. `verified_email` and `verified_order_id` remember the latest
    tool-verified account/order pair across topic changes. `return_flow`
    is returns_flow.py's state for an in-progress return, or None.
    `tool_calls` is reset at the start of
    every turn and collects every real tool call made during it (see
    _execute_tool below and returns_flow._call_tool), for the frontend's
    "dev: show tool calls" debug view. `config` is the AgentConfig this
    conversation is pinned to for its whole life -- a deploy mid-
    conversation never changes the rules under a customer's feet."""

    session_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    config: AgentConfig = field(default_factory=AgentConfig)
    version_id: int | None = None  # Studio version behind `config`, if any
    verified_email: str | None = None
    verified_order_id: str | None = None
    return_flow: ReturnFlow | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Last session context sent to the model as a system message -- only a
    # change is re-sent, so the transcript stays append-only.
    context_sent: str | None = None
    # "chat" or "email" -- picks AOP channel variants; fixed per session.
    channel: str = "chat"
    # Facts about the verified customer, for {{ metadata }} in AOPs.
    customer_metadata: dict[str, Any] = field(default_factory=dict)
    # Studio Preview's "test metadata" -- wins over looked-up values.
    metadata_overrides: dict[str, Any] = field(default_factory=dict)
    escalated_by_tool: bool = False
    # Orders ruled ineligible for return in this conversation (by the coded
    # flow or by initiate_return called from the agent loop).
    ineligible_return_orders: set[str] = field(default_factory=set)
    # Forced AOPs (see AgentConfig.forced_aop): ids already loaded into
    # this conversation, notes waiting to be sent with the next model call,
    # and every note sent so far (for models without system messages).
    forced_loaded: set[str] = field(default_factory=set)
    # Every AOP loaded in this conversation, forced or via use_aop -- what
    # unlocks AOP-scoped tools (AgentConfig.aop_scoped_tools).
    loaded_aops: set[str] = field(default_factory=set)
    pending_notes: list[str] = field(default_factory=list)
    forced_notes: list[str] = field(default_factory=list)

    def note_ineligible_return(self, tool_name: str, tool_input: dict[str, Any], result: dict[str, Any]) -> None:
        """Remember an order ruled ineligible for return -- whichever tool
        showed it -- so escalate_to_human can't be used to route around the
        rule (see _blocked_escalation). The model may skip initiate_return
        once get_order_status already shows the window has closed."""
        order_id = str(tool_input.get("order_id", "")).strip().upper()
        if tool_name == "initiate_return" and result.get("error") == "not_eligible":
            self.ineligible_return_orders.add(order_id)
        elif (tool_name == "get_order_status" and result.get("status") == "delivered"
              and result.get("return_eligible") is False):
            self.ineligible_return_orders.add(order_id)

    def metadata(self) -> dict[str, Any]:
        merged = {"channel": self.channel}
        if self.verified_email:
            merged["email"] = self.verified_email
        merged.update(self.customer_metadata)
        merged.update({k: v for k, v in self.metadata_overrides.items() if v not in (None, "")})
        return merged

    def remember_verified_context(self, tool_input: dict[str, Any]) -> None:
        """Called only after a successful identity-checking tool result.

        Keep the order paired with its email; changing accounts must never
        carry over an order from the previous account.
        """
        email = tool_input.get("email")
        if not email:
            return
        email = email.strip().lower()
        if email != self.verified_email:
            self.verified_order_id = None
            try:
                self.customer_metadata = tools_module.customer_metadata(email)
            except Exception:  # metadata is a nicety; never fail a turn over it
                self.customer_metadata = {}
        self.verified_email = email
        if tool_input.get("order_id"):
            self.verified_order_id = tool_input["order_id"].strip().upper()


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
    # Observability: what the scope gate decided and which path handled
    # the turn ("declined" | "clarify" | "returns_flow" | "agent_loop").
    category: str | None = None
    route: str | None = None
    escalated: bool = False
    # Audit trail: the AOPs that shaped this reply, in the order loaded.
    aops: list[str] = field(default_factory=list)


def get_session(session_id: str, config_factory=None) -> Session:
    """Fetch or create a session. `config_factory` is called only on
    creation and returns (AgentConfig, version_id) -- how a new
    conversation gets pinned to whatever version is deployed for it."""
    session = _sessions.get(session_id)
    if session is None:
        session = Session(session_id=session_id)
        if config_factory is not None:
            session.config, session.version_id = config_factory(session_id)
        _sessions[session_id] = session
    return session


def _dispatch(session: Session, category: str, user_text: str) -> TurnResult:
    """Handle a category once it's known -- from classify_intent
    (run_turn) or a quick-reply button (run_turn_for_category)."""
    config = session.config
    if category == "out_of_scope":
        session.messages.append({"role": "assistant", "content": config.out_of_scope_reply})
        return TurnResult(reply=config.out_of_scope_reply, category=category, route="declined")

    forced = config.forced_aop(category)
    if category == "returns_refunds" and forced is None:
        # No AOP owns returns in this config: the coded state machine does.
        return TurnResult(
            reply=advance_return_flow(session, user_text), category=category, route="returns_flow",
            aops=[RETURNS_FLOW_AOP],
        )

    forced_names = [forced.name] if forced else []
    if forced and forced.id not in session.forced_loaded:
        _load_forced_aop(session, forced)

    for _ in range(config.max_tool_iterations):
        response = _call_model(session)

        session.messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return TurnResult(
                reply=_extract_text(response.content), category=category, route="agent_loop", aops=forced_names,
            )

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

    session.messages.append({"role": "assistant", "content": ESCALATION_REPLY})
    return TurnResult(
        reply=ESCALATION_REPLY, category=category, route="agent_loop", escalated=True, aops=forced_names,
    )


FORCED_AOP_NOTE = (
    "This message was routed to a forced AOP. Follow it now -- it's already loaded, so you don't "
    "need to call use_aop for it.\n"
)


def _load_forced_aop(session: Session, aop) -> None:
    """Deliver a forced AOP's steps with the next model call (as an operator
    system message, like session context), and record it as a use_aop call
    so the audit trail shows it shaped the reply."""
    steps = session.config.render_aop(aop.name, session.channel)
    note = FORCED_AOP_NOTE + steps
    session.pending_notes.append(note)
    session.forced_notes.append(note)
    session.forced_loaded.add(aop.id)
    session.loaded_aops.add(aop.name)
    session.tool_calls.append({
        "name": USE_AOP_TOOL,
        "input": {"name": aop.name, "forced": True},
        "result": {"aop": aop.name, "steps": steps, "forced": True},
    })


def run_turn(session: Session, user_text: str) -> TurnResult:
    """Run one user turn to completion, including any tool round-trips."""
    session.tool_calls = []
    session.escalated_by_tool = False

    if looks_like_a_continuation(session, user_text):
        # An in-progress return is still missing exactly what this message
        # supplies (an order ID or email) -- skip classification entirely.
        session.messages.append({"role": "user", "content": user_text})
        reply = advance_return_flow(session, user_text)
        return TurnResult(
            reply=reply, tool_calls=session.tool_calls, category="returns_refunds", route="returns_flow",
            aops=[RETURNS_FLOW_AOP],
        )

    category = classify_intent(
        _get_client(),
        session.messages,
        user_text,
        system_prompt=session.config.router_prompt,
        model=session.config.router_model,
    )
    session.messages.append({"role": "user", "content": user_text})

    if category == "unclear":
        session.messages.append({"role": "assistant", "content": CLARIFYING_REPLY})
        return TurnResult(
            reply=CLARIFYING_REPLY, suggestions=CLARIFYING_SUGGESTIONS, category=category, route="clarify"
        )

    return _finish_turn(session, _dispatch(session, category, user_text))


def run_turn_for_category(session: Session, user_text: str, category: str) -> TurnResult:
    """Run a turn whose category is already known -- used when a customer
    clicks a CLARIFYING_SUGGESTIONS button, skipping classify_intent since
    the click already answered the question. `category` is one of
    IN_SCOPE_CATEGORIES or "out_of_scope", never "unclear"."""
    session.tool_calls = []
    session.escalated_by_tool = False
    session.messages.append({"role": "user", "content": user_text})
    return _finish_turn(session, _dispatch(session, category, user_text))


def _finish_turn(session: Session, result: TurnResult) -> TurnResult:
    result.tool_calls = session.tool_calls
    loaded = [c["input"].get("name") for c in session.tool_calls
              if c["name"] == USE_AOP_TOOL and "error" not in c["result"]]
    result.aops = list(dict.fromkeys(result.aops + loaded))
    result.escalated = result.escalated or session.escalated_by_tool
    return result


def _session_context(session: Session) -> str | None:
    """None until there's something worth telling the model."""
    meta = {k: v for k, v in session.metadata().items() if k != "channel"}
    if not session.verified_email and not meta:
        return None
    return "Verified session context: " + json.dumps({
        "verified_email": session.verified_email,
        "last_verified_order_id": session.verified_order_id,
        "customer_metadata": meta,
    }, sort_keys=True)


def _call_model(session: Session):
    """One agent-loop model call.

    The top-level system prompt never changes within a conversation:
    current models bind thinking blocks to the exact system prompt they
    were produced under, so editing it mid-conversation (e.g. once a tool
    verifies the customer) makes the next call fail with a 400. Session
    context is appended to the transcript as a {"role": "system"} message
    instead, only when it changes -- an operator-authority channel a
    customer can't spoof by typing "verified_email: ..." into the chat.
    """
    config = session.config
    context = _session_context(session)
    legacy = config.agent_model in _NO_SYSTEM_MESSAGE_MODELS
    # One system message per call at most: a system message must directly
    # follow a user turn, so context and forced-AOP notes travel together.
    notes = []
    if context and context != session.context_sent:
        notes.append(context)
        session.context_sent = context
    notes += session.pending_notes
    session.pending_notes = []
    if not legacy and notes:
        session.messages.append({"role": "system", "content": "\n\n".join(notes)})

    base_system = config.compiled_system_prompt(session.channel)
    # Models without mid-conversation system messages get it all in the prompt.
    legacy_extra = "\n\n".join(([context] if context else []) + session.forced_notes)
    kwargs = dict(
        model=config.agent_model,
        max_tokens=MAX_TOKENS,
        system=base_system + ("\n" + legacy_extra if legacy and legacy_extra else ""),
        tools=config.loop_tools(),
    )
    try:
        return telemetry.create_message(_get_client(), "agent", messages=_api_messages(session.messages, legacy), **kwargs)
    except anthropic.BadRequestError as exc:
        unsupported = "system" in str(exc) and "not supported" in str(exc)
        if legacy or not unsupported or not any(m["role"] == "system" for m in session.messages):
            raise
        print(f"[agent] {config.agent_model} rejected a system message; using the system prompt instead", file=sys.stderr)
        _NO_SYSTEM_MESSAGE_MODELS.add(config.agent_model)
        kwargs["system"] = base_system + "\n" + legacy_extra
        return telemetry.create_message(_get_client(), "agent", messages=_api_messages(session.messages, True), **kwargs)


# Words that mark an escalation as being about a return. A heuristic, but
# it only has to catch the one case below, and a false positive costs a
# customer one clarifying turn, not a policy breach.
_RETURN_WORDS = re.compile(r"\b(return|refund|exception|store credit|eligib|window)\w*", re.I)


def _scope_refusal(session: Session, name: str) -> dict[str, Any] | None:
    """Per-AOP tool scoping: an AOP-scoped tool works only once an AOP that
    references it has been loaded in this conversation. Enforced here, in
    code, so the model can't take an action while skipping the procedure
    that governs it."""
    config = session.config
    if name not in config.aop_scoped_tools:
        return None
    users = config.aops_using_tool(name)
    if any(u in session.loaded_aops for u in users):
        return None
    if not users:
        return {"error": "aop_required", "message": f"{name} is only available inside an AOP, and no AOP uses it."}
    return {
        "error": "aop_required",
        "message": f"{name} can only be used while following an AOP that uses it. Load "
                   + " or ".join(f'"{u}"' for u in users) + f" with {USE_AOP_TOOL} first, then follow its steps.",
    }


def _blocked_escalation(session: Session, tool_input: dict[str, Any]) -> dict[str, Any] | None:
    """Code-level guardrail: once the returns flow has ruled a return
    ineligible, an escalation can't be used to get around that rule -- no
    matter what an AOP or the model decides. (The system prompt says the
    same; this is what actually enforces it.)"""
    if not session.ineligible_return_orders:
        return None
    reason = str(tool_input.get("reason", ""))
    about_it = _RETURN_WORDS.search(reason) or any(o in reason.upper() for o in session.ineligible_return_orders)
    if not about_it:
        return None
    orders = ", ".join(sorted(session.ineligible_return_orders))
    return {
        "error": "escalation_not_allowed",
        "message": (
            f"Not escalated: the return for {orders} is outside Bookly's return window, and the support "
            "team can't make exceptions to it either. Explain that plainly and kindly, without promising "
            "a review, and offer help with anything else."
        ),
    }


def _api_messages(messages: list[dict[str, Any]], drop_system: bool) -> list[dict[str, Any]]:
    if not drop_system:
        return messages
    return [m for m in messages if m["role"] != "system"]


def _execute_tool(name: str, tool_input: dict[str, Any], session: Session) -> Any:
    if name == USE_AOP_TOOL:
        steps = session.config.render_aop(str(tool_input.get("name", "")), session.channel)
        result = ({"aop": tool_input.get("name"), "steps": steps} if steps
                  else {"error": "unknown_aop", "message": f"No active AOP named {tool_input.get('name')!r}"})
        if steps:
            session.loaded_aops.update(a.name for a in session.config.active_aops()
                                       if a.name.lower() == str(tool_input.get("name", "")).strip().lower())
        session.tool_calls.append({"name": name, "input": tool_input, "result": result})
        return result

    refusal = _scope_refusal(session, name)
    if refusal:
        session.tool_calls.append({"name": name, "input": tool_input, "result": refusal})
        return refusal

    if name == "escalate_to_human":
        blocked = _blocked_escalation(session, tool_input)
        if blocked:
            session.tool_calls.append({"name": name, "input": tool_input, "result": blocked})
            return blocked

    impl = TOOL_IMPLEMENTATIONS.get(name)
    if name not in session.config.enabled_tools:
        # The model was never offered it, but don't trust that alone.
        impl = None
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
            if email and "error" not in result and name != "escalate_to_human":
                # escalate_to_human takes an email but doesn't verify it.
                session.remember_verified_context(tool_input)
            if name == "escalate_to_human" and result.get("success"):
                session.escalated_by_tool = True
            session.note_ineligible_return(name, tool_input, result)

    session.tool_calls.append({"name": name, "input": tool_input, "result": result})
    return result


def _extract_text(content_blocks) -> str:
    return "".join(block.text for block in content_blocks if block.type == "text").strip()
