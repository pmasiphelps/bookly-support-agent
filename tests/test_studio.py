"""Tests for the Agent Studio (app/studio/): config versioning, the release
gate, canary routing, per-turn logging + Monitor metrics, and the test
runner. Every model call is scripted or stubbed -- no credentials needed;
the Studio store is an in-memory SQLite database per test."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import agent, mock_snowflake, telemetry
from app.agent_config import AOP, AgentConfig
from app.studio import deploy, evals, judges, monitor, runtime
from app.studio.store import Store, set_store
from tests.fake_anthropic import FakeResponse, ScriptedClient, TextBlock, ToolUseBlock


@pytest.fixture(autouse=True)
def studio_store():
    store = Store(":memory:")
    set_store(store)
    mock_snowflake.RETURNS.clear()
    agent._sessions.clear()
    yield store
    set_store(None)


def _stub_router(monkeypatch, category="order_status"):
    seen = {}

    def _classify(client, prior, text, **overrides):
        seen.update(overrides)
        return category

    monkeypatch.setattr(agent, "classify_intent", _classify)
    return seen


def _scripted(monkeypatch, script):
    client = ScriptedClient(script)
    monkeypatch.setattr(agent, "_get_client", lambda: client)
    return client


def _say(text):
    return FakeResponse(content=[TextBlock(text)], stop_reason="end_turn")


def _pass_full_suite(store, config):
    run_id = store.create_test_run("draft", None, config.fingerprint(), 1, full_suite=True)
    store.add_test_result(run_id, {"case_id": "c", "case_name": "c", "kind": "scripted", "passed": True,
                                   "checks": [], "transcript": []})
    store.finish_test_run(run_id)
    return run_id


# -- AgentConfig ------------------------------------------------------------------

def test_default_config_matches_pre_studio_behavior():
    config = AgentConfig()
    from app.prompts import SYSTEM_PROMPT
    from app.tools import FLEXIBLE_LOOP_TOOLS

    assert config.compiled_system_prompt() == SYSTEM_PROMPT
    assert config.loop_tools() == FLEXIBLE_LOOP_TOOLS
    assert config.max_tool_iterations == agent.MAX_TOOL_ITERATIONS


def test_aop_catalog_compiles_into_prompt_and_steps_load_on_demand():
    base = AgentConfig()
    with_aop = AgentConfig(aops=[AOP("Lost package", "Customer says it never arrived", "1. Check @get_order_status")])
    disabled = AgentConfig(aops=[AOP("Old", "never", "Do Y", enabled=False)])

    prompt = with_aop.compiled_system_prompt()
    assert "- Lost package: Customer says it never arrived" in prompt
    assert "Check" not in prompt  # steps aren't in the prompt...
    assert "the `get_order_status` tool" in with_aop.render_aop("lost package")  # ...they're loaded via use_aop
    assert with_aop.loop_tools()[-1]["input_schema"]["properties"]["name"]["enum"] == ["Lost package"]
    assert "Old" not in disabled.compiled_system_prompt()
    assert disabled.render_aop("Old") is None
    assert base.fingerprint() != with_aop.fingerprint()
    assert AgentConfig.from_dict(with_aop.to_dict()).fingerprint() == with_aop.fingerprint()


def test_aop_bookkeeping_doesnt_change_the_fingerprint():
    a = AgentConfig(aops=[AOP("X", "when", "do", updated_by="ann", updated_at="2026-01-01")])
    b = AgentConfig(aops=[AOP("X", "when", "do", updated_by="bo", updated_at="2026-02-02")])
    assert a.fingerprint() == b.fingerprint()


def test_aop_references_are_checked():
    cfg = AgentConfig(
        enabled_tools=["get_order_status", "search_policies"],
        aops=[
            AOP("Escalation request", "asks for a human", "Call @escalate_to_human"),
            AOP("Lost package", "never arrived",
                "Use @get_order_status, greet {{ customer_name }}, then #Escalation request. "
                "Also @made_up, {{ shoe_size }}, #Nonexistent thing."),
        ],
    )
    problems = cfg.reference_problems()
    assert problems["escalation-request"] == ["@escalate_to_human is turned off in Tools"]
    assert set(problems["lost-package"]) == {
        "@made_up isn't a tool the agent can call",
        "#Nonexistent thing… doesn't match any AOP",
        "{{ shoe_size }} isn't a known metadata field",
    }
    with pytest.raises(ValueError, match="Broken AOP references"):
        cfg.validate()
    cfg.validate(check_refs=False)  # drafts may be saved half-written


def test_channel_variant_and_email_prompt():
    cfg = AgentConfig(aops=[AOP("X", "when", "chat steps", variants={"email": "email steps"})])
    assert "chat steps" in cfg.render_aop("X", "chat")
    assert "email steps" in cfg.render_aop("X", "email")
    assert "over email" in cfg.compiled_system_prompt("email")
    assert "over email" in AgentConfig().compiled_system_prompt("email")


def test_legacy_procedures_load_as_aops():
    cfg = AgentConfig.from_dict({"procedures": [{"name": "P", "trigger": "t", "instructions": "i", "enabled": False}]})
    assert (cfg.aops[0].name, cfg.aops[0].when, cfg.aops[0].logic, cfg.aops[0].enabled) == ("P", "t", "i", False)


def test_unknown_tools_are_rejected_and_returns_tools_are_opt_in():
    with pytest.raises(ValueError):
        AgentConfig.from_dict({"enabled_tools": ["search_policies", "drop_tables"]})
    assert "initiate_return" not in AgentConfig().enabled_tools  # v1: returns run through the coded flow
    AgentConfig.from_dict({"enabled_tools": ["initiate_return", "generate_return_label"]})


def test_agent_loop_uses_the_sessions_config(monkeypatch):
    seen = _stub_router(monkeypatch)
    client = _scripted(monkeypatch, [_say("ok")])
    config = AgentConfig(
        agent_model="model-x", router_model="router-y",
        enabled_tools=["search_policies"],
        aops=[AOP("Tone", "Sign off with 'Happy reading!'", "Be nice")],
    )
    session = agent.Session(session_id="s", config=config)

    result = agent.run_turn(session, "how long does shipping take?")

    call = client.messages.calls[0]
    assert call["model"] == "model-x"
    assert [t["name"] for t in call["tools"]] == ["search_policies", "use_aop"]
    assert "Happy reading!" in call["system"]
    assert seen == {"system_prompt": config.router_prompt, "model": "router-y"}
    assert (result.category, result.route) == ("order_status", "agent_loop")


def test_disabled_tool_is_refused_even_if_the_model_calls_it(monkeypatch):
    _stub_router(monkeypatch)
    _scripted(monkeypatch, [
        FakeResponse([ToolUseBlock("t1", "get_order_status", {"order_id": "BK-1001", "email": "alice@bookly-demo.com"})],
                     "tool_use"),
        _say("Sorry, I can't look that up."),
    ])
    session = agent.Session(session_id="s", config=AgentConfig(enabled_tools=["search_policies"]))

    result = agent.run_turn(session, "where is BK-1001, alice@bookly-demo.com")

    assert result.tool_calls[0]["result"]["error"] == "unknown_tool"
    assert session.verified_email is None


def test_custom_out_of_scope_reply(monkeypatch):
    _stub_router(monkeypatch, "out_of_scope")
    session = agent.Session(session_id="s", config=AgentConfig(out_of_scope_reply="Nope, books only."))
    result = agent.run_turn(session, "write a poem")
    assert (result.reply, result.route) == ("Nope, books only.", "declined")


def test_telemetry_records_usage_only_inside_a_trace():
    response = SimpleNamespace(content=[], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=120, output_tokens=30))
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: response))

    telemetry.create_message(client, "agent", model="m")  # no active trace: no-op
    with telemetry.trace() as t:
        telemetry.create_message(client, "agent", model="m")
    assert (t.input_tokens, t.output_tokens, t.llm_calls[0].purpose) == (120, 30, "agent")


# -- versions, gate, deploys -----------------------------------------------------------

def test_bootstrap_puts_code_defaults_in_production(studio_store):
    deploy.ensure_initialized(evals.SEED_CASES)
    deploy.ensure_initialized(evals.SEED_CASES)  # idempotent

    assert len(studio_store.list_versions()) == 1
    assert studio_store.current_deployment()["production_version_id"] == 1
    assert studio_store.get_version(1)["config"].fingerprint() == AgentConfig().fingerprint()
    draft = studio_store.get_draft()["config"]
    assert [a.name for a in draft.aops] == ["Escalation request", "Returns & refunds", "Lost package"]
    assert draft.forced_aop("returns_refunds").name == "Returns & refunds"
    assert "initiate_return" in draft.enabled_tools
    assert "deterministic flow" not in draft.system_prompt  # the v1 paragraph is gone
    assert studio_store.get_version(1)["config"].uses_coded_returns()
    assert draft.reference_problems() == {}
    assert len(studio_store.list_test_cases()) == len(evals.SEED_CASES)


def test_gate_blocks_untested_versions_and_accepts_a_passing_draft_run(studio_store):
    deploy.ensure_initialized([])
    changed = AgentConfig(out_of_scope_reply="Books only, sorry!")
    studio_store.save_draft(changed, base_version_id=1)
    v2 = deploy.publish_draft("Shorter decline")

    with pytest.raises(deploy.DeployError, match="gate"):
        deploy.deploy(v2)

    # A run against the draft counts: same fingerprint, same behavior.
    _pass_full_suite(studio_store, changed)
    deploy.deploy(v2, note="ship it")
    assert studio_store.current_deployment()["production_version_id"] == v2


def test_failing_run_blocks_and_override_is_recorded(studio_store):
    deploy.ensure_initialized([])
    studio_store.save_draft(AgentConfig(max_tool_iterations=3), base_version_id=1)
    v2 = deploy.publish_draft("Fewer iterations")
    cfg = studio_store.get_version(v2)["config"]
    run_id = studio_store.create_test_run("v2", v2, cfg.fingerprint(), 1, full_suite=True)
    studio_store.add_test_result(run_id, {"case_id": "c", "case_name": "c", "kind": "scripted", "passed": False,
                                          "checks": [], "transcript": []})
    studio_store.finish_test_run(run_id)

    assert "failing" in deploy.gate_status(v2)["reason"]
    deploy.deploy(v2, override_reason="Hotfix, failing test is known-flaky")
    assert studio_store.current_deployment()["gate_override_reason"].startswith("Hotfix")


def test_canary_routes_a_stable_slice_then_promote_and_rollback(studio_store):
    deploy.ensure_initialized([])
    cfg = AgentConfig(out_of_scope_reply="v2 decline")
    studio_store.save_draft(cfg, base_version_id=1)
    v2 = deploy.publish_draft("v2")
    _pass_full_suite(studio_store, cfg)

    deploy.deploy(v2, mode="canary", canary_percent=30)
    assigned = [runtime.production_config_for(f"s{i}")[1] for i in range(400)]
    share = assigned.count(v2) / len(assigned)
    assert 0.2 < share < 0.4
    assert runtime.production_config_for("s7")[1] == runtime.production_config_for("s7")[1]

    deploy.promote_canary()
    assert runtime.production_config_for("anyone")[1] == v2
    deploy.rollback()
    assert runtime.production_config_for("anyone")[1] == 1


def test_canary_requires_something_in_production(studio_store):
    studio_store.create_version(AgentConfig(), "v1")
    with pytest.raises(deploy.DeployError):
        deploy.deploy(1, mode="canary", override_reason="x")


# -- logging + monitor ---------------------------------------------------------------------

def test_execute_turn_logs_and_monitor_aggregates(monkeypatch, studio_store):
    _stub_router(monkeypatch)
    _scripted(monkeypatch, [
        FakeResponse([ToolUseBlock("t1", "search_policies", {"query": "shipping"})], "tool_use"),
        _say("4-6 business days."),
    ])
    session = agent.Session(session_id="conv-1", version_id=1)
    runtime.execute_turn(session, "how long is shipping?", env="production")

    monkeypatch.setattr(agent, "classify_intent", lambda *a, **kw: "out_of_scope")
    runtime.execute_turn(agent.Session(session_id="conv-2", version_id=1), "tell me a joke", env="production")
    studio_store.set_csat("conv-1", 1)

    conv = studio_store.get_conversation("conv-1")
    assert conv["turns"][0]["route"] == "agent_loop"
    assert conv["turns"][0]["tool_calls"][0]["name"] == "search_policies"

    s = monitor.summary(["production"], "all")
    assert s["totals"]["conversations"] == 2
    assert s["outcomes"] == {"automated": 1, "declined": 1}
    assert s["totals"]["csat"] == 100.0
    assert s["tools"][0]["name"] == "search_policies"
    assert s["by_version"][0]["version_id"] == 1
    assert [c["id"] for c in monitor.list_conversations(["production"], "all", status="declined")] == ["conv-2"]


def test_agent_crash_becomes_a_logged_escalation(monkeypatch, studio_store):
    def _boom(*a, **kw):
        raise RuntimeError("router down")

    monkeypatch.setattr(agent, "classify_intent", _boom)
    result, trace = runtime.execute_turn(agent.Session(session_id="c"), "hi", env="production")

    assert result.escalated and "human agent" in result.reply
    assert "router down" in trace["error"]
    assert monitor.summary(["production"], "all")["totals"]["escalation_rate"] == 100.0


def test_qa_review_is_stored(monkeypatch, studio_store):
    studio_store.log_turn("c1", "production", 1, {"user_text": "hi", "reply": "hello", "latency_ms": 5})
    monkeypatch.setattr(judges, "review_conversation", lambda turns: {
        "summary": "greeting", "resolution": "resolved", "score": 2, "flags": ["tone"], "suggested_fix": ""})

    monitor.review("c1")

    assert monitor.summary(["production"], "all")["qa_flags"] == {"tone": 1}
    assert monitor.list_conversations(["production"], "all", status="flagged")[0]["id"] == "c1"


# -- test runner ---------------------------------------------------------------------------

def test_scripted_case_checks_each_turn(monkeypatch):
    _stub_router(monkeypatch, "general_policy")
    _scripted(monkeypatch, [
        FakeResponse([ToolUseBlock("t1", "search_policies", {"query": "returns"})], "tool_use"),
        _say("You have 30 days to return a book."),
    ])
    case = evals.normalize_case({
        "name": "policy", "kind": "scripted",
        "turns": [{"user": "return policy?", "expect": {
            "category": "general_policy", "tools_called": ["search_policies"],
            "reply_contains": ["30 days"], "reply_not_contains": ["exception"]}}],
    })

    result = evals.run_case(case, AgentConfig(), None)

    assert result["passed"], result["checks"]
    assert len(result["checks"]) == 4


def test_failed_expectation_and_judge_verdict_fail_the_case(monkeypatch):
    _stub_router(monkeypatch, "out_of_scope")
    monkeypatch.setattr(judges, "grade", lambda turns, criteria: {"passed": False, "reasoning": "nope"})
    case = evals.normalize_case({
        "name": "x", "turns": [{"user": "hi", "expect": {"category": "small_talk"}}], "criteria": "Be nice",
    })

    result = evals.run_case(case, AgentConfig(), None)

    assert not result["passed"]
    assert [c["passed"] for c in result["checks"]] == [False, False]


def test_simulation_runs_until_the_customer_ends(monkeypatch):
    _stub_router(monkeypatch, "small_talk")
    _scripted(monkeypatch, [_say("Hi! How can I help?")])
    lines = iter(["hello", judges.END_TOKEN])
    monkeypatch.setattr(judges, "simulate_customer_message", lambda *a: next(lines))
    monkeypatch.setattr(judges, "grade", lambda turns, criteria: {"passed": True, "reasoning": "fine"})
    case = evals.normalize_case({"name": "sim", "kind": "simulation", "criteria": "greets",
                                 "simulation": {"goal": "say hi"}})

    result = evals.run_case(case, AgentConfig(), None)

    assert result["passed"]
    assert [t["user"] for t in result["transcript"]] == ["hello"]


def test_case_validation():
    with pytest.raises(ValueError):
        evals.normalize_case({"name": "", "turns": [{"user": "x"}]})
    with pytest.raises(ValueError):
        evals.normalize_case({"name": "sim", "kind": "simulation", "simulation": {"goal": "g"}})  # no criteria
    for seed in evals.SEED_CASES:
        assert evals.normalize_case(seed)["id"] == seed["id"]


def test_case_from_conversation_snapshots_category_and_tools(studio_store):
    studio_store.log_turn("c1", "playground", None, {
        "user_text": "return policy?", "reply": "30 days", "category": "general_policy", "latency_ms": 1,
        "tool_calls": [{"name": "search_policies", "input": {}, "result": {}}]})
    case = evals.case_from_conversation("Captured", studio_store.get_conversation("c1")["turns"])
    assert case["turns"][0]["expect"] == {"category": "general_policy", "tools_called": ["search_policies"]}


# -- HTTP ------------------------------------------------------------------------------------------

def test_widget_chat_is_pinned_logged_and_rateable(monkeypatch, studio_store):
    from app.main import app

    _stub_router(monkeypatch, "out_of_scope")
    with TestClient(app) as client:  # lifespan bootstraps v1
        r = client.post("/chat", json={"session_id": "w1", "message": "write code"})
        assert r.status_code == 200
        assert client.post("/feedback", json={"session_id": "w1", "rating": -1}).json() == {"ok": True}
        assert client.post("/feedback", json={"session_id": "nope", "rating": 1}).status_code == 404

        conv = client.get("/api/studio/monitor/conversations/w1").json()
        assert (conv["env"], conv["version_id"], conv["csat"]) == ("production", 1, -1)

        draft = client.get("/api/studio/draft").json()
        draft["config"]["out_of_scope_reply"] = "Books only!"
        assert client.put("/api/studio/draft", json={"config": draft["config"]}).json()["dirty"] is True
        assert "Books only!" in "\n".join(client.get("/api/studio/diff?a=production&b=draft").json()["diff"])
        v2 = client.post("/api/studio/versions", json={"note": "shorter"}).json()["id"]
        assert client.post("/api/studio/deploy", json={"version_id": v2}).status_code == 409  # gate
        assert client.get("/studio").status_code == 200



# -- AOPs at runtime -------------------------------------------------------------------------

def _aop_config():
    return AgentConfig(
        enabled_tools=["get_order_status", "search_policies", "escalate_to_human"],
        aops=[AOP("Escalation request", "asks for a human", "Call @escalate_to_human. Greet {{ customer_name }}.")],
    )


def test_use_aop_loads_steps_and_is_the_audit_trail(monkeypatch):
    _stub_router(monkeypatch, "general_policy")
    _scripted(monkeypatch, [
        FakeResponse([ToolUseBlock("t1", "use_aop", {"name": "Escalation request"})], "tool_use"),
        FakeResponse([ToolUseBlock("t2", "escalate_to_human", {"reason": "wants a person"})], "tool_use"),
        _say("Done -- ticket ESC-12345."),
    ])
    session = agent.Session(session_id="s", config=_aop_config())

    result = agent.run_turn(session, "can I talk to a human?")

    assert result.aops == ["Escalation request"]
    assert "the `escalate_to_human` tool" in result.tool_calls[0]["result"]["steps"]
    assert result.escalated  # a successful escalate_to_human escalates the conversation
    assert session.verified_email is None


def test_unknown_aop_is_an_error_not_a_crash(monkeypatch):
    _stub_router(monkeypatch)
    _scripted(monkeypatch, [
        FakeResponse([ToolUseBlock("t1", "use_aop", {"name": "Nope"})], "tool_use"),
        _say("ok"),
    ])
    result = agent.run_turn(agent.Session(session_id="s", config=_aop_config()), "hi")
    assert result.tool_calls[0]["result"]["error"] == "unknown_aop"
    assert result.aops == []


def test_returns_flow_shows_up_as_the_coded_aop(monkeypatch):
    _stub_router(monkeypatch, "returns_refunds")
    result = agent.run_turn(agent.Session(session_id="s"), "I want to return something")
    assert result.aops == [agent.RETURNS_FLOW_AOP]


def test_verified_customer_metadata_reaches_the_model(monkeypatch):
    _stub_router(monkeypatch)
    client = _scripted(monkeypatch, [
        FakeResponse([ToolUseBlock("t1", "get_order_status",
                                   {"order_id": "BK-1001", "email": "alice@bookly-demo.com"})], "tool_use"),
        _say("ok"),
    ])
    session = agent.Session(session_id="s", config=_aop_config(), metadata_overrides={"customer_type": "new"})

    agent.run_turn(session, "BK-1001, alice@bookly-demo.com")

    meta = session.metadata()
    assert meta["customer_name"] == "Alice"
    assert meta["order_count"] == 2
    assert meta["customer_type"] == "new"  # Preview's test metadata wins
    context = [m for m in session.messages if m["role"] == "system"][-1]["content"]
    assert '"customer_name": "Alice"' in context


# -- Studio API: AOPs, tools, convos, watchtower ---------------------------------------------------

def _client():
    from app.main import app
    return TestClient(app)


def test_aop_status_tracks_the_release(monkeypatch, studio_store):
    with _client() as client:
        aops = client.get("/api/studio/aops").json()["aops"]
        statuses = {a["name"]: a["status"] for a in aops}
        assert statuses == {"Returns & refunds": "draft", "Escalation request": "draft", "Lost package": "draft"}
        removals = client.get("/api/studio/aops").json()["pending_removals"]
        assert removals == [f"{agent.RETURNS_FLOW_AOP} -- replaced by Returns & refunds"]

        # Release the draft, then edit one AOP: it becomes "update".
        cfg = studio_store.get_draft()["config"]
        v2 = deploy.publish_draft("AOPs")
        _pass_full_suite(studio_store, cfg)
        deploy.deploy(v2)
        lost = next(a for a in aops if a["name"] == "Lost package")
        r = client.put(f"/api/studio/aops/{lost['id']}", json={
            "name": "Lost package", "when": lost["when"], "logic": lost["logic"] + "\n6. Thank them.",
            "author": "pat@bookly"})
        assert r.status_code == 200
        assert (r.json()["status"], r.json()["updated_by"], r.json()["created_by"]) == ("update", "pat@bookly", "Patrick Phrayme")
        statuses = {a["name"]: a["status"] for a in client.get("/api/studio/aops").json()["aops"]}
        assert statuses["Escalation request"] == "live"


def test_broken_reference_can_be_drafted_but_not_published(studio_store):
    with _client() as client:
        r = client.post("/api/studio/aops", json={"name": "Typo", "when": "w", "logic": "use @not_a_tool"})
        assert r.status_code == 200 and r.json()["problems"] == ["@not_a_tool isn't a tool the agent can call"]
        r = client.post("/api/studio/versions", json={"note": "oops"})
        assert r.status_code == 400 and "not_a_tool" in r.json()["detail"]


def test_tools_registry_and_toggle(studio_store):
    with _client() as client:
        tools = {t["name"]: t for t in client.get("/api/studio/tools").json()}
        assert tools["initiate_return"]["used_by"] == ["Returns & refunds"]  # the AOP, not the coded flow
        assert "30-day" in tools["initiate_return"]["enforces"]
        assert tools["escalate_to_human"]["used_by"] == ["Escalation request"]  # Returns reaches it via #Escalation request
        assert tools["list_orders_for_customer"]["used_by"] == []
        assert client.put("/api/studio/tools/drop_tables", json={"enabled": True}).status_code == 404
        tools = {t["name"]: t for t in client.put("/api/studio/tools/escalate_to_human", json={"enabled": False}).json()}
        assert tools["escalate_to_human"]["enabled"] is False
        # The AOP that references it is now flagged.
        aops = client.get("/api/studio/aops").json()["aops"]
        assert next(a for a in aops if a["name"] == "Escalation request")["problems"]


def test_human_review_and_sampling(studio_store):
    for i in range(5):
        studio_store.log_turn(f"c{i}", "production", 1, {"user_text": "hi", "reply": "hello", "latency_ms": 5})
    with _client() as client:
        ids = client.post("/api/studio/convos/sample", json={"size": 3}).json()["conversation_ids"]
        assert len(set(ids)) == 3
        r = client.put("/api/studio/convos/c1/review", json={
            "review": {"assignee": "sam", "issue_status": "resolved", "fixed": "yes", "feedback": "fine"},
            "reviewer": "pat"})
        assert r.json()["reviewed_by"] == "pat"
        assert client.put("/api/studio/convos/c1/review", json={"review": {"fixed": "maybe"}}).status_code == 400
        conv = client.get("/api/studio/convos/c1").json()
        assert conv["review"]["issue_status"] == "resolved" and conv["tags"] == ["Deflected"]
        open_ids = [c["id"] for c in client.get("/api/studio/convos?status=needs_review&window=all").json()]
        assert "c1" not in open_ids and "c0" in open_ids


def test_watchtower_analysis_flags_and_summarizes(monkeypatch, studio_store):
    from app.studio import watchtower

    studio_store.log_turn("c1", "production", 1, {"user_text": "this is ridiculous", "reply": "sorry", "latency_ms": 5})
    studio_store.log_turn("c2", "production", 1, {"user_text": "thanks!", "reply": "you're welcome", "latency_ms": 5})
    watchtower.ensure_seeded()
    wts = studio_store.list_watchtowers()
    neg = wts[0]

    monkeypatch.setattr(judges, "review_conversation", lambda turns: {
        "summary": "s", "resolution_summary": "r", "topics": ["Other"], "resolution": "resolved",
        "score": 4, "flags": [], "suggested_fix": ""})
    monkeypatch.setattr(judges, "evaluate_watchtowers", lambda turns, monitors: {
        m["id"]: {"flagged": m["id"] == neg["id"] and "ridiculous" in turns[0]["user"],
                  "label": "Angry", "reason": "said ridiculous"} for m in monitors})

    watchtower.analyze_conversation("c1")
    watchtower.analyze_conversation("c2")

    summary = next(s for s in watchtower.summaries(["production"], "all") if s["id"] == neg["id"])
    assert (summary["analyzed"], summary["flagged"], summary["flagged_rate"]) == (2, 1, 50.0)
    assert summary["by_label"] == {"Angry": 1} and summary["unanalyzed"] == 0
    assert [c["id"] for c in watchtower.flagged_conversations(neg["id"], ["production"], "all")] == ["c1"]

    # A new turn makes the conversation stale again.
    studio_store.log_turn("c2", "production", 1, {"user_text": "actually I'm angry", "reply": "sorry", "latency_ms": 5})
    assert next(s for s in watchtower.summaries(["production"], "all") if s["id"] == neg["id"])["unanalyzed"] == 1


def test_watchtower_validation():
    from app.studio import watchtower

    with pytest.raises(ValueError):
        watchtower.normalize({"name": "x", "criteria": "y", "labels": ["a", "b", "c", "d"]})
    with pytest.raises(ValueError):
        watchtower.normalize({"name": "", "criteria": "y"})


def test_store_recreates_a_deleted_database_file(tmp_path):
    import os

    store = Store(tmp_path / "studio.db")
    set_store(store)
    seeded = []
    store.on_recreate = lambda: (seeded.append(True), deploy.ensure_initialized([]))
    deploy.ensure_initialized([])
    os.remove(tmp_path / "studio.db")

    # Without the reopen, this write fails: "attempt to write a readonly database".
    store.log_turn("c1", "production", 1, {"user_text": "hi", "reply": "hello", "latency_ms": 5})

    assert seeded == [True]
    assert store.current_deployment()["production_version_id"] == 1
    assert store.get_conversation("c1")["turns"][0]["reply"] == "hello"


def test_escalation_cant_route_around_an_ineligible_return(monkeypatch):
    from app import tools

    tools.ESCALATIONS.clear()
    session = agent.Session(session_id="s", config=_aop_config())
    _stub_router(monkeypatch, "returns_refunds")
    agent.run_turn(session, "I want to return something from BK-1004, dave@bookly-demo.com")
    assert session.ineligible_return_orders == {"BK-1004"}

    _stub_router(monkeypatch, "general_policy")
    _scripted(monkeypatch, [
        FakeResponse([ToolUseBlock("t1", "escalate_to_human",
                                   {"reason": "Customer wants an exception to the 30-day window"})], "tool_use"),
        _say("I'm sorry, that return isn't eligible."),
    ])
    result = agent.run_turn(session, "Can a manager make an exception?")

    assert result.tool_calls[0]["result"]["error"] == "escalation_not_allowed"
    assert not result.escalated and tools.ESCALATIONS == []

    # An unrelated escalation in the same conversation still goes through.
    _scripted(monkeypatch, [
        FakeResponse([ToolUseBlock("t2", "escalate_to_human", {"reason": "Customer can't log in to their account"})], "tool_use"),
        _say("Done."),
    ])
    assert agent.run_turn(session, "also I can't log in, can a person help").escalated



# -- returns as a forced AOP ------------------------------------------------------------------

def _returns_aop_config():
    return AgentConfig(
        enabled_tools=["get_order_status", "search_policies", "initiate_return", "generate_return_label"],
        aops=[AOP("Returns & refunds", "wants a return", "1. Check @get_order_status\n2. File with @initiate_return",
                  force_category="returns_refunds")],
    )


def test_forced_aop_replaces_the_coded_returns_flow(monkeypatch):
    _stub_router(monkeypatch, "returns_refunds")
    client = _scripted(monkeypatch, [_say("Sure -- which order is it?"), _say("Which book?")])
    session = agent.Session(session_id="s", config=_returns_aop_config())

    result = agent.run_turn(session, "I want to return a book")

    assert (result.route, result.aops) == ("agent_loop", ["Returns & refunds"])
    assert session.return_flow is None  # the coded state machine never ran
    forced = result.tool_calls[0]
    assert forced["name"] == "use_aop" and forced["input"] == {"name": "Returns & refunds", "forced": True}
    note = [m for m in client.messages.calls[0]["messages"] if m["role"] == "system"]
    assert len(note) == 1 and "File with the `initiate_return` tool" in note[0]["content"]
    assert {"initiate_return", "generate_return_label"} <= {t["name"] for t in client.messages.calls[0]["tools"]}

    # Loaded once per conversation; later returns turns still credit it.
    second = agent.run_turn(session, "BK-1001")
    assert second.aops == ["Returns & refunds"]
    assert [m["role"] for m in session.messages].count("system") == 1


def test_default_config_still_uses_the_coded_returns_flow(monkeypatch):
    _stub_router(monkeypatch, "returns_refunds")
    result = agent.run_turn(agent.Session(session_id="s"), "I want to return something")
    assert (result.route, result.aops) == ("returns_flow", [agent.RETURNS_FLOW_AOP])


def test_forced_aop_reaches_models_without_system_messages(monkeypatch):
    _stub_router(monkeypatch, "returns_refunds")
    client = _scripted(monkeypatch, [_say("ok")])
    monkeypatch.setattr(agent, "_NO_SYSTEM_MESSAGE_MODELS", {AgentConfig().agent_model})
    agent.run_turn(agent.Session(session_id="s", config=_returns_aop_config()), "return please")
    call = client.messages.calls[0]
    assert "File with the `initiate_return` tool" in call["system"]
    assert all(m["role"] != "system" for m in call["messages"])


def test_only_one_aop_can_be_forced_per_category():
    with pytest.raises(ValueError, match="only one"):
        AgentConfig(aops=[AOP("A", "w", "l", force_category="returns_refunds"),
                          AOP("B", "w", "l", force_category="returns_refunds")]).validate(check_refs=False)
    with pytest.raises(ValueError):
        AgentConfig(aops=[AOP("A", "w", "l", force_category="small_talk")]).validate(check_refs=False)


def test_agent_loop_return_guardrails_hold(monkeypatch):
    _stub_router(monkeypatch, "returns_refunds")
    _scripted(monkeypatch, [
        FakeResponse([ToolUseBlock("t1", "initiate_return", {
            "order_id": "BK-1004", "order_item_id": "OI-10041", "email": "dave@bookly-demo.com", "reason": "x"})], "tool_use"),
        _say("Sorry, that's outside the window."),
    ])
    session = agent.Session(session_id="s", config=_returns_aop_config())
    result = agent.run_turn(session, "return Sapiens from BK-1004, dave@bookly-demo.com")
    assert result.tool_calls[-1]["result"]["error"] == "not_eligible"  # the tool, not the model, decides
    assert session.ineligible_return_orders == {"BK-1004"}  # so escalation can't route around it


def test_initiate_return_is_idempotent_per_item():
    from app import tools

    args = dict(order_id="BK-1001", order_item_id="OI-10011", email="alice@bookly-demo.com", reason="x")
    first = tools.initiate_return(**args)
    again = tools.initiate_return(**args)
    assert again["return_id"] == first["return_id"] and again["already_on_file"]
    assert len(mock_snowflake.RETURNS) == 1


def test_seed_tests_upgrade_only_if_untouched(studio_store):
    from app.studio.evals import SEED_CASES, SEED_HISTORY

    old_untouched = {"id": "return-window-expired", "name": "old"}
    old_edited = {"id": "return-not-delivered", "name": "edited by a person"}
    studio_store.upsert_test_case(old_untouched)
    studio_store.upsert_test_case(old_edited)
    original = deploy._case_hash(old_untouched)
    SEED_HISTORY["superseded"]["return-window-expired"] += (original,)
    try:
        deploy._sync_seed_cases(studio_store, SEED_CASES)
    finally:
        SEED_HISTORY["superseded"]["return-window-expired"] = SEED_HISTORY["superseded"]["return-window-expired"][:-1]

    assert studio_store.get_test_case("return-window-expired")["name"] == "Return: outside the 30-day window"
    assert studio_store.get_test_case("return-not-delivered")["name"] == "edited by a person"
    # Existing database: brand-new seeds are added, initial ones aren't resurrected.
    assert studio_store.get_test_case("damaged-item-end-to-end") is not None
    assert studio_store.get_test_case("prompt-injection") is None

    studio_store.delete_test_case("damaged-item-end-to-end")
    deploy._sync_seed_cases(studio_store, SEED_CASES)
    assert studio_store.get_test_case("damaged-item-end-to-end") is None  # a deleted seed stays deleted


def test_escalation_guardrail_learns_from_order_status_too(monkeypatch):
    # A model can skip initiate_return once get_order_status shows the window
    # has closed; the guardrail must still know the return is ineligible.
    _stub_router(monkeypatch, "returns_refunds")
    cfg = _returns_aop_config()
    cfg.enabled_tools = cfg.enabled_tools + ["escalate_to_human"]
    _scripted(monkeypatch, [
        FakeResponse([ToolUseBlock("t1", "get_order_status", {"order_id": "BK-1004", "email": "dave@bookly-demo.com"})], "tool_use"),
        FakeResponse([ToolUseBlock("t2", "escalate_to_human", {"reason": "Manager review of an exception for BK-1004"})], "tool_use"),
        _say("Sorry, I can't escalate that."),
    ])
    session = agent.Session(session_id="s", config=cfg)
    result = agent.run_turn(session, "BK-1004, dave@bookly-demo.com -- get me a manager for an exception")
    assert session.ineligible_return_orders == {"BK-1004"}
    assert result.tool_calls[-1]["result"]["error"] == "escalation_not_allowed"
    assert not result.escalated


# -- per-AOP tool scoping ------------------------------------------------------------------------

def _scoped_config():
    cfg = _aop_config()  # Escalation request AOP references @escalate_to_human
    cfg.aop_scoped_tools = ["escalate_to_human"]
    return cfg


def test_scoped_tool_needs_its_aop_loaded_first(monkeypatch):
    from app import tools

    tools.ESCALATIONS.clear()
    _stub_router(monkeypatch, "general_policy")
    client = _scripted(monkeypatch, [
        FakeResponse([ToolUseBlock("t1", "escalate_to_human", {"reason": "wants a person"})], "tool_use"),
        FakeResponse([ToolUseBlock("t2", "use_aop", {"name": "Escalation request"})], "tool_use"),
        FakeResponse([ToolUseBlock("t3", "escalate_to_human", {"reason": "wants a person"})], "tool_use"),
        _say("Ticket ESC-1."),
    ])
    session = agent.Session(session_id="s", config=_scoped_config())

    result = agent.run_turn(session, "can I talk to a person?")

    first, _, third = [c["result"] for c in result.tool_calls]
    assert first["error"] == "aop_required" and '"Escalation request"' in first["message"]
    assert third["success"] and len(tools.ESCALATIONS) == 1  # only the call made inside the AOP went through
    assert result.aops == ["Escalation request"] and result.escalated
    desc = next(t for t in client.messages.calls[0]["tools"] if t["name"] == "escalate_to_human")["description"]
    assert "Only available while following an AOP that uses it (Escalation request)" in desc


def test_unscoped_tools_stay_available_everywhere(monkeypatch):
    _stub_router(monkeypatch)
    _scripted(monkeypatch, [
        FakeResponse([ToolUseBlock("t1", "search_policies", {"query": "shipping"})], "tool_use"),
        _say("4-6 days."),
    ])
    result = agent.run_turn(agent.Session(session_id="s", config=_scoped_config()), "shipping time?")
    assert "error" not in result.tool_calls[0]["result"]


def test_forced_aop_unlocks_its_scoped_tools(monkeypatch):
    _stub_router(monkeypatch, "returns_refunds")
    cfg = _returns_aop_config()
    cfg.aop_scoped_tools = ["initiate_return"]
    _scripted(monkeypatch, [
        FakeResponse([ToolUseBlock("t1", "initiate_return", {
            "order_id": "BK-1001", "order_item_id": "OI-10011", "email": "alice@bookly-demo.com", "reason": "x"})], "tool_use"),
        _say("Filed."),
    ])
    result = agent.run_turn(agent.Session(session_id="s", config=cfg), "return The Hobbit from BK-1001, alice@bookly-demo.com")
    assert result.tool_calls[-1]["result"]["success"]


def test_empty_scope_keeps_existing_fingerprints():
    cfg = AgentConfig()
    d = cfg.to_dict()
    del d["aop_scoped_tools"]
    assert AgentConfig.from_dict(d).fingerprint() == cfg.fingerprint()
    assert AgentConfig(aop_scoped_tools=["escalate_to_human"]).fingerprint() != cfg.fingerprint()


def test_tool_scope_toggle_via_api(studio_store):
    with _client() as client:
        tools = {t["name"]: t for t in client.get("/api/studio/tools").json()}
        assert tools["escalate_to_human"]["aop_only"] and not tools["get_order_status"]["aop_only"]
        tools = {t["name"]: t for t in client.put("/api/studio/tools/escalate_to_human", json={"aop_only": False}).json()}
        assert not tools["escalate_to_human"]["aop_only"] and tools["escalate_to_human"]["enabled"]
