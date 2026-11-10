"""
Agent Studio HTTP API, mounted at /api/studio by app/main.py.

No auth -- this is a local demo. A real deployment would put the Studio
behind SSO and keep it off the customer-facing host entirely.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import agent
from ..agent_config import (
    AOP, AVAILABLE_LOOP_TOOLS, CHANNELS, DEFAULT_AUTHOR, FORCEABLE_CATEGORIES, LOOP_TOOL_SCHEMAS, METADATA_FIELDS, AgentConfig,
    aop_refs, metadata_refs, tool_refs,
)
from ..intent_router import _TOOL_SCHEMA as ROUTER_TOOL_SCHEMA
from . import deploy, evals, gitsync, judges, monitor, watchtower
from .store import now
from .runtime import ENVS, config_for_target, execute_turn
from .store import get_store

router = APIRouter(prefix="/api/studio")

CATEGORIES = ROUTER_TOOL_SCHEMA["input_schema"]["properties"]["category"]["enum"]
ROUTES = ["declined", "clarify", "returns_flow", "agent_loop"]
# Models offered in Settings. forced_tools=False: the model rejects a forced
# tool_choice, which the scope gate needs, so it can't be the scope-gate model.
MODEL_CATALOG = [
    {"id": "claude-fable-5-1", "name": "Claude Fable 5.1", "family": "Fable", "forced_tools": False},
    {"id": "claude-fable-5", "name": "Claude Fable 5", "family": "Fable", "forced_tools": True},
    {"id": "claude-opus-5-5", "name": "Claude Opus 5.5", "family": "Opus", "forced_tools": False},
    {"id": "claude-opus-5", "name": "Claude Opus 5", "family": "Opus", "forced_tools": True},
    {"id": "claude-opus-4-8", "name": "Claude Opus 4.8", "family": "Opus", "forced_tools": True},
    {"id": "claude-opus-4-7", "name": "Claude Opus 4.7", "family": "Opus", "forced_tools": True},
    {"id": "claude-opus-4-6", "name": "Claude Opus 4.6", "family": "Opus", "forced_tools": True},
    {"id": "claude-sonnet-5-5", "name": "Claude Sonnet 5.5", "family": "Sonnet", "forced_tools": False},
    {"id": "claude-sonnet-5", "name": "Claude Sonnet 5", "family": "Sonnet", "forced_tools": True},
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "family": "Sonnet", "forced_tools": True},
    {"id": "claude-haiku-5-5", "name": "Claude Haiku 5.5", "family": "Haiku", "forced_tools": True},
    {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5", "family": "Haiku", "forced_tools": True},
]


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


# -- meta ---------------------------------------------------------------------------

@router.get("/meta")
def meta() -> dict[str, Any]:
    return {
        "tools": AVAILABLE_LOOP_TOOLS,
        "all_tools": sorted({*AVAILABLE_LOOP_TOOLS, "initiate_return", "generate_return_label"}),
        "categories": CATEGORIES,
        "routes": ROUTES,
        "models": MODEL_CATALOG,
        "envs": list(ENVS),
        "qa_flags": judges.QA_FLAGS,
        "windows": list(monitor.WINDOWS),
        "channels": list(CHANNELS),
        "forceable_categories": list(FORCEABLE_CATEGORIES),
        "metadata_fields": METADATA_FIELDS,
        "topics": judges.TOPICS,
        "review_fields": {k: list(v) if isinstance(v, tuple) else "text" for k, v in monitor.REVIEW_FIELDS.items()},
    }


# -- build: draft + versions ----------------------------------------------------------

def _version_summary(v: dict[str, Any], dep: dict[str, Any] | None) -> dict[str, Any]:
    gate = deploy.gate_status(v["id"])
    labels = []
    if dep and dep["production_version_id"] == v["id"]:
        labels.append("production")
    if dep and dep["canary_version_id"] == v["id"]:
        labels.append(f"canary {dep['canary_percent']}%")
    return {**v, "labels": labels, "gate": {"passed": gate["passed"], "reason": gate["reason"]}}


@router.get("/draft")
def get_draft() -> dict[str, Any]:
    draft = get_store().get_draft()
    config: AgentConfig = draft["config"]
    base = get_store().get_version(draft["base_version_id"]) if draft["base_version_id"] else None
    return {
        "config": config.to_dict(),
        "reference_problems": config.reference_problems(),
        "fingerprint": config.fingerprint(),
        "base_version_id": draft["base_version_id"],
        "dirty": base is None or base["fingerprint"] != config.fingerprint(),
        "updated_at": draft["updated_at"],
        "compiled_system_prompt": config.compiled_system_prompt(),
    }


class DraftIn(BaseModel):
    config: dict[str, Any]


@router.put("/draft")
def put_draft(body: DraftIn) -> dict[str, Any]:
    store = get_store()
    try:
        config = AgentConfig.from_dict(body.config, check_refs=False)
    except (TypeError, ValueError) as exc:
        raise _bad_request(exc)
    store.save_draft(config, store.get_draft()["base_version_id"])
    return get_draft()


@router.post("/draft/load/{version_id}")
def load_version_into_draft(version_id: int) -> dict[str, Any]:
    store = get_store()
    version = store.get_version(version_id)
    if version is None:
        raise HTTPException(404, f"No version {version_id}")
    store.save_draft(version["config"], base_version_id=version_id)
    return get_draft()


@router.get("/versions")
def list_versions() -> list[dict[str, Any]]:
    store = get_store()
    dep = store.current_deployment()
    return [_version_summary(v, dep) for v in store.list_versions()]


@router.get("/versions/{version_id}")
def get_version(version_id: int) -> dict[str, Any]:
    version = get_store().get_version(version_id)
    if version is None:
        raise HTTPException(404, f"No version {version_id}")
    return {**version, "config": version["config"].to_dict()}


class PublishIn(BaseModel):
    note: str = ""
    open_pr: bool = True
    author: str = ""


@router.post("/versions")
def publish(body: PublishIn) -> dict[str, Any]:
    if not body.note.strip():
        raise HTTPException(400, "Describe what changed in this version.")
    try:
        version_id = deploy.publish_draft(body.note)
    except ValueError as exc:
        raise _bad_request(exc)
    out = get_version(version_id)
    if body.open_pr and gitsync.is_connected():
        try:
            out["git"] = gitsync.open_pr(version_id, body.author or DEFAULT_AUTHOR)
        except (gitsync.GitSyncError, OSError) as exc:
            out["git_error"] = str(exc)
    return out


@router.delete("/versions/{version_id}")
def delete_version(version_id: int, author: str = "") -> dict[str, Any]:
    try:
        deploy.delete_version(version_id)
    except deploy.DeployError as exc:
        raise HTTPException(409, str(exc))
    out: dict[str, Any] = {"deleted": version_id}
    try:
        out["git_notice"] = gitsync.on_version_deleted(version_id, author or DEFAULT_AUTHOR)
    except (gitsync.GitSyncError, OSError) as exc:
        out["git_error"] = str(exc)
    return out


def _config_lines(config: AgentConfig) -> list[str]:
    d = config.to_dict()
    lines = []
    for key in ("agent_model", "router_model", "max_tool_iterations"):
        lines.append(f"{key}: {d[key]}")
    lines.append(f"enabled_tools: {', '.join(d['enabled_tools'])}")
    if d["aop_scoped_tools"]:
        lines.append(f"aop_scoped_tools: {', '.join(d['aop_scoped_tools'])}")
    lines.append("")
    lines.append("## AOPs")
    for a in d["aops"]:
        lines.append(f"### {a['name']}{'' if a['enabled'] else ' (not in release)'}")
        lines.append(f"when: {a['when']}")
        lines += a["logic"].splitlines()
        for channel, text in sorted(a["variants"].items()):
            lines.append(f"[{channel} variant]")
            lines += text.splitlines()
    for key in ("system_prompt", "router_prompt", "out_of_scope_reply"):
        lines.append("")
        lines.append(f"## {key}")
        lines += d[key].splitlines()
    return lines


@router.get("/diff")
def diff(a: str, b: str) -> dict[str, Any]:
    """Unified diff between two targets ('draft', 'production', or a version id)."""
    try:
        ca, _ = config_for_target(a if a in ("draft", "production") else int(a))
        cb, _ = config_for_target(b if b in ("draft", "production") else int(b))
    except (KeyError, ValueError) as exc:
        raise HTTPException(404, str(exc))
    lines = list(difflib.unified_diff(_config_lines(ca), _config_lines(cb), fromfile=a, tofile=b, lineterm="", n=2))
    return {"a": a, "b": b, "identical": ca.fingerprint() == cb.fingerprint(), "diff": lines}


# -- build: playground --------------------------------------------------------------------

_playground: dict[str, agent.Session] = {}


class PlaygroundIn(BaseModel):
    session_id: str
    message: str
    target: str = "draft"
    category: str | None = None
    channel: str = "chat"
    metadata: dict[str, Any] = {}


@router.post("/playground/chat")
def playground_chat(body: PlaygroundIn) -> dict[str, Any]:
    session = _playground.get(body.session_id)
    if session is None:
        try:
            config, version_id = config_for_target(body.target if body.target in ("draft", "production") else int(body.target))
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, str(exc))
        if body.channel not in CHANNELS:
            raise HTTPException(400, f"channel must be one of {CHANNELS}")
        session = agent.Session(
            session_id=body.session_id, config=config, version_id=version_id, channel=body.channel,
            metadata_overrides={k: v for k, v in body.metadata.items() if k in METADATA_FIELDS},
        )
        _playground[body.session_id] = session
    result, trace = execute_turn(session, body.message, env="playground", category=body.category)
    return {
        "reply": result.reply,
        "suggestions": result.suggestions,
        "tool_calls": json.loads(json.dumps(result.tool_calls, default=str)),
        "trace": trace,
        "aops": result.aops,
        "session": {
            "metadata": session.metadata(),
            "verified_email": session.verified_email,
            "verified_order_id": session.verified_order_id,
            "return_flow": session.return_flow.__dict__ if session.return_flow else None,
            "fingerprint": session.config.fingerprint(),
        },
    }


# -- test ---------------------------------------------------------------------------------------

@router.get("/tests")
def list_tests() -> list[dict[str, Any]]:
    return get_store().list_test_cases()


@router.post("/tests")
def save_test(case: dict[str, Any]) -> dict[str, Any]:
    try:
        normalized = evals.normalize_case(case)
    except (ValueError, TypeError) as exc:
        raise _bad_request(exc)
    get_store().upsert_test_case(normalized)
    return normalized


@router.delete("/tests/{case_id}")
def delete_test(case_id: str) -> dict[str, str]:
    get_store().delete_test_case(case_id)
    return {"deleted": case_id}


class FromConversationIn(BaseModel):
    conversation_id: str
    name: str


@router.post("/tests/from-conversation")
def test_from_conversation(body: FromConversationIn) -> dict[str, Any]:
    conv = get_store().get_conversation(body.conversation_id)
    if conv is None or not conv["turns"]:
        raise HTTPException(404, "No logged turns for that conversation.")
    case = evals.case_from_conversation(body.name, conv["turns"])
    get_store().upsert_test_case(case)
    return case


class RunIn(BaseModel):
    target: str = "draft"  # 'draft', 'production', or a version id
    case_ids: list[str] | None = None


@router.post("/runs")
def start_run(body: RunIn) -> dict[str, Any]:
    try:
        target: str | int = body.target if body.target in ("draft", "production") else int(body.target)
        run_id = evals.start_run(target, body.case_ids)
    except (KeyError, ValueError) as exc:
        raise _bad_request(exc)
    return get_store().get_test_run(run_id)


@router.get("/runs")
def list_runs() -> list[dict[str, Any]]:
    return get_store().list_test_runs()


@router.get("/runs/{run_id}")
def get_run(run_id: int) -> dict[str, Any]:
    run = get_store().get_test_run(run_id)
    if run is None:
        raise HTTPException(404, f"No run {run_id}")
    return run


# -- deploy ---------------------------------------------------------------------------------------

@router.get("/deployment")
def deployment() -> dict[str, Any]:
    store = get_store()
    return {"current": store.current_deployment(), "history": store.deployment_history()}


@router.get("/gate/{version_id}")
def gate(version_id: int) -> dict[str, Any]:
    try:
        return deploy.gate_status(version_id)
    except deploy.DeployError as exc:
        raise HTTPException(404, str(exc))


class DeployIn(BaseModel):
    version_id: int
    mode: str = "full"
    canary_percent: int = 10
    note: str = ""
    override_reason: str | None = None
    author: str = ""


def _deploy_call(fn, *args, author: str = "", note: str = "", **kwargs) -> dict[str, Any]:
    store = get_store()
    before = store.current_deployment()
    try:
        fn(*args, note=note, **kwargs)
    except deploy.DeployError as exc:
        raise HTTPException(409, str(exc))
    out = deployment()
    after = out["current"]
    # Mirror the release in GitHub. A GitHub failure never undoes a deploy.
    action = store.deployment_history(limit=1)[0]["action"]
    subject = after["canary_version_id"] if action == "canary" else (
        before["canary_version_id"] if action == "abort_canary" and before else after["production_version_id"])
    try:
        out["git_notice"] = gitsync.on_deployed(action, subject, note, author or DEFAULT_AUTHOR,
                                                previous_production=before["production_version_id"] if before else None)
    except (gitsync.GitSyncError, OSError) as exc:
        out["git_error"] = str(exc)
    return out


@router.post("/deploy")
def do_deploy(body: DeployIn) -> dict[str, Any]:
    return _deploy_call(
        deploy.deploy, body.version_id, mode=body.mode, canary_percent=body.canary_percent,
        note=body.note, override_reason=body.override_reason, author=body.author,
    )


class NoteIn(BaseModel):
    note: str = ""
    author: str = ""


@router.post("/deploy/promote")
def do_promote(body: NoteIn) -> dict[str, Any]:
    return _deploy_call(deploy.promote_canary, note=body.note, author=body.author)


@router.post("/deploy/abort-canary")
def do_abort(body: NoteIn) -> dict[str, Any]:
    return _deploy_call(deploy.abort_canary, note=body.note, author=body.author)


@router.post("/deploy/rollback")
def do_rollback(body: NoteIn) -> dict[str, Any]:
    return _deploy_call(deploy.rollback, note=body.note, author=body.author)


# -- GitHub ----------------------------------------------------------------------------------------

class GitConnectIn(BaseModel):
    repo: str
    author: str = ""


class AuthorIn(BaseModel):
    author: str = ""


def _git_call(fn, *args) -> Any:
    try:
        return fn(*args)
    except (gitsync.GitSyncError, OSError) as exc:
        raise HTTPException(409, str(exc))


@router.get("/git")
def git_status() -> dict[str, Any]:
    return gitsync.status()


@router.post("/git/connect")
def git_connect(body: GitConnectIn) -> dict[str, Any]:
    return _git_call(gitsync.connect, body.repo, body.author or DEFAULT_AUTHOR)


@router.post("/git/disconnect")
def git_disconnect() -> dict[str, Any]:
    gitsync.disconnect()
    return gitsync.status()


@router.post("/git/pull")
def git_pull(body: AuthorIn) -> dict[str, Any]:
    return _git_call(gitsync.pull_main, body.author or DEFAULT_AUTHOR)


@router.post("/git/prs/{version_id}")
def git_open_pr(version_id: int, body: AuthorIn) -> dict[str, Any]:
    return _git_call(gitsync.open_pr, version_id, body.author or DEFAULT_AUTHOR)


# -- monitor ---------------------------------------------------------------------------------------

def _envs(env: str) -> list[str]:
    envs = [e for e in env.split(",") if e in ENVS]
    return envs or ["production"]


@router.get("/monitor/summary")
def monitor_summary(env: str = "production", window: str = "7d", version_id: int | None = None) -> dict[str, Any]:
    return monitor.summary(_envs(env), window, version_id)


@router.get("/monitor/conversations")
def monitor_conversations(
    env: str = "production", window: str = "7d", version_id: int | None = None,
    status: str | None = None, search: str | None = None,
) -> list[dict[str, Any]]:
    return monitor.list_conversations(_envs(env), window, version_id, status or None, search or None)


@router.get("/monitor/conversations/{conversation_id}")
def monitor_conversation(conversation_id: str) -> dict[str, Any]:
    conv = get_store().get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(404, "No such conversation")
    return conv


@router.post("/monitor/conversations/{conversation_id}/review")
def monitor_review(conversation_id: str) -> dict[str, Any]:
    try:
        return monitor.review(conversation_id)
    except KeyError:
        raise HTTPException(404, "No such conversation")


class SimulateIn(BaseModel):
    count: int = 8


@router.post("/monitor/simulate")
def monitor_simulate(body: SimulateIn) -> dict[str, Any]:
    ids = evals.run_simulated_traffic(max(1, min(body.count, 40)), evals.TRAFFIC_SCENARIOS)
    return {"started": len(ids), "conversation_ids": ids}


# -- build: AOPs ------------------------------------------------------------------------------

# The deterministic returns flow, presented as a locked AOP so the list
# shows everything that shapes the agent's behavior.
CODED_RETURNS_AOP = {
    "id": "returns-refunds-coded",
    "name": agent.RETURNS_FLOW_AOP,
    "type": "coded",
    "status": "live",
    "when": "The scope gate classifies a message as returns_refunds and no text AOP is forced for "
            "returns_refunds. Runs before the model, every time.",
    "logic": (
        "1. Collect the order ID and account email (regex -- fixed formats).\n"
        "2. Verify identity and fetch the order with @get_order_status.\n"
        "3. If the order has several books, ask which one (title or \"the second one\").\n"
        "4. Summarize the customer's stated reason (the one model call in this flow).\n"
        "5. File it with @initiate_return -- the 30-day window is enforced in code.\n"
        "6. Offer a prepaid label via @generate_return_label; the refund lands on receipt."
    ),
    "source": "app/returns_flow.py",
    "tools": ["get_order_status", "initiate_return", "generate_return_label"],
    "variants": {},
    "enabled": True,
}


def _aop_status(aop: AOP, prod: AgentConfig | None) -> str:
    """Live = released exactly as drafted; Update = released, but the draft
    has edits; Draft = not in production (new, or not marked for release)."""
    live = next((a for a in prod.aops if a.id == aop.id and a.enabled), None) if prod else None
    if live is None or not aop.enabled:
        return "draft"
    return "live" if live.behavior() == aop.behavior() else "update"


def _aop_view(aop: AOP, cfg: AgentConfig, prod: AgentConfig | None, usage: dict[str, int],
              problems: dict[str, list[str]]) -> dict[str, Any]:
    names = [a.name for a in cfg.aops]
    return {
        **aop.__dict__,
        "type": "text",
        "status": _aop_status(aop, prod),
        "tools": sorted(tool_refs(aop.all_text())),
        "linked_aops": sorted(aop_refs(aop.all_text(), names)[0]),
        "metadata": sorted(metadata_refs(aop.all_text())),
        "problems": problems.get(aop.id, []),
        "conversations_7d": usage.get(aop.name, 0),
    }


def _aop_usage() -> dict[str, int]:
    convs = monitor.list_conversations(["production", "simulation"], "7d", limit=100_000)
    counts: dict[str, int] = {}
    for c in convs:
        for a in c["aops"]:
            counts[a] = counts.get(a, 0) + 1
    return counts


@router.get("/aops")
def list_aops() -> dict[str, Any]:
    store = get_store()
    cfg = store.get_draft()["config"]
    prod, _ = config_for_target("production")
    usage = _aop_usage()
    problems = cfg.reference_problems()
    removed = [a.name for a in prod.aops if a.enabled and a.id not in {x.id for x in cfg.aops if x.enabled}]
    # The coded flow shows up only while a config actually uses it.
    coded = []
    if cfg.uses_coded_returns():
        coded = [{**CODED_RETURNS_AOP, "status": "live" if prod.uses_coded_returns() else "draft",
                  "conversations_7d": usage.get(agent.RETURNS_FLOW_AOP, 0)}]
    elif prod.uses_coded_returns():
        removed.append(f"{agent.RETURNS_FLOW_AOP} -- replaced by {cfg.forced_aop('returns_refunds').name}")
    return {"aops": coded + [_aop_view(a, cfg, prod, usage, problems) for a in cfg.aops], "pending_removals": removed}


class AOPIn(BaseModel):
    name: str
    when: str
    logic: str
    variants: dict[str, str] = {}
    enabled: bool = True
    force_category: str = ""
    author: str = ""


def _save_aop(aop_id: str | None, body: AOPIn) -> dict[str, Any]:
    store = get_store()
    draft = store.get_draft()
    cfg: AgentConfig = draft["config"]
    existing = next((a for a in cfg.aops if a.id == aop_id), None) if aop_id else None
    if aop_id and existing is None:
        raise HTTPException(404, f"No AOP {aop_id}")
    author = body.author.strip() or DEFAULT_AUTHOR
    aop = AOP(
        name=body.name.strip(), when=body.when.strip(), logic=body.logic.strip(),
        id=existing.id if existing else "", variants=body.variants, enabled=body.enabled,
        force_category=body.force_category,
        created_by=existing.created_by if existing else author, updated_by=author, updated_at=now(),
    )
    aops = [aop if a.id == aop.id else a for a in cfg.aops] if existing else [*cfg.aops, aop]
    if not existing and any(a.id == aop.id for a in cfg.aops):
        raise HTTPException(400, f"An AOP named {aop.name!r} already exists")
    try:
        new_cfg = AgentConfig.from_dict({**cfg.to_dict(), "aops": [a.__dict__ for a in aops]}, check_refs=False)
        store.save_draft(new_cfg, draft["base_version_id"])
    except ValueError as exc:
        raise _bad_request(exc)
    prod, _ = config_for_target("production")
    return _aop_view(aop, new_cfg, prod, _aop_usage(), new_cfg.reference_problems())


@router.post("/aops")
def create_aop(body: AOPIn) -> dict[str, Any]:
    return _save_aop(None, body)


@router.put("/aops/{aop_id}")
def update_aop(aop_id: str, body: AOPIn) -> dict[str, Any]:
    return _save_aop(aop_id, body)


@router.delete("/aops/{aop_id}")
def delete_aop(aop_id: str) -> dict[str, str]:
    store = get_store()
    draft = store.get_draft()
    cfg: AgentConfig = draft["config"]
    if not any(a.id == aop_id for a in cfg.aops):
        raise HTTPException(404, f"No AOP {aop_id}")
    cfg.aops = [a for a in cfg.aops if a.id != aop_id]
    store.save_draft(cfg, draft["base_version_id"])
    return {"deleted": aop_id}


# -- build: tools -------------------------------------------------------------------------------

TOOL_GUARDRAILS = {
    "get_order_status": "Order details only when the email matches the order's account.",
    "list_orders_for_customer": "Lists order IDs, dates, and statuses only -- no details.",
    "initiate_return": "Re-verifies identity, the item, delivery, and the 30-day window; idempotent per item.",
    "generate_return_label": "Only for an item that already has a return on file; re-verifies identity.",
    "escalate_to_human": "Refuses to escalate a return already ruled ineligible in the conversation.",
}

# The system each tool talks to, for the Tools page: (logo key, detail).
# Snowflake is mocked in memory unless BOOKLY_DATA_BACKEND=live;
# the carrier and helpdesk calls are mocked.
TOOL_SOURCES = {
    "get_order_status": ("snowflake", "Snowflake · reads ORDERS and ORDER_ITEMS"),
    "list_orders_for_customer": ("snowflake", "Snowflake · reads ORDERS"),
    "initiate_return": ("snowflake", "Snowflake · writes RETURNS"),
    "generate_return_label": ("api", "Carrier REST API (UPS) · mocked"),
    "search_policies": ("snowflake", "Snowflake Cortex Search · bookly_policy_search"),
    "escalate_to_human": ("freshdesk", "Freshdesk · opens a ticket · mocked"),
}


@router.get("/tools")
def list_tools() -> list[dict[str, Any]]:
    cfg: AgentConfig = get_store().get_draft()["config"]
    used_by: dict[str, list[str]] = {}
    for a in cfg.aops:
        for t in tool_refs(a.all_text()):
            used_by.setdefault(t, []).append(a.name)
    if cfg.uses_coded_returns():
        for t in CODED_RETURNS_AOP["tools"]:
            used_by.setdefault(t, []).append(agent.RETURNS_FLOW_AOP)
    calls: dict[str, int] = {}
    for s in [monitor.summary(["production", "simulation"], "7d")]:
        for t in s["tools"]:
            calls[t["name"]] = t["calls"]
    out = []
    for schema in LOOP_TOOL_SCHEMAS:
        name = schema["name"]
        out.append({
            "name": name,
            "description": schema["description"],
            "input_schema": schema["input_schema"],
            "type": "coded",
            "scope": "agent",
            "enabled": name in cfg.enabled_tools,
            "aop_only": name in cfg.aop_scoped_tools,
            # Checks the tool enforces in code, whatever the AOP or model says.
            "enforces": TOOL_GUARDRAILS.get(name, ""),
            "source": TOOL_SOURCES.get(name, ("api", ""))[0],
            "source_detail": TOOL_SOURCES.get(name, ("api", ""))[1],
            "used_by": sorted(set(used_by.get(name, []))),
            "calls_7d": calls.get(name, 0),
        })
    return out


class ToolToggleIn(BaseModel):
    enabled: bool | None = None
    aop_only: bool | None = None  # only usable inside AOPs that reference it


@router.put("/tools/{name}")
def toggle_tool(name: str, body: ToolToggleIn) -> list[dict[str, Any]]:
    if name not in AVAILABLE_LOOP_TOOLS:
        raise HTTPException(404, f"No tool {name}")
    store = get_store()
    draft = store.get_draft()
    cfg: AgentConfig = draft["config"]
    if body.enabled is not None:
        tools = [t for t in cfg.enabled_tools if t != name] + ([name] if body.enabled else [])
        cfg.enabled_tools = [t for t in AVAILABLE_LOOP_TOOLS if t in tools]
    if body.aop_only is not None:
        scoped = [t for t in cfg.aop_scoped_tools if t != name] + ([name] if body.aop_only else [])
        cfg.aop_scoped_tools = [t for t in AVAILABLE_LOOP_TOOLS if t in scoped]
    store.save_draft(cfg, draft["base_version_id"])
    return list_tools()


# -- convos -----------------------------------------------------------------------------------------

@router.get("/convos")
def convos(
    env: str = "production", window: str = "7d", version_id: int | None = None,
    status: str | None = None, search: str | None = None, tag: str | None = None, limit: int = 200,
) -> list[dict[str, Any]]:
    return monitor.list_conversations(_envs(env), window, version_id, status or None, search or None,
                                      limit=min(limit, 1000), tag=tag or None)


@router.get("/convos/{conversation_id}")
def convo(conversation_id: str) -> dict[str, Any]:
    store = get_store()
    conv = store.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(404, "No such conversation")
    turns = conv["turns"]
    routes = {t["route"] for t in turns}
    conv["outcome"] = ("escalated" if conv["escalated"] else
                       "declined" if routes and routes <= {"declined", "clarify"} else "automated")
    conv["tags"] = monitor.tags_for(conv)
    wts = {w["id"]: w for w in store.list_watchtowers()}
    conv["watchtowers"] = [
        {"id": r["watchtower_id"], "name": wts[r["watchtower_id"]]["name"], "flagged": bool(r["flagged"]),
         "label": r["label"], "reason": r["reason"], "stale": r["analyzed_turns"] < conv["turn_count"]}
        for r in store.watchtower_results([conversation_id]) if r["watchtower_id"] in wts
    ]
    return conv


class HumanReviewIn(BaseModel):
    review: dict[str, Any]
    reviewer: str = ""


@router.put("/convos/{conversation_id}/review")
def human_review(conversation_id: str, body: HumanReviewIn) -> dict[str, Any]:
    try:
        return monitor.save_human_review(conversation_id, body.review, body.reviewer)
    except KeyError:
        raise HTTPException(404, "No such conversation")
    except ValueError as exc:
        raise _bad_request(exc)


@router.post("/convos/{conversation_id}/analyze")
def analyze_convo(conversation_id: str) -> dict[str, Any]:
    if get_store().get_conversation(conversation_id) is None:
        raise HTTPException(404, "No such conversation")
    watchtower.analyze_conversation(conversation_id)
    return convo(conversation_id)


class SampleIn(BaseModel):
    env: str = "production"
    window: str = "7d"
    size: int = 10
    status: str | None = None


@router.post("/convos/sample")
def sample_batch(body: SampleIn) -> dict[str, Any]:
    ids = monitor.sample(_envs(body.env), body.window, max(1, min(body.size, 50)), body.status or None)
    return {"conversation_ids": ids}


# -- watchtower --------------------------------------------------------------------------------------

@router.get("/watchtowers")
def list_watchtowers(env: str = "production", window: str = "7d") -> list[dict[str, Any]]:
    return watchtower.summaries(_envs(env), window)


@router.post("/watchtowers")
def create_watchtower(data: dict[str, Any]) -> dict[str, Any]:
    try:
        wt = watchtower.normalize({**data, "id": None})
    except ValueError as exc:
        raise _bad_request(exc)
    return {**wt, "id": get_store().save_watchtower(wt)}


@router.put("/watchtowers/{wt_id}")
def update_watchtower(wt_id: int, data: dict[str, Any]) -> dict[str, Any]:
    if not any(w["id"] == wt_id for w in get_store().list_watchtowers()):
        raise HTTPException(404, "No such Watchtower")
    try:
        wt = watchtower.normalize({**data, "id": wt_id})
    except ValueError as exc:
        raise _bad_request(exc)
    get_store().save_watchtower(wt)
    return wt


@router.delete("/watchtowers/{wt_id}")
def delete_watchtower(wt_id: int) -> dict[str, int]:
    get_store().delete_watchtower(wt_id)
    return {"deleted": wt_id}


@router.get("/watchtowers/{wt_id}/flagged")
def watchtower_flagged(wt_id: int, env: str = "production", window: str = "7d",
                       label: str | None = None) -> list[dict[str, Any]]:
    return watchtower.flagged_conversations(wt_id, _envs(env), window, label or None)


class AnalysisIn(BaseModel):
    env: str = "production"
    window: str = "7d"
    limit: int = 40


@router.post("/analysis")
def start_analysis(body: AnalysisIn) -> dict[str, Any]:
    return watchtower.start_analysis(_envs(body.env), body.window, max(1, min(body.limit, 200)))


@router.get("/analysis")
def analysis_status() -> dict[str, Any]:
    return watchtower.analysis_status()
