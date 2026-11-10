"""
The one path every conversation turn takes -- customer widget, Studio
playground, test runs, and simulated traffic alike: run the turn inside a
telemetry trace, then write it to the conversation log.

Also decides which AgentConfig a *new* production conversation gets:
the deployed production version, or (for a canary deploy) the candidate
version for a stable hash-bucket of sessions.
"""

from __future__ import annotations

import hashlib
import time
import traceback
from typing import Any

from .. import agent, telemetry
from ..agent_config import AgentConfig
from .store import get_store

ENVS = ("production", "playground", "test", "simulation")


def canary_bucket(session_id: str) -> int:
    """0-99, stable per session -- a customer never flips versions mid-chat
    or between page reloads that keep the same session id."""
    return int(hashlib.sha256(session_id.encode()).hexdigest()[:8], 16) % 100


def production_config_for(session_id: str) -> tuple[AgentConfig, int | None]:
    store = get_store()
    dep = store.current_deployment()
    if dep is None:
        return AgentConfig(), None
    version_id = dep["production_version_id"]
    if dep["canary_version_id"] and canary_bucket(session_id) < dep["canary_percent"]:
        version_id = dep["canary_version_id"]
    version = store.get_version(version_id)
    return version["config"], version_id


def config_for_target(target: str | int) -> tuple[AgentConfig, int | None]:
    """'draft' -> the working draft; 'production' -> what's deployed (no
    canary); a version id -> that version."""
    store = get_store()
    if target == "draft":
        draft = store.get_draft()
        return (draft["config"] if draft else AgentConfig()), None
    if target == "production":
        dep = store.current_deployment()
        if dep is None:
            return AgentConfig(), None
        target = dep["production_version_id"]
    version = store.get_version(int(target))
    if version is None:
        raise KeyError(f"No version {target}")
    return version["config"], int(target)


def execute_turn(
    session: agent.Session,
    message: str,
    *,
    env: str,
    category: str | None = None,
    log: bool = True,
) -> tuple[agent.TurnResult, dict[str, Any]]:
    """Run one turn and log it. Returns the TurnResult plus a trace dict
    (route, category, llm calls, tokens, latency) for UIs that show it.

    A crash inside the agent becomes an escalation reply rather than a
    500 -- and is logged with its error, so it shows up in Monitor."""
    start = time.perf_counter()
    error = None
    with telemetry.trace() as t:
        try:
            if category:
                result = agent.run_turn_for_category(session, message, category)
            else:
                result = agent.run_turn(session, message)
        except Exception as exc:  # noqa: BLE001 -- surfaced in the log, not swallowed
            error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
            session.messages.append({"role": "assistant", "content": agent.ESCALATION_REPLY})
            result = agent.TurnResult(
                reply=agent.ESCALATION_REPLY, tool_calls=list(session.tool_calls), route="error", escalated=True
            )
    latency_ms = int((time.perf_counter() - start) * 1000)

    trace = {
        "category": result.category,
        "route": result.route,
        "escalated": result.escalated,
        "llm_calls": [c.to_dict() for c in t.llm_calls],
        "input_tokens": t.input_tokens,
        "output_tokens": t.output_tokens,
        "latency_ms": latency_ms,
        "error": error,
        "version_id": session.version_id,
        "aops": result.aops,
    }
    if log:
        get_store().log_turn(
            session.session_id,
            env,
            session.version_id,
            {
                "user_text": message,
                "reply": result.reply,
                "tool_calls": result.tool_calls,
                "channel": session.channel,
                **trace,
            },
        )
    return result, trace
