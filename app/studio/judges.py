"""
Model-backed evaluation pieces: a rubric judge for test cases, a
simulated customer for simulation tests and synthetic traffic, and the
QA reviewer that scores logged conversations in Monitor.

Structured output comes from `output_config.format` (JSON schema), not a
forced tool call: current models (Opus 5.5, Sonnet 5.5, Fable 5.1) reject
forced tool_choice, and the judge defaults to the agent's model. Each has
its own lazy client, so tests can swap it.
None of them run inside a turn's telemetry trace -- evaluation cost is
not agent cost.
"""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic

from ..agent_config import DEFAULT_AGENT_MODEL

JUDGE_MODEL = os.environ.get("BOOKLY_JUDGE_MODEL", DEFAULT_AGENT_MODEL)
SIMULATOR_MODEL = os.environ.get("BOOKLY_SIMULATOR_MODEL", DEFAULT_AGENT_MODEL)

QA_FLAGS = [
    "hallucination",        # stated an order/policy fact no tool returned
    "policy_violation",     # e.g. promised an exception, skipped identity check
    "unresolved",           # customer's need wasn't met and wasn't clearly declined
    "customer_frustration",
    "wrong_decline",        # in-scope request turned away
    "repetition",           # asked for the same info twice, looped
    "tone",
]

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


# Generous: thinking is always on for current models and counts against it.
JUDGE_MAX_TOKENS = 4096


def _structured_call(system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    response = _get_client().messages.create(
        model=JUDGE_MODEL,
        max_tokens=JUDGE_MAX_TOKENS,
        system=system,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
    if response.stop_reason != "end_turn" or not text:
        raise RuntimeError(f"Judge gave no usable answer (stop_reason={response.stop_reason})")
    return json.loads(text)


def transcript_text(turns: list[dict[str, Any]], max_result_chars: int = 500) -> str:
    """turns: [{"user", "reply", "tool_calls"?, "category"?}] -> plain text
    a judge can read, including what the tools actually returned (so it
    can tell a grounded answer from a made-up one)."""
    lines = []
    for t in turns:
        lines.append(f"Customer: {t['user']}")
        for call in t.get("tool_calls") or []:
            result = json.dumps(call.get("result"), default=str)
            if len(result) > max_result_chars:
                result = result[:max_result_chars] + "...(truncated)"
            lines.append(f"  [tool] {call['name']}({json.dumps(call.get('input'))}) -> {result}")
        lines.append(f"Agent: {t['reply']}")
    return "\n".join(lines)


# -- rubric judge --------------------------------------------------------------

_GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string", "description": "2-4 sentences citing the transcript."},
        "passed": {"type": "boolean"},
    },
    "required": ["reasoning", "passed"],
    "additionalProperties": False,
}

_GRADE_SYSTEM = """You grade a customer-support agent's conversation against a single pass/fail \
criterion written by the support team. Judge only the criterion -- not style you'd prefer, not \
other issues. Lines marked [tool] are real backend calls and their real results; the customer \
never sees them. Be strict but literal: pass if the criterion is met, fail if it isn't."""


def grade(turns: list[dict[str, Any]], criteria: str) -> dict[str, Any]:
    prompt = f"Criterion:\n{criteria}\n\nTranscript:\n{transcript_text(turns)}"
    out = _structured_call(_GRADE_SYSTEM, prompt, _GRADE_SCHEMA)
    return {"passed": bool(out.get("passed")), "reasoning": out.get("reasoning", "")}


# -- simulated customer -----------------------------------------------------------

END_TOKEN = "[END]"

_SIM_SYSTEM = """You are role-playing a CUSTOMER chatting with Bookly's online support widget \
(Bookly is an online bookstore). You are testing the support agent, but you must behave like a \
real customer: write short, natural chat messages (usually one sentence), only reveal details \
(order ID, email) when asked or when it's natural, and never mention that this is a test or \
simulation.

Persona: {persona}
Your goal: {goal}
Details you know (share only as needed): {details}

If the agent offers buttons, you may reply with a button's exact label to click it.
When your goal is accomplished, or the agent has clearly and finally told you it can't be done, \
or you're going in circles, reply with exactly {end} and nothing else."""


def simulate_customer_message(
    persona: str, goal: str, details: str, history: list[dict[str, str]]
) -> str:
    """history: [{"customer": str, "agent": str}, ...] -- returns the next
    customer message, or END_TOKEN."""
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "(The Bookly support chat window just opened. Send your first message.)"}
    ]
    for h in history:
        messages.append({"role": "assistant", "content": h["customer"]})
        messages.append({"role": "user", "content": h["agent"]})
    response = _get_client().messages.create(
        model=SIMULATOR_MODEL,
        max_tokens=2048,  # room for always-on thinking before a one-line reply
        system=_SIM_SYSTEM.format(persona=persona, goal=goal, details=details or "none", end=END_TOKEN),
        messages=messages,
    )
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    if not text or END_TOKEN in text:
        return END_TOKEN
    return text.strip('"')


# -- conversation QA review --------------------------------------------------------

# Topic tags for Convos (Decagon-style "Card Issues"); fixed so they can be
# counted and filtered.
TOPICS = [
    "Order status", "Returns", "Refunds", "Shipping", "Payments", "Account access",
    "Damaged item", "Lost package", "Escalation request", "Off-topic", "Other",
]

_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "1-2 sentences: what the customer wanted and the relevant facts."},
        "resolution_summary": {"type": "string", "description": "1-2 sentences: what the agent did about it, and where it ended."},
        "topics": {"type": "array", "items": {"type": "string", "enum": TOPICS}, "description": "1-3 topic tags."},
        "resolution": {"type": "string", "enum": ["resolved", "partially_resolved", "unresolved", "declined_out_of_scope"]},
        "score": {"type": "integer", "description": "Overall quality from 1 to 5, 5 = excellent."},
        "flags": {"type": "array", "items": {"type": "string", "enum": QA_FLAGS}},
        "suggested_fix": {
            "type": "string",
            "description": "If anything went wrong: the concrete prompt/procedure change that would fix it. Empty if nothing.",
        },
    },
    "required": ["summary", "resolution_summary", "topics", "resolution", "score", "flags", "suggested_fix"],
    "additionalProperties": False,
}

_REVIEW_SYSTEM = """You are a QA reviewer for Bookly's AI support agent. Bookly's agent handles \
order status (details of a specific order require the order ID + the matching account email, \
verified by a tool; a customer without their order ID may have their recent orders listed from \
their email alone, which is intended), returns (30-day window from delivery, enforced by a tool), \
and general policy questions (answered only from a policy-search tool). It must decline anything \
else, must never state order/policy facts that no tool returned, and must not promise exceptions \
to the return policy.

Lines marked [tool] are real backend calls and results the customer never sees. Use them to check \
whether every factual claim the agent made is grounded. Flag only real problems; a short, correct, \
grounded conversation deserves a 5 with no flags."""


def review_conversation(turns: list[dict[str, Any]]) -> dict[str, Any]:
    out = _structured_call(_REVIEW_SYSTEM, f"Transcript:\n{transcript_text(turns)}", _REVIEW_SCHEMA)
    flags = [f for f in out.get("flags", []) if f in QA_FLAGS]
    return {
        "summary": out.get("summary", ""),
        "resolution_summary": out.get("resolution_summary", ""),
        "topics": [t for t in out.get("topics", []) if t in TOPICS][:3],
        "resolution": out.get("resolution", "unresolved"),
        "score": max(1, min(5, int(out.get("score", 3)))),
        "flags": flags,
        "suggested_fix": out.get("suggested_fix", ""),
        "model": JUDGE_MODEL,
    }


# -- watchtower -----------------------------------------------------------------------

_WATCH_SYSTEM = """You monitor customer-support conversations for Bookly, an online bookstore. \
For each monitor below, decide whether this conversation matches the monitor's criteria. Judge \
each monitor independently and literally against its own criteria. If a monitor lists labels and \
the conversation is flagged, pick the single best label; otherwise leave label empty. Keep each \
reason to one sentence that cites what happened. Lines marked [tool] are backend calls the \
customer never sees."""


def evaluate_watchtowers(turns: list[dict[str, Any]], monitors: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """One model call that checks a conversation against several
    Watchtower monitors at once. Returns {monitor_id: verdict}."""
    schema = {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "monitor_id": {"type": "integer"},
                        "flagged": {"type": "boolean"},
                        "label": {"type": "string", "description": "One of the monitor's labels, or empty."},
                        "reason": {"type": "string"},
                    },
                    "required": ["monitor_id", "flagged", "label", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }
    listing = "\n".join(
        f"- monitor_id {m['id']} \"{m['name']}\": {m['criteria']}"
        + (f" Labels: {', '.join(m['labels'])}." if m["labels"] else "")
        for m in monitors
    )
    out = _structured_call(_WATCH_SYSTEM, f"Monitors:\n{listing}\n\nTranscript:\n{transcript_text(turns)}", schema)
    wanted = {m["id"]: m for m in monitors}
    verdicts: dict[int, dict[str, Any]] = {}
    for r in out.get("results", []):
        m = wanted.get(r.get("monitor_id"))
        if m is None:
            continue
        flagged = bool(r.get("flagged"))
        label = (r.get("label") or "").strip() or None
        if not flagged or not m["labels"]:
            label = None
        elif label not in m["labels"]:
            label = m["labels"][-1]  # convention: the last label is the catch-all
        verdicts[m["id"]] = {"flagged": flagged, "label": label, "reason": r.get("reason", "")}
    return verdicts
