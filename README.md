# Bookly Support Agent

A customer support agent for Bookly (a fictional online bookstore). It
handles order status, returns, and general policy questions through a small
web chat, with Claude driving tool calls against a Snowflake-shaped data
backend. The orchestration is hand-rolled rather than built on an agent
framework: `app/agent.py` is a plain tool-use loop for order status / policy
/ small talk, and `app/returns_flow.py` is a deterministic state machine (no
model call) for returns. See "Key decisions" below for why those two paths
differ.

On top of the agent sits **Agent Studio** (`/studio`), a Decagon-style
operator console for building, testing, deploying, and monitoring it:
natural-language AOPs, a release gate backed by a test suite, canary
deploys, conversation review, Watchtower monitors, and GitHub sync that
keeps the agent's configuration in a repo as code. Released versions run
returns through a forced *Returns & refunds* AOP instead of the coded flow.

## Setup

```bash
cp .env.example .env             # fill in ANTHROPIC_API_KEY -- the only required value
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload    # open http://localhost:8000
```

The server and diagnostic scripts read `.env` from this project's root.
`ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` in that file take precedence
over values exported in your terminal (Claude Code also uses
`ANTHROPIC_MODEL`, so a terminal can carry a different one without you
noticing). Without a value in `.env`, the environment's is used. After
changing either, fully stop and restart Uvicorn.

Runs with zero Snowflake setup -- it talks to an in-memory mock
(`app/mock_snowflake.py`) that mirrors the real schema/seed data/Cortex
Search content exactly. To run tests:

```bash
pip install -r requirements-dev.txt
python3 -m pytest -v
```

Tests run against the mock backend and a scripted fake Claude client
(`tests/fake_anthropic.py`) -- no credentials needed. Separately,
`scripts/eval_intent_router.py` evaluates the scope-gate classifier
against the *real* model (`pytest` stubs `classify_intent`, so it can't
catch the classifier itself being wrong); run it after any change to
`app/intent_router.py`'s system prompt.

**Optional: real Snowflake.** Run `snowflake/01_schema.sql`,
`02_seed_data.sql`, and `03_cortex_search.sql` against a `BOOKLY.SUPPORT`
schema (any account, e.g. a [free trial](https://signup.snowflake.com/)).
Then generate a key pair (`./scripts/generate_agent_keypair.sh`), paste the
public key into `snowflake/04_agent_access.sql` and run it as
`ACCOUNTADMIN` -- this creates the least-privilege service identity the
app actually runs as (see [Key decisions](#key-decisions)). Finally set
`BOOKLY_DATA_BACKEND=live` plus the `SNOWFLAKE_*` variables in `.env`.

## Demo script

Each scenario starts from a fresh browser tab (a new tab = a new session =
no memory of earlier scenarios) except 4a, which continues 3's conversation
in the same tab. "Type:" is exactly what to type into the widget. Turn on
**Dev: show tool calls** in the header to see the real tool call (name,
input, result) behind every reply below.

1. **Order status.**
   - Type: `Where's my order?`
   - Type: `BK-1002, bob@bookly-demo.com`
   - Agent reports it's still in transit, with a tracking number.
2. **Clarifying question (ambiguous return).**
   - Type: `I'd like to return something from order BK-1002, my email is bob@bookly-demo.com`
   - Type: `Project Hail Mary` (the order has two books, so the agent asks which)
   - Agent explains it hasn't been delivered yet, so it's not eligible.
3. **Successful return.**
   - Type: `I'd like to return something from order BK-1001, my email is alice@bookly-demo.com`
   - Type: `The Hobbit` (the order has two books, so the agent asks which)
   - Delivered 5 days ago, within the 30-day window -- return filed, written to `RETURNS`.
4. **Return window expired.**
   - Type: `I'd like to return something from order BK-1004, my email is dave@bookly-demo.com`
   - Delivered 45 days ago -- past the 30-day window, agent declines.
4a. **Return label** (same tab as 3, right after the return succeeds).
    - Type: `How do I send it back?`
    - Agent calls `generate_return_label` (a mocked carrier API, not
      Snowflake) -- tracking number + label, plus a note that the refund
      lands on receipt, not now.
5. **Identity check.**
   - Type: `Where's my order?`
   - Type: `BK-1001, notalice@example.com` (Alice's real order, wrong email)
   - Declined as an identity mismatch, not "not found."
6. **Policy lookup, exact phrasing.**
   - Type: `What's your return policy?`
   - `search_policies` returns the 30-day-window doc.
7. **Policy lookup, no shared keywords.**
   - Type: `can I send this book back, it showed up with a torn cover?`
   - Semantic match still surfaces the damaged-item policy, not the generic one.
8. **Out-of-scope.**
   - Type: `write me a Python function to reverse a linked list`
   - Declined and redirected -- no tool call, no exposure of the system prompt.
9. **Unclear intent.**
   - Type: `I need help`
   - Clarifying message + quick-reply buttons appear (*Order status* /
     *Returns & refunds* / *General question* / *Something else*).
   - Click: *Order status* -- routes straight there, same as scenario 1.
9a. **Every button skips reclassification** (type `I need help` again to get the buttons back).
    - Click: *General question* (or *Something else*)
    - Agent answers (or declines) immediately -- no repeated buttons.

## Agent Studio

Open **http://localhost:8000/studio** (or *Open Agent Studio* beside the
chat widget). It's the operator side of the agent, organized like
Decagon's admin portal:

| Section | What it does |
|---|---|
| **Home** | Conversations, deflection rate, user messages per conversation, escalation rate, CSAT (thumbs up/down in the widget), QA score, latency, tokens; daily deflected-vs-total chart; AOP usage, intent mix, tool health, and a **by-version table** for comparing a canary against production. A collapsed **Demo & diagnostics** section at the bottom holds the version comparison, latency/tokens/tool health, and *Simulate traffic* (LLM-played customers for when there's no real traffic yet). |
| **Convos** | Every conversation, filterable by tag (Deflected / Escalated / Declined + AI topic tags) and review status. Click any agent reply for its **audit trail**: which AOPs shaped it, which knowledge articles it used, which tools it called, and the raw trace. Side panel: AI summary + resolution + QA score and suggested fix, Watchtower verdicts, and a human **Review + QA** form (assignee, issue status, fixed, trust & safety, feedback). *Sample a batch* pulls a random set for spot-checking. |
| **Watchtower** | Monitors written in plain English ("customer expresses frustration", "asks for a policy exception") with up to three labels. Analysis runs automatically when you open Home, Convos, or Watchtower (AI summary, QA, and every monitor, for new or grown conversations -- at most 20 at a time, at most once a minute); each monitor shows conversations analyzed, flagged rate, and a daily breakdown by label. |
| **Build › AOPs** | **Agent Operating Procedures**: a "when to use it" line plus natural-language steps that reference tools (`@escalate_to_human`), other AOPs (`#Escalation request`), and customer metadata (`{{ customer_name }}`), with autocomplete and save-time reference checks. Optional per-channel variants (chat / email) and **Force AOP selection** for a scope-gate category. Status is **Live / Update / Draft** relative to production. A config with no AOP forced for returns falls back to the built-in coded returns flow, which then appears in the list as locked. |
| **Build › Tools** | The coded tools (`app/tools.py`), each with the system it talks to (Snowflake, a carrier REST API, Freshdesk), whether any AOP uses it, what it enforces in code, on/off, and **scope** (always available, or only inside AOPs that reference it). **+ New tool** shows the connected systems and an illustrative integration catalog (Shopify, Stripe, Zendesk, an MCP server, …); connecting new integrations isn't wired up. |
| **Build › Settings** | Base system prompt, scope-gate prompt, decline message, models (grouped by family; models that can't do the scope gate's forced tool call are greyed out there), limits. |
| **Build › Releases** | Publish the draft as an immutable version, diff versions, and deploy. A **release gate** blocks a version until a full test-suite run of its exact config (by fingerprint) passes; overriding requires a reason, which is recorded. **Canary** to 1-50% of new conversations, then promote, abort, or roll back. Versions that aren't serving traffic can be redeployed or deleted. **Connect GitHub** keeps the agent in a repo (below). |
| **Tests** | Scripted tests (per-turn assertions on category, route, tools, reply, quick-reply buttons) and **simulations** (an LLM customer with a persona and goal; an LLM judge grades the transcript). Any conversation can be saved as a regression test. |
| **Preview** | Chat with the draft or any version, on **chat or email**, with **test metadata** (e.g. `customer_type = new`) to exercise `{{ }}` references. |

**How AOPs run.** The system prompt lists each released AOP's name and
"when to use it"; the agent calls a `use_aop` tool to load an AOP's steps
(with `@`/`#` references resolved), so prompts stay small as AOPs grow and
`use_aop` calls double as the audit trail. An AOP *forced* for a category
skips the choice: when the scope gate says `returns_refunds`, the turn
starts with *Returns & refunds* already loaded. Customer metadata arrives as an
appended system message once a tool verifies the customer, never by
editing the system prompt mid-conversation (see Key decisions).

On first start, the config in code becomes **v1** and goes straight to
production, so behavior is unchanged until you release something (v1
still runs returns through the coded flow). The draft starts with three
AOPs, all unreleased: *Returns & refunds* (six steps, forced for returns,
replacing the coded flow), *Lost package*, and
*Escalation request* (which uses a new mocked `escalate_to_human` tool),
plus a scope-gate tweak so "can I talk to a person?" and "please send it
again" reach them.

**Studio demo (about 10 minutes): build, test, deploy, and watch a new AOP.**

The demo adds an *Order cancellation* AOP. Its text and the Preview
messages are in collapsed **Demo** dropdowns at the bottom of the new-AOP
page and of Preview. Before you start: *Simulate traffic* (Home › Demo &
diagnostics) so the dashboards have data, and **Connect GitHub** on
Releases if you want the pull-request part.

1. **Tour**: Home (deflection, CSAT, top AOPs), Convos (click a reply for
   its audit trail), Watchtower, and Build › AOPs (open *Returns &
   refunds*: `@tool`, `#AOP`, and `{{ metadata }}` chips, forced for the
   returns category).
2. **Before**: in **Preview**, chatting with the live version, send "Can I
   cancel order BK-1002? bob@bookly-demo.com". With no cancellation
   procedure, the agent can only suggest a return later.
3. **Build**: Build › AOPs › New AOP. Name *Order cancellation*, "when":
   "The customer wants to cancel an order.", and the steps (type `@` and
   `#` to show autocomplete):
   ```
   1. get the sender's order ID and email address
   2. check the order's status with @get_order_status
   a. If it's still processing, it hasn't left our warehouse: follow #Escalation request so the team cancels it.
   b. If it has shipped, it can't be cancelled. Tell them they can refuse the delivery when it arrives, and the refund is issued once it's back with us -- or they can return it within 30 days of delivery.
   ```
   Save draft.
4. **Preview** on the draft: the BK-1002 message again (shipped: refuse
   the delivery or return it), then, in a new conversation, "I want to
   cancel my order BK-1003, my email is carol@bookly-demo.com" (still
   processing: a ticket through *Escalation request*). *Save as test…*
   turns a good conversation into a regression test.
5. **Tests**: run the full suite on the draft (about 35 seconds).
6. **Releases**: *View changes*, then *Publish version…*. With GitHub
   connected this opens a pull request: show the change summary, the AOP
   diff, and the two checks (the release gate, green because the suite
   passed on this exact config, and the repo's reference validator).
   *Deploy…* (canary, then promote, or full): the PR is merged and tagged.
7. **Live**: open the widget in a new tab, send the BK-1002 message, then
   find the conversation in **Convos** with *Order cancellation* in its
   audit trail.

If anyone asks why step 3a escalates: in production it would call a
`cancel_order` tool (for example from a Shopify integration), with the
"only while processing" rule enforced in code, the way returns already
work.

Design notes:

- **AOPs for the conversation, code for the guarantees.** No AOP or model
  choice can file an ineligible return: `initiate_return` re-checks
  identity, the item, delivery, and the 30-day window itself, and is
  idempotent per item; `generate_return_label` only works for a return on
  file; `escalate_to_human` refuses to escalate a return already shown to
  be ineligible. `_execute_tool` refuses any tool the session's config
  hasn't enabled.
- **Releases are whole-config, status is per-AOP.** Each AOP's
  Live/Update/Draft status comes from diffing the draft against
  production, while a release ships and gates the whole config, so the
  tested thing is exactly the deployed thing.
- **Conversations are pinned** to the version (and channel) they started
  with; a stable hash bucket decides canary membership.
- **One code path.** The widget, Preview, tests, and simulated traffic all
  run through `app/studio/runtime.py:execute_turn`. A crash inside a turn
  becomes a logged escalation, not a 500.
- **Storage** is one SQLite file (`data/studio.db`, gitignored, migrated
  on startup). In production the conversation log would go to the
  warehouse and the Studio would sit behind SSO; it has no auth today.
- **Costs**: tests, analysis, and simulated traffic make real model
  calls. The judge and simulator default to `ANTHROPIC_MODEL` (override
  with `BOOKLY_JUDGE_MODEL` / `BOOKLY_SIMULATOR_MODEL`).

### GitHub sync (config as code)

**Connect GitHub** on the Releases page links a repo (needs the GitHub
CLI, logged in with `gh auth login`; `BOOKLY_AGENT_REPO` in `.env`
pre-fills it). Studio stays the editor; the repo records what shipped, and
the Live card shows whether `main` matches production:

| In Studio | In GitHub |
|---|---|
| Connect | Production version committed to `main`, tagged `agent-vN` |
| Publish version | Branch `agent/vN` and a pull request with a change summary |
| Release gate | A **Bookly Studio / release gate** commit status, updated when a full test run finishes |
| Canary / abort | A comment on the PR |
| Deploy to 100% / promote | PR squash-merged, tagged `agent-vN`, branch deleted |
| Roll back | The older version committed to `main` |
| Delete a version | Its open PR is closed and its branch deleted |
| Pull main into draft | Edits made on `main` outside Studio come back into the draft |

The repo is readable on its own: `agent/agent.yaml`, `agent/prompts/*.md`,
one Markdown file per AOP, and `tests/*.yaml`. Export and import
round-trip exactly (same fingerprint), so a run that passed in Studio
counts for what's in git. The repo's own GitHub Action
(`scripts/validate.py`) checks AOP references on every PR. A GitHub
failure never blocks a publish or deploy: Studio finishes the release and
shows the error. Code: `app/studio/gitsync.py`, templates in
`app/studio/repo_template/`.

## Architecture at a glance

```
 Browser (static HTML/JS chat)
        │  POST /chat {session_id, message}
        ▼
 FastAPI (app/main.py)
        │
        ▼
 Scope gate (app/intent_router.py)
        │
        ├─ out_of_scope / unclear  ──▶  fixed reply or quick-reply buttons -- nothing below ever runs
        │
        ├─ returns_refunds  ──▶  Deterministic flow (app/returns_flow.py) -- code decides every step, no model call
        │
        └─ order_status / general_policy / small_talk
                 │
                 ▼
          Agent loop (app/agent.py)  ──calls──▶  Claude Messages API (tool use)
                 │        ▲
                 │ executes tool_use blocks
                 ▼        │
          Tools (app/tools.py)  ◀── both the agent loop above and the deterministic flow call in here
                 │
                 ├──▶ app/data_backend.py ──▶ app/mock_snowflake.py   (default)
                 │                        └──▶ app/snowflake_client.py (BOOKLY_DATA_BACKEND=live)
                 │                               ├─SQL──▶ Snowflake (orders, order_items, returns, customers)
                 │                               └─Cortex Search──▶ Snowflake (policy_docs)
                 └──▶ generate_return_label ──▶ a mocked shipping-carrier API (neither backend above)
```

- **Scope gate**: `app/intent_router.py` classifies every message into one
  of six outcomes *before* any orchestration runs. `out_of_scope` gets a
  fixed decline, no tool calls, no exposure of the system prompt. `unclear`
  (a genuinely vague message) gets a clarifying reply plus quick-reply
  buttons, each carrying its own known category so a click routes straight
  there (`run_turn_for_category`) without a second classifier call. The
  classifier is a forced single tool call on a small/fast model and fails
  *closed* to `out_of_scope`, never `unclear`, on a malformed response.
- **Tools**: `app/tools.py` -- `get_order_status`, `list_orders_for_customer`,
  `initiate_return`, `generate_return_label`, `search_policies`, and
  `escalate_to_human` (opens a ticket in a mocked helpdesk).
  `search_policies` queries a **Cortex Search** service so it matches a
  customer's own phrasing to the right policy by meaning, not keyword.
  `generate_return_label` hits a separate mocked shipping-carrier API and
  its `refund_note` makes clear the refund happens on receipt, not at label
  creation.
- **Memory**: `Session` in `app/agent.py` keeps the transcript plus the latest
  tool-verified email/order pair. Both the model and the returns flow reuse
  those details across topic changes; a new return fetches the order again
  to check identity and current eligibility. Explicit new details override
  remembered ones, and switching accounts clears the old order. State is
  in memory only and does not persist across server restarts or browser sessions.
- **Guardrails**: identity verification, the 30-day return window,
  duplicate returns, escalating ineligible returns, and topic scope are
  all enforced in code (`app/tools.py`, `app/agent.py`,
  `app/intent_router.py`), not left to the model's judgment. The system prompt repeats the scope rule as a
  backstop, but the classifier is the real gate.

## Key decisions

**Returns: from a coded flow to a forced AOP, guarantees in the tools.**
The original build ran returns through a deterministic state machine
(`app/returns_flow.py`), because returns touch identity, a fixed
eligibility rule, and money -- "executed correctly every time" mattered
more than flexible phrasing. That's still what v1 runs. The Studio draft
moves returns to a natural-language *Returns & refunds* AOP that's forced
for the `returns_refunds` category, matching Decagon's model: natural
language for the conversation, code for the critical actions. What made
the coded flow safe moved into the tools (`initiate_return` re-verifies
identity, item, delivery, and the window, and is idempotent;
`escalate_to_human` won't escalate an ineligible return), so the model
can choose the wording and order without being able to break a rule.
The payoff: the AOP can branch -- a damaged book now follows the
damaged-item policy (return filed, no shipping back, no label), which
the coded flow got wrong. The cost: step order is no longer
guaranteed, which the test suite and Watchtower are there to catch.

**Mocked by default, real Snowflake as an opt-in.** `app/tools.py` never
talks to Snowflake directly -- it goes through `app/data_backend.py`, which
picks `app/mock_snowflake.py` (default, in-memory, seeded to match
`snowflake/02_seed_data.sql` exactly) or `app/snowflake_client.py`
(`BOOKLY_DATA_BACKEND=live`), with nothing else in `tools.py` changing
either way. The point of this repo is the architecture, so evaluating it
shouldn't require a Snowflake trial account -- but everything under
`snowflake/*.sql` is real and runnable.

**Least-privilege Snowflake identity.** The agent runs as its own service
identity (`BOOKLY_AGENT_SVC`, key-pair auth, no password), not as a human
account, created by `snowflake/04_agent_access.sql`: `SELECT`-only on
`customers`/`orders`/`order_items`, `SELECT`+`INSERT`-only (no
`UPDATE`/`DELETE`) on `returns`, `USAGE`-only on the Cortex Search service
(no privilege on `policy_docs` itself), a dedicated auto-suspending XS
warehouse with a 30s statement timeout, and nothing else -- no `CREATE`,
`ALTER`, `DROP`. The file ends with a commented verification block to
prove each boundary actually holds.

## Project layout

```
app/
  main.py              FastAPI app (/, /chat, /feedback, /health, /studio)
  agent.py             Flexible-loop orchestration + session state; routes
                       returns_refunds to returns_flow.py instead
  intent_router.py     Scope gate -- classifies every message (incl.
                       out_of_scope/unclear) before either orchestration
                       path runs
  returns_flow.py      Deterministic returns/refunds workflow -- no model
                       call, calls tools.py directly
  tools.py             Tool schemas + implementations (backend-agnostic)
  prompts.py           System prompt (flexible loop only)
  data_backend.py      Picks mock vs. live backend from BOOKLY_DATA_BACKEND
  mock_snowflake.py     In-memory default backend (also used directly by tests)
  snowflake_client.py  Real Snowflake connection (key-pair auth) + query
                       helpers -- used only when BOOKLY_DATA_BACKEND=live
  agent_config.py      AgentConfig + AOPs -- the versionable prompts,
                       models, tools, and AOPs a Session runs with
  config.py            Loads .env (project values win for the API key/model)
  telemetry.py         Per-turn trace of model calls (latency, tokens)
  studio/              Agent Studio backend
    api.py               /api/studio routes
    runtime.py           execute_turn (run + log), canary routing
    deploy.py            Versions, release gate, canary/promote/rollback,
                         seed AOPs
    gitsync.py           GitHub sync: repo format, git, pull requests
    repo_template/       README, validator, and Action written into the repo
    evals.py             Test runner, simulations, starter suite
    judges.py            LLM judge, simulated customer, conversation
                         analysis, Watchtower evaluator
    monitor.py           Dashboard metrics, conversation queries, human review
    watchtower.py        Watchtower monitors + the background analysis job
    store.py             SQLite persistence
  static/              Single-page chat UI (HTML/CSS/vanilla JS)
  static/studio/       Agent Studio UI (vanilla JS; logos.js bundles the
                       integration logos)
snowflake/
  01_schema.sql        DDL for customers/orders/order_items/returns
  02_seed_data.sql     Mock Bookly data
  03_cortex_search.sql policy_docs table + the Cortex Search service over it
  04_agent_access.sql  Least-privilege role/user/warehouse for the agent
scripts/
  generate_agent_keypair.sh  Generates the agent's RSA key pair
  eval_intent_router.py      Live scope-gate eval against the real model
  debug_classify.py          Raw-response diagnostic for the classifier
tests/
  fake_anthropic.py    Scripted stand-in for the Claude client
  test_tools.py        Tool/business-logic unit tests (run against
                       app/mock_snowflake.py)
  test_agent_loop.py   Flexible-loop orchestration tests
  test_returns_flow.py Deterministic returns-flow tests (no fake client --
                       there's no model call to fake)
  test_intent_router.py Scope-gate tests (classifier + routing integration)
  test_studio.py       Studio: config, gate, canary, logging, metrics,
                       test runner (all model calls scripted)
  test_gitsync.py      GitHub sync against a local bare repo and a fake
                       GitHub API (no network)
  test_config.py       .env precedence
```
