# Bookly Support Agent

A customer support agent for Bookly (a fictional online bookstore). It
handles order status, returns, and general policy questions through a small
web chat, with Claude driving tool calls against a Snowflake-shaped data
backend. The orchestration is hand-rolled rather than built on an agent
framework: `app/agent.py` is a plain tool-use loop for order status / policy
/ small talk, and `app/returns_flow.py` is a deterministic state machine (no
model call) for returns. See "Key decisions" below for why those two paths
differ.

## Setup

```bash
cp .env.example .env             # fill in ANTHROPIC_API_KEY -- the only required value
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload    # open http://localhost:8000
```

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
  `initiate_return`, `generate_return_label`, `search_policies`.
  `search_policies` queries a **Cortex Search** service so it matches a
  customer's own phrasing to the right policy by meaning, not keyword.
  `generate_return_label` hits a separate mocked shipping-carrier API and
  its `refund_note` makes clear the refund happens on receipt, not at label
  creation.
- **Memory**: an in-memory per-session message list (`Session` in
  `app/agent.py`) -- free recall within one conversation, nothing persists
  across sessions. Deliberately out of scope for this proof of concept; see
  the pitch deck's "what I'd change."
- **Guardrails**: identity verification, the 30-day return window, topic
  scope, and the returns flow's step order are all enforced in code
  (`app/tools.py`, `app/intent_router.py`, `app/returns_flow.py`), not left
  to the model's judgment. The system prompt repeats the scope rule as a
  backstop, but the classifier is the real gate.

## Key decisions

**Deterministic returns, not model-driven.** Every other in-scope category
runs through the flexible tool loop, where Claude decides which tool to
call and when. Returns don't -- `app/returns_flow.py` runs a fixed sequence
in code, because returns are the one flow that touches identity, a fixed
eligibility rule, and (once a label's generated) money, where "executed
correctly every time" matters more than flexible phrasing. Order IDs and
emails are fixed formats (regex) and matching a reply to a line item is a
short substring/ordinal check -- not enough real ambiguity to need a model
in the loop. See the pitch deck for the full tradeoff.

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
  main.py              FastAPI app (/, /chat, /health)
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
  static/              Single-page chat UI (HTML/CSS/vanilla JS)
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
```
