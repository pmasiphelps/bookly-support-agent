"""
Watchtower: natural-language monitors over conversations ("customer
expressed negative sentiment", "customer asked for an exception to
policy"). Each monitor has criteria and up to three labels; every
analyzed conversation gets a verdict per monitor (flagged, label, reason).

Analysis runs as one background job that also refreshes each
conversation's AI summary / QA review, and only touches conversations
that are new or have grown since they were last analyzed.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import judges
from .monitor import list_conversations, since_for
from .store import get_store

MAX_LABELS = 3

SEED_WATCHTOWERS = [
    {
        "name": "Negative sentiment",
        "criteria": "The customer expresses frustration, anger, or disappointment at any point.",
        "labels": ["Frustrated", "Angry"],
    },
    {
        "name": "Policy exception requests",
        "criteria": "The customer asks for an exception to a Bookly policy (e.g. a return outside the "
                    "30-day window), or asks for a manager or a human to override a decision.",
        "labels": [],
    },
    {
        "name": "Ungrounded answers",
        "criteria": "The agent states a specific fact about an order, account, or policy that no [tool] "
                    "result in the conversation supports, or promises something no tool did.",
        "labels": ["Order facts", "Policy facts", "Promises"],
    },
]


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    name = (data.get("name") or "").strip()
    criteria = (data.get("criteria") or "").strip()
    if not name or not criteria:
        raise ValueError("A Watchtower needs a name and criteria.")
    labels = [l.strip() for l in data.get("labels") or [] if l and l.strip()]
    if len(labels) > MAX_LABELS:
        raise ValueError(f"At most {MAX_LABELS} labels -- more than that can't be told apart on a chart.")
    if len({l.lower() for l in labels}) != len(labels):
        raise ValueError("Labels must be distinct.")
    return {"id": data.get("id"), "name": name, "criteria": criteria, "labels": labels}


def ensure_seeded() -> None:
    store = get_store()
    if not store.list_watchtowers():
        for wt in SEED_WATCHTOWERS:
            store.save_watchtower(wt)


def summaries(envs: list[str], window: str) -> list[dict[str, Any]]:
    store = get_store()
    convs = {c["id"]: c for c in store.query_conversations(envs, since_for(window))}
    results = [r for r in store.watchtower_results(list(convs)) if r["conversation_id"] in convs]
    by_wt: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        by_wt[r["watchtower_id"]].append(r)
    hourly = window in ("1h", "24h")
    out = []
    for wt in store.list_watchtowers():
        rs = by_wt[wt["id"]]
        flagged = [r for r in rs if r["flagged"]]
        daily: dict[str, Counter] = defaultdict(Counter)
        for r in rs:
            started = convs[r["conversation_id"]]["started_at"]
            key = started[:13] + ":00" if hourly else started[:10]
            daily[key]["analyzed"] += 1
            if r["flagged"]:
                daily[key][r["label"] or "Flagged"] += 1
        out.append({
            **wt,
            "analyzed": len(rs),
            "flagged": len(flagged),
            "flagged_rate": round(100 * len(flagged) / len(rs), 1) if rs else None,
            "by_label": dict(Counter(r["label"] or "Flagged" for r in flagged)),
            "series": [{"bucket": k, **v} for k, v in sorted(daily.items())],
            "series_unit": "hour" if hourly else "day",
            "unanalyzed": sum(1 for c in convs.values() if not _fresh(c, by_wt[wt["id"]])),
        })
    return out


def flagged_conversations(wt_id: int, envs: list[str], window: str, label: str | None = None) -> list[dict[str, Any]]:
    store = get_store()
    convs = {c["id"]: c for c in list_conversations(envs, window, limit=10_000)}
    rows = [r for r in store.watchtower_results(list(convs))
            if r["watchtower_id"] == wt_id and r["flagged"] and (label is None or r["label"] == label)]
    return [{**convs[r["conversation_id"]], "watchtower": {"label": r["label"], "reason": r["reason"]}}
            for r in sorted(rows, key=lambda r: convs[r["conversation_id"]]["last_at"], reverse=True)]


def _fresh(conv: dict[str, Any], results_for_wt: list[dict[str, Any]]) -> bool:
    r = next((r for r in results_for_wt if r["conversation_id"] == conv["id"]), None)
    return r is not None and r["analyzed_turns"] >= conv["turn_count"]


# -- the analysis job ---------------------------------------------------------------------

_lock = threading.Lock()
_state: dict[str, Any] = {"running": False, "queued": 0, "done": 0, "failed": 0, "started_at": None}


def analyze_conversation(conversation_id: str, review: bool = True, monitors: list[dict[str, Any]] | None = None) -> None:
    store = get_store()
    conv = store.get_conversation(conversation_id)
    if conv is None or not conv["turns"]:
        return
    turns = [{"user": t["user_text"], "reply": t["reply"], "tool_calls": t["tool_calls"]} for t in conv["turns"]]
    n = len(conv["turns"])
    if review:
        qa = judges.review_conversation(turns)
        qa["reviewed_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        store.set_qa(conversation_id, qa, analyzed_turns=n)
    monitors = store.list_watchtowers() if monitors is None else monitors
    if monitors:
        for wt_id, verdict in judges.evaluate_watchtowers(turns, monitors).items():
            store.save_watchtower_result(wt_id, conversation_id, verdict, n)


def start_analysis(envs: list[str], window: str, limit: int = 40) -> dict[str, Any]:
    """Analyze up to `limit` conversations that are new or have new turns
    since their last analysis, in the background."""
    store = get_store()
    with _lock:
        if _state["running"]:
            return dict(_state)
        convs = store.query_conversations(envs, since_for(window))
        monitors = store.list_watchtowers()
        results = store.watchtower_results([c["id"] for c in convs])
        jobs = []
        for c in convs:
            stale_review = not c["qa"] or (c.get("qa_turns") or 0) < c["turn_count"]
            stale_monitors = [m for m in monitors if not _fresh(c, [r for r in results if r["watchtower_id"] == m["id"]])]
            if stale_review or stale_monitors:
                jobs.append((c["id"], stale_review, stale_monitors))
            if len(jobs) >= limit:
                break
        _state.update(running=bool(jobs), queued=len(jobs), done=0, failed=0,
                      started_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))

    def _one(job) -> None:
        cid, review, mons = job
        try:
            analyze_conversation(cid, review=review, monitors=mons)
        except Exception as exc:  # noqa: BLE001
            print(f"[watchtower] analysis of {cid} failed: {exc}")
            with _lock:
                _state["failed"] += 1
        with _lock:
            _state["done"] += 1

    def _work() -> None:
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(_one, jobs))
        finally:
            with _lock:
                _state["running"] = False

    if jobs:
        threading.Thread(target=_work, daemon=True, name="analysis").start()
    return dict(_state)


def analysis_status() -> dict[str, Any]:
    with _lock:
        return dict(_state)
