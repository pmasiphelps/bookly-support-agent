"""
Monitor: aggregate the conversation log into the metrics a support team
watches, list/inspect conversations, and run AI QA review over them.
"""

from __future__ import annotations

import datetime as dt
import random
from collections import Counter, defaultdict
from typing import Any

from ..agent_config import DEFAULT_AUTHOR
from . import judges
from .store import get_store

WINDOWS = {"1h": dt.timedelta(hours=1), "24h": dt.timedelta(hours=24), "7d": dt.timedelta(days=7),
           "30d": dt.timedelta(days=30), "all": None}


def since_for(window: str) -> str | None:
    delta = WINDOWS.get(window, WINDOWS["7d"])
    if delta is None:
        return None
    return (dt.datetime.now(dt.timezone.utc) - delta).isoformat(timespec="seconds")


def _pct(n: int, d: int) -> float | None:
    return round(100.0 * n / d, 1) if d else None


def _percentile(values: list[int], p: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, int(round(p / 100 * (len(ordered) - 1)))))
    return ordered[k]


def _tool_failed(call: dict[str, Any]) -> bool:
    result = call.get("result")
    # Expected business outcomes (not found, identity mismatch, ineligible)
    # are the tool working correctly; only execution failures count.
    return isinstance(result, dict) and result.get("error") in ("tool_execution_error", "unknown_tool")


def _conv_outcome(conv: dict[str, Any], turns: list[dict[str, Any]]) -> str:
    if conv["escalated"]:
        return "escalated"
    routes = {t["route"] for t in turns}
    if routes and routes <= {"declined", "clarify"}:
        return "declined"
    return "automated"


def summary(envs: list[str], window: str, version_id: int | None = None) -> dict[str, Any]:
    store = get_store()
    convs = store.query_conversations(envs, since_for(window), version_id)
    turns = store.query_turns([c["id"] for c in convs])
    turns_by_conv: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in turns:
        turns_by_conv[t["conversation_id"]].append(t)

    n = len(convs)
    outcomes = Counter(_conv_outcome(c, turns_by_conv[c["id"]]) for c in convs)
    rated = [c for c in convs if c["csat"] is not None]
    reviewed = [c for c in convs if c["qa"]]
    flagged = [c for c in reviewed if c["qa"]["flags"]]
    latencies = [t["latency_ms"] for t in turns]

    tool_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "failures": 0})
    for t in turns:
        for call in t["tool_calls"]:
            if call["name"] == "use_aop":  # AOP loading is bookkeeping, not an action
                continue
            tool_stats[call["name"]]["calls"] += 1
            tool_stats[call["name"]]["failures"] += int(_tool_failed(call))

    # Time series: hourly buckets for short windows, daily otherwise.
    hourly = window in ("1h", "24h")
    series: dict[str, dict[str, int]] = defaultdict(lambda: {"conversations": 0, "escalated": 0, "deflected": 0})
    for c in convs:
        key = c["started_at"][:13] + ":00" if hourly else c["started_at"][:10]
        series[key]["conversations"] += 1
        series[key]["escalated"] += c["escalated"]
        series[key]["deflected"] += int(_conv_outcome(c, turns_by_conv[c["id"]]) == "automated")

    by_version: dict[Any, dict[str, Any]] = {}
    for vid in sorted({c["version_id"] for c in convs}, key=lambda v: (v is None, v)):
        vc = [c for c in convs if c["version_id"] == vid]
        vt = [t for c in vc for t in turns_by_conv[c["id"]]]
        v_rated = [c for c in vc if c["csat"] is not None]
        v_rev = [c for c in vc if c["qa"]]
        by_version[vid] = {
            "version_id": vid,
            "conversations": len(vc),
            "automation_rate": _pct(sum(_conv_outcome(c, turns_by_conv[c["id"]]) == "automated" for c in vc), len(vc)),
            "escalation_rate": _pct(sum(c["escalated"] for c in vc), len(vc)),
            "csat": _pct(sum(c["csat"] > 0 for c in v_rated), len(v_rated)),
            "qa_score": round(sum(c["qa"]["score"] for c in v_rev) / len(v_rev), 2) if v_rev else None,
            "flag_rate": _pct(sum(bool(c["qa"]["flags"]) for c in v_rev), len(v_rev)),
            "p50_latency_ms": _percentile([t["latency_ms"] for t in vt], 50),
            "errors": sum(1 for t in vt if t["error"]),
        }

    return {
        "window": window,
        "envs": envs,
        "totals": {
            "conversations": n,
            "turns": len(turns),
            "automation_rate": _pct(outcomes["automated"], n),
            "escalation_rate": _pct(outcomes["escalated"], n),
            "decline_rate": _pct(outcomes["declined"], n),
            "csat": _pct(sum(c["csat"] > 0 for c in rated), len(rated)),
            "csat_responses": len(rated),
            "qa_reviewed": len(reviewed),
            "qa_avg_score": round(sum(c["qa"]["score"] for c in reviewed) / len(reviewed), 2) if reviewed else None,
            "qa_flagged": len(flagged),
            "p50_latency_ms": _percentile(latencies, 50),
            "p95_latency_ms": _percentile(latencies, 95),
            "input_tokens": sum(t["input_tokens"] for t in turns),
            "output_tokens": sum(t["output_tokens"] for t in turns),
            "turn_errors": sum(1 for t in turns if t["error"]),
            "messages_per_conversation": round(len(turns) / n, 2) if n else None,
        },
        "aop_usage": dict(Counter(a for t in turns for a in t["aops"]).most_common()),
        "outcomes": dict(outcomes),
        "intents": dict(Counter(t["category"] for t in turns if t["category"]).most_common()),
        "routes": dict(Counter(t["route"] for t in turns if t["route"]).most_common()),
        "qa_flags": dict(Counter(f for c in reviewed for f in c["qa"]["flags"]).most_common()),
        "qa_resolution": dict(Counter(c["qa"]["resolution"] for c in reviewed).most_common()),
        "tools": [
            {"name": name, **s, "failure_rate": _pct(s["failures"], s["calls"])}
            for name, s in sorted(tool_stats.items(), key=lambda kv: -kv[1]["calls"])
        ],
        "series": [{"bucket": k, **v} for k, v in sorted(series.items())],
        "series_unit": "hour" if hourly else "day",
        "by_version": list(by_version.values()),
    }


def tags_for(conv: dict[str, Any]) -> list[str]:
    """Decagon-style tags: whether the AI fully handled it, plus the AI
    analysis's topic tags once the conversation has been analyzed."""
    tags = {"automated": ["Deflected"], "escalated": ["Escalated"], "declined": ["Declined"]}[conv["outcome"]]
    return tags + list((conv.get("qa") or {}).get("topics") or [])


def list_conversations(
    envs: list[str],
    window: str,
    version_id: int | None = None,
    status: str | None = None,
    search: str | None = None,
    limit: int = 100,
    tag: str | None = None,
) -> list[dict[str, Any]]:
    store = get_store()
    convs = store.query_conversations(envs, since_for(window), version_id)
    turns_by_conv: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in store.query_turns([c["id"] for c in convs]):
        turns_by_conv[t["conversation_id"]].append(t)
    out = []
    for c in convs:
        ts = turns_by_conv[c["id"]]
        c["outcome"] = _conv_outcome(c, ts)
        c["intents"] = sorted({t["category"] for t in ts if t["category"]})
        c["tags"] = tags_for(c)
        c["aops"] = sorted({a for t in ts for a in t["aops"]})
        if tag and tag not in c["tags"]:
            continue
        if status == "needs_review" and (c["review"] or {}).get("issue_status") in ("resolved", "no_issue"):
            continue
        if status == "assigned_open" and not ((c["review"] or {}).get("assignee")
                                               and (c["review"] or {}).get("issue_status") != "resolved"):
            continue
        if status == "flagged" and not (c["qa"] and c["qa"]["flags"]):
            continue
        if status == "unreviewed" and c["qa"]:
            continue
        if status == "negative_csat" and not (c["csat"] is not None and c["csat"] < 0):
            continue
        if status in ("escalated", "declined", "automated") and c["outcome"] != status:
            continue
        if search and not _matches(c, search):
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


def _matches(conv: dict[str, Any], search: str) -> bool:
    q = search.lower()
    hay = " ".join([conv["id"], conv["first_message"] or "", (conv.get("qa") or {}).get("summary", "")]).lower()
    return q in hay


def sample(envs: list[str], window: str, size: int, status: str | None = None) -> list[str]:
    """A random batch for human QA -- the way a support lead spot-checks."""
    convs = list_conversations(envs, window, status=status, limit=100_000)
    return [c["id"] for c in random.sample(convs, min(size, len(convs)))]


def review(conversation_id: str) -> dict[str, Any]:
    """Re-run the AI analysis (summary, resolution, tags, QA) for one
    conversation, right now."""
    store = get_store()
    conv = store.get_conversation(conversation_id)
    if conv is None:
        raise KeyError(conversation_id)
    qa = judges.review_conversation(
        [{"user": t["user_text"], "reply": t["reply"], "tool_calls": t["tool_calls"]} for t in conv["turns"]]
    )
    qa["reviewed_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    store.set_qa(conversation_id, qa, analyzed_turns=len(conv["turns"]))
    return qa


REVIEW_FIELDS = {
    "assignee": str,
    "issue_status": ("open", "in_progress", "resolved", "no_issue"),
    "fixed": ("yes", "no", "n/a"),
    "trust_and_safety": ("yes", "no"),
    "feedback": str,
}


def save_human_review(conversation_id: str, data: dict[str, Any], reviewer: str) -> dict[str, Any]:
    review: dict[str, Any] = {}
    for key, kind in REVIEW_FIELDS.items():
        value = data.get(key)
        if value in (None, ""):
            continue
        if isinstance(kind, tuple):
            if value not in kind:
                raise ValueError(f"{key} must be one of {', '.join(kind)}")
        else:
            value = str(value).strip()[:2000]
        review[key] = value
    review["reviewed_by"] = reviewer.strip()[:80] or DEFAULT_AUTHOR
    review["reviewed_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    if not get_store().set_review(conversation_id, review):
        raise KeyError(conversation_id)
    return review
