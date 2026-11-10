"""
Versioning and deployment: publish the draft as an immutable version,
gate production on a passing test suite, canary a candidate to a slice of
traffic, promote, roll back.
"""

from __future__ import annotations

from typing import Any

from ..agent_config import AOP, DEFAULT_AUTHOR, AgentConfig
from .store import get_store, now

# The v1 system prompt's paragraph about returns, and what the draft says
# instead once the "Returns & refunds" AOP handles them.
RETURNS_PARAGRAPH_V1 = """Returns
and refunds themselves -- filing one, picking which item, generating a
shipping label -- are handled by a separate, deterministic flow before a
message ever reaches you (app/returns_flow.py), so you don't have
initiate_return or generate_return_label in your tool list and couldn't
act on a return even if asked. If a return-specific request somehow still
reaches you, treat it as a general policy question instead -- explain what
Bookly's return policy says via search_policies -- rather than trying to
process the return yourself."""
RETURNS_PARAGRAPH_AOP = """Returns
and refunds follow the "Returns & refunds" AOP. Eligibility -- identity,
delivery, and the 30-day window -- is enforced by the initiate_return tool
itself: rely on its result, never decide eligibility on your own, and
never file a return the customer hasn't asked for."""

# Example AOPs the draft starts with on first boot -- unreleased, so they
# show as "Draft" until a tested version ships them.
SEED_AOPS = [
    AOP(
        name="Escalation request",
        when="The customer asks for a human or a manager, or needs something none of your tools can do "
             "(e.g. a reshipment). Never for a return that's outside the return window.",
        logic=(
            "1. Acknowledge the request once, without over-apologizing.\n"
            "2. Call @escalate_to_human with a one-sentence reason, plus the customer's email if it's "
            "been verified ({{ email }}).\n"
            "3. Give the customer the ticket ID and say the support team will follow up by email within "
            "one business day.\n"
            "4. Ask if there's anything else you can help with in the meantime."
        ),
        variants={"email": (
            "1. Thank the customer for writing in.\n"
            "2. Call @escalate_to_human with a one-sentence reason and the customer's email ({{ email }}).\n"
            "3. Reply with the ticket ID, say a member of the support team will reply on this thread "
            "within one business day, and sign off as Bookly Support."
        )},
        created_by=DEFAULT_AUTHOR, updated_by=DEFAULT_AUTHOR,
    ),
    AOP(
        name="Returns & refunds",
        when="The customer wants to return a book or get a refund, including a book that arrived damaged.",
        force_category="returns_refunds",
        logic=(
            "1. If the book arrived damaged, look up the damaged-item policy with @search_policies and share it.\n"
            "2. Get the order ID and the email on the account, if you don't have them yet.\n"
            "3. Look up the order with @get_order_status. If it isn't eligible for return (not delivered yet, "
            "or past the 30-day window), explain why and stop.\n"
            "4. If the order has more than one book, ask which one.\n"
            "5. File the return with @initiate_return. Don't ask for a reason -- pass along whatever they said, "
            "or \"No reason given.\" Share the return ID and the refund amount.\n"
            "6. If the book arrived damaged, tell them they don't need to send it back. Otherwise, offer a "
            "prepaid label: unless they say no, create it with @generate_return_label and explain that the "
            "refund is issued once the book arrives back."
        ),
        created_by=DEFAULT_AUTHOR, updated_by=DEFAULT_AUTHOR,
    ),
    AOP(
        name="Lost package",
        when="The customer says an order or package never arrived, is missing, or was lost -- especially "
             "when tracking shows it as delivered.",
        logic=(
            "1. If you don't already have them, ask for the order ID and the email on the account.\n"
            "2. Check the order with @get_order_status.\n"
            "   a. If it isn't marked delivered yet, share the carrier, tracking number and estimated "
            "delivery, and stop here.\n"
            "3. Address the customer as {{ customer_name }}. Apologize once.\n"
            "4. Use @search_policies to look up the policy for late or missing packages, and explain "
            "the options it gives (reship or refund, the customer's choice).\n"
            "5. You can't issue reships or refunds yourself: once the customer picks one, follow "
            "#Escalation request."
        ),
        created_by=DEFAULT_AUTHOR, updated_by=DEFAULT_AUTHOR,
    ),
]


class DeployError(Exception):
    pass


def publish_draft(note: str) -> int:
    store = get_store()
    draft = store.get_draft()
    if draft is None:
        raise DeployError("No draft to publish")
    version_id = store.create_version(draft["config"], note.strip())
    store.save_draft(draft["config"], base_version_id=version_id)
    return version_id


def gate_status(version_id: int) -> dict[str, Any]:
    """Release gate: the version's exact config (by fingerprint) must have
    a completed full-suite test run with zero failures. The run may have
    been against the draft before it was published -- same fingerprint,
    same behavior."""
    store = get_store()
    version = store.get_version(version_id)
    if version is None:
        raise DeployError(f"No version {version_id}")
    run = store.latest_full_run_for_fingerprint(version["fingerprint"])
    if run is None:
        return {"passed": False, "run": None, "reason": "No completed full test-suite run for this exact config."}
    if run["failed"] > 0:
        return {
            "passed": False,
            "run": run,
            "reason": f"Latest full run #{run['id']} had {run['failed']} failing test(s).",
        }
    return {"passed": True, "run": run, "reason": f"Full suite passed in run #{run['id']}."}


def _require_gate(version_id: int, override_reason: str | None) -> str | None:
    gate = gate_status(version_id)
    if gate["passed"]:
        return None
    if override_reason and override_reason.strip():
        return override_reason.strip()
    raise DeployError(f"Release gate failed for v{version_id}: {gate['reason']}")


def deploy(
    version_id: int,
    *,
    mode: str = "full",
    canary_percent: int = 10,
    note: str = "",
    override_reason: str | None = None,
) -> dict[str, Any]:
    store = get_store()
    if store.get_version(version_id) is None:
        raise DeployError(f"No version {version_id}")
    current = store.current_deployment()
    override = _require_gate(version_id, override_reason)

    if mode == "full":
        store.record_deployment(version_id, "deploy", note, gate_override_reason=override)
    elif mode == "canary":
        if current is None:
            raise DeployError("Nothing in production yet -- do a full deploy first.")
        if version_id == current["production_version_id"]:
            raise DeployError(f"v{version_id} is already the production version.")
        if not 1 <= canary_percent <= 50:
            raise DeployError("Canary traffic must be between 1% and 50%.")
        store.record_deployment(
            current["production_version_id"], "canary", note,
            canary_version_id=version_id, canary_percent=canary_percent, gate_override_reason=override,
        )
    else:
        raise DeployError(f"Unknown deploy mode {mode!r}")
    return store.current_deployment()


def promote_canary(note: str = "") -> dict[str, Any]:
    store = get_store()
    current = store.current_deployment()
    if not current or not current["canary_version_id"]:
        raise DeployError("No canary to promote.")
    # The canary already passed (or overrode) the gate when it went out.
    store.record_deployment(current["canary_version_id"], "promote", note)
    return store.current_deployment()


def abort_canary(note: str = "") -> dict[str, Any]:
    store = get_store()
    current = store.current_deployment()
    if not current or not current["canary_version_id"]:
        raise DeployError("No canary to abort.")
    store.record_deployment(current["production_version_id"], "abort_canary", note)
    return store.current_deployment()


def delete_version(version_id: int) -> None:
    """Delete a version that isn't serving traffic. Deploy history keeps its
    entries (they're the audit trail); rollback skips deleted versions."""
    store = get_store()
    if store.get_version(version_id) is None:
        raise DeployError(f"No version {version_id}")
    current = store.current_deployment()
    if current and version_id == current["production_version_id"]:
        raise DeployError(f"v{version_id} is in production. Deploy another version first.")
    if current and version_id == current["canary_version_id"]:
        raise DeployError(f"v{version_id} is the running canary. Abort or promote it first.")
    store.delete_version(version_id)
    draft = store.get_draft()
    if draft and draft["base_version_id"] == version_id:
        store.save_draft(draft["config"], base_version_id=current["production_version_id"] if current else None)


def rollback(note: str = "") -> dict[str, Any]:
    """Back to the previous production version. Never gated: the point of
    a rollback is to get to a known-good state fast."""
    store = get_store()
    history = store.deployment_history(limit=500)
    if not history:
        raise DeployError("Nothing deployed.")
    current_prod = history[0]["production_version_id"]
    previous = next((d["production_version_id"] for d in history if d["production_version_id"] != current_prod
                     and store.get_version(d["production_version_id"]) is not None), None)
    if previous is None:
        raise DeployError("No earlier production version to roll back to.")
    store.record_deployment(previous, "rollback", note or f"Rolled back from v{current_prod}")
    return store.current_deployment()


def ensure_initialized(seed_cases: list[dict[str, Any]]) -> None:
    """First boot: the code's built-in config becomes v1 and goes straight
    to production (it's what the app ran before the Studio existed), the
    draft starts as a copy, and the starter test suite is loaded."""
    store = get_store()
    store.mark_interrupted_runs()
    if not store.list_versions():
        config = AgentConfig()
        v1 = store.create_version(config, "Initial version (from code defaults)")
        draft = AgentConfig.from_dict(config.to_dict())
        draft.enabled_tools = draft.enabled_tools + ["initiate_return", "generate_return_label", "escalate_to_human"]
        # Actions only inside the AOPs that govern them; lookups stay global.
        draft.aop_scoped_tools = ["initiate_return", "generate_return_label", "escalate_to_human"]
        # Returns move from the coded flow to the forced "Returns & refunds"
        # AOP, so the prompt stops telling the agent it can't handle them.
        draft.system_prompt = draft.system_prompt.replace(RETURNS_PARAGRAPH_V1, RETURNS_PARAGRAPH_AOP)
        assert RETURNS_PARAGRAPH_AOP in draft.system_prompt, "system prompt changed; update the seed"
        # So "can I talk to a person?" reaches the Escalation request AOP
        # instead of being declined by the scope gate.
        anchor = "policy questions not tied to a specific order."
        assert anchor in draft.router_prompt
        draft.router_prompt = draft.router_prompt.replace(
            anchor, anchor + " Also: asking to talk to a human, a manager, or the support team.")
        # ...and "please send it again" after a lost package isn't a return.
        anchor = "including a shipping label for one already in progress."
        assert anchor in draft.router_prompt, "router prompt changed; update the seed"
        draft.router_prompt = draft.router_prompt.replace(anchor, anchor + (
            " Not returns_refunds: asking Bookly to resend, reship, or replace an order that never arrived "
            "-- that's order_status."))
        stamp = now()
        draft.aops = [AOP(**{**a.__dict__, "updated_at": stamp}) for a in SEED_AOPS]
        draft.validate()
        store.save_draft(draft, base_version_id=v1)
        store.record_deployment(v1, "deploy", "Initial deploy", gate_override_reason="Bootstrap: pre-Studio baseline")
    if store.get_draft() is None:
        latest = store.list_versions()[0]["id"]
        store.save_draft(store.get_version(latest)["config"], base_version_id=latest)
    _sync_seed_cases(store, seed_cases)


def _case_hash(case: dict[str, Any]) -> str:
    import hashlib
    import json

    return hashlib.sha256(json.dumps(case, sort_keys=True).encode()).hexdigest()[:16]


def _sync_seed_cases(store, seed_cases: list[dict[str, Any]]) -> None:
    """Keep the starter suite current without trampling the user's edits:
    add seed tests this database has never had (a deleted one stays
    deleted), and replace a stored seed test with its new definition only
    if it's still byte-for-byte the definition it was seeded from."""
    from .evals import SEED_HISTORY

    stored = {c["id"]: c for c in store.list_test_cases()}
    seeded = set(store.get_meta("seeded_test_ids") or [])
    if not seeded and stored:
        seeded = set(SEED_HISTORY["initial_ids"])  # databases from before this bookkeeping existed
    for case in seed_cases:
        if case["id"] not in stored and case["id"] not in seeded:
            store.upsert_test_case(case)
        elif case["id"] in stored:
            old = stored[case["id"]]
            if _case_hash(old) in SEED_HISTORY["superseded"].get(case["id"], ()) and old != case:
                store.upsert_test_case(case)
        seeded.add(case["id"])
    store.set_meta("seeded_test_ids", sorted(seeded))
