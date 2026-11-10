"""
The agent's editable behavior, as one versionable object.

Everything a support team would want to change without a code deploy --
prompts, models, which tools the flexible loop may call, the decline
message, and Agent Operating Procedures (AOPs) -- lives in AgentConfig.
The Agent Studio (app/studio/) edits a draft of it, freezes drafts into
immutable versions, tests them, and deploys them; each chat Session is
pinned to the AgentConfig (and channel) it started with.

AOPs are natural-language procedures with three kinds of reference:

    @tool_name        a coded tool the agent should call
    #AOP Name         another AOP to hand off to
    {{ field }}       customer metadata (see METADATA_FIELDS)

Every reference is checked on save. At runtime the system prompt lists
each AOP's name and "when to use it"; the agent loads an AOP's steps by
calling the use_aop tool, which is also the audit trail of which AOPs
shaped each reply.

An AOP can also be *forced* for a scope-gate category (Decagon's "force
AOP selection"): when the gate says returns_refunds, the agent starts the
turn with that AOP already loaded instead of choosing. A config with no
AOP forced for returns_refunds falls back to the coded state machine in
app/returns_flow.py -- which is what v1 (the code defaults) uses.

What deliberately is NOT here: identity checks and the 30-day return
window. Those are enforced inside the tools themselves (app/tools.py), so
no AOP wording -- and no model mistake -- can file an ineligible return.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .intent_router import OUT_OF_SCOPE_REPLY, ROUTER_MODEL, ROUTER_SYSTEM_PROMPT
from .prompts import SYSTEM_PROMPT
from .tools import FLEXIBLE_LOOP_TOOLS, OPTIONAL_LOOP_TOOLS, TOOLS

DEFAULT_AGENT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
DEFAULT_MAX_TOOL_ITERATIONS = 6

# Tools the agent loop *could* be given. The defaults leave out the returns
# tools (v1 runs returns through the coded flow); a config that handles
# returns with an AOP turns them on. Either way, initiate_return and
# generate_return_label re-check identity and eligibility themselves.
LOOP_TOOL_SCHEMAS = TOOLS + OPTIONAL_LOOP_TOOLS
AVAILABLE_LOOP_TOOLS = [t["name"] for t in LOOP_TOOL_SCHEMAS]
DEFAULT_ENABLED_TOOLS = [t["name"] for t in FLEXIBLE_LOOP_TOOLS]

CHANNELS = ("chat", "email")

# What {{ field }} can reference. Values come from the verified customer
# (app/tools.py:customer_metadata) or, in Studio Preview, test metadata.
# Name recorded on edits when the editor leaves "Your name" blank (demo user).
DEFAULT_AUTHOR = "Patrick Phrayme"

METADATA_FIELDS = {
    "customer_name": "Customer's first name (once verified)",
    "customer_type": '"new" (one order) or "returning"',
    "order_count": "Number of orders on the account",
    "email": "Verified account email",
    "channel": '"chat" or "email"',
}

USE_AOP_TOOL = "use_aop"

# Scope-gate categories an AOP can be forced for.
FORCEABLE_CATEGORIES = ("returns_refunds", "order_status", "general_policy")

# Metadata about an AOP edit, not behavior -- excluded from the fingerprint
# so renaming an author doesn't invalidate a passing test run.
_BOOKKEEPING_KEYS = {"created_by", "updated_by", "updated_at"}

_TOOL_REF = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")
_META_REF = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_AOP_REF_START = re.compile(r"#(?=[A-Za-z])")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "aop"


@dataclass
class AOP:
    """An Agent Operating Procedure. `logic` is the global version;
    `variants` optionally overrides it per channel ("chat" / "email")."""

    name: str
    when: str
    logic: str
    id: str = ""
    variants: dict[str, str] = field(default_factory=dict)
    enabled: bool = True  # included when this config is released
    # Scope-gate category this AOP is always used for ("" = the agent picks).
    force_category: str = ""
    created_by: str = ""
    updated_by: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        self.id = self.id or slugify(self.name)
        self.variants = {c: v for c, v in (self.variants or {}).items() if c in CHANNELS and v.strip()}

    def logic_for(self, channel: str) -> str:
        return self.variants.get(channel) or self.logic

    def all_text(self) -> str:
        return "\n".join([self.when, self.logic, *self.variants.values()])

    def behavior(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if k not in _BOOKKEEPING_KEYS}
        if not d["force_category"]:
            del d["force_category"]  # keeps fingerprints of pre-existing configs stable
        return d


def tool_refs(text: str) -> set[str]:
    return set(_TOOL_REF.findall(text))


def metadata_refs(text: str) -> set[str]:
    return set(_META_REF.findall(text))


def aop_refs(text: str, names: list[str]) -> tuple[set[str], list[str]]:
    """(#references that match a known AOP name, unmatched fragments).
    Names can contain spaces, so match the longest known name at each '#'."""
    found: set[str] = set()
    unknown: list[str] = []
    by_length = sorted(names, key=len, reverse=True)
    for m in _AOP_REF_START.finditer(text):
        rest = text[m.end():]
        hit = next((n for n in by_length if rest.lower().startswith(n.lower())), None)
        if hit:
            found.add(hit)
        else:
            unknown.append(re.match(r"[\w-]+(?: [\w-]+)?", rest).group(0))
    return found, unknown


@dataclass
class AgentConfig:
    agent_model: str = DEFAULT_AGENT_MODEL
    router_model: str = ROUTER_MODEL
    system_prompt: str = SYSTEM_PROMPT
    router_prompt: str = ROUTER_SYSTEM_PROMPT
    aops: list[AOP] = field(default_factory=list)
    enabled_tools: list[str] = field(default_factory=lambda: list(DEFAULT_ENABLED_TOOLS))
    out_of_scope_reply: str = OUT_OF_SCOPE_REPLY
    max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS
    # Tools the agent may call only while following an AOP that references
    # them (Decagon-style per-AOP tool scoping). Everything else enabled is
    # available on every turn.
    aop_scoped_tools: list[str] = field(default_factory=list)

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any], check_refs: bool = True) -> "AgentConfig":
        defaults = cls()
        known = {k: v for k, v in data.items() if k in defaults.__dataclass_fields__}
        raw_aops = known.get("aops")
        if raw_aops is None and "procedures" in data:
            # Pre-AOP Studio drafts: {name, trigger, instructions, enabled}.
            raw_aops = [
                {"name": p["name"], "when": p.get("trigger", ""), "logic": p.get("instructions", ""),
                 "enabled": p.get("enabled", True)}
                for p in data["procedures"]
            ]
        known["aops"] = [
            a if isinstance(a, AOP) else AOP(**{k: v for k, v in a.items() if k in AOP.__dataclass_fields__})
            for a in raw_aops or []
        ]
        config = cls(**known)
        config.validate(check_refs=check_refs)
        return config

    def fingerprint(self) -> str:
        """Stable hash of the behavior-relevant content. Two configs with
        the same fingerprint behave identically, so a passing test run on
        a draft also counts for a version published from that draft."""
        d = self.to_dict()
        d["aops"] = [a.behavior() for a in self.aops]
        if not d["aop_scoped_tools"]:
            del d["aop_scoped_tools"]  # keeps fingerprints of pre-existing configs stable
        canonical = json.dumps(d, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]

    def reference_problems(self) -> dict[str, list[str]]:
        """Per-AOP list of broken references, keyed by AOP id."""
        names = [a.name for a in self.aops]
        enabled_names = {a.name for a in self.aops if a.enabled}
        problems: dict[str, list[str]] = {}
        for a in self.aops:
            issues = []
            text = a.all_text()
            for tool in sorted(tool_refs(text)):
                if tool not in AVAILABLE_LOOP_TOOLS:
                    issues.append(f"@{tool} isn't a tool the agent can call")
                elif tool not in self.enabled_tools:
                    issues.append(f"@{tool} is turned off in Tools")
            found, unknown = aop_refs(text, names)
            issues += [f"#{u}… doesn't match any AOP" for u in unknown]
            if a.enabled:
                issues += [f"#{n} isn't included in the release" for n in sorted(found - enabled_names)]
            issues += [f"{{{{ {f} }}}} isn't a known metadata field"
                       for f in sorted(metadata_refs(text) - set(METADATA_FIELDS))]
            if issues:
                problems[a.id] = issues
        return problems

    def validate(self, check_refs: bool = True) -> None:
        """check_refs=False for drafts: a half-written AOP with a dangling
        reference can be saved, but not published as a version."""
        unknown = set(self.enabled_tools) - set(AVAILABLE_LOOP_TOOLS)
        if unknown:
            raise ValueError(f"Unknown or non-loop tools: {sorted(unknown)}")
        unknown = set(self.aop_scoped_tools) - set(AVAILABLE_LOOP_TOOLS)
        if unknown:
            raise ValueError(f"Can't scope unknown tools: {sorted(unknown)}")
        if not 1 <= int(self.max_tool_iterations) <= 12:
            raise ValueError("max_tool_iterations must be between 1 and 12")
        if not self.system_prompt.strip() or not self.router_prompt.strip():
            raise ValueError("Prompts can't be empty")
        if not self.agent_model.strip() or not self.router_model.strip():
            raise ValueError("Model names can't be empty")
        seen_ids, seen_names, forced = set(), set(), {}
        for a in self.aops:
            if a.force_category and a.force_category not in FORCEABLE_CATEGORIES:
                raise ValueError(f"{a.name}: can't force an AOP for {a.force_category!r}")
            if a.force_category and a.enabled:
                if a.force_category in forced:
                    raise ValueError(f"Both {forced[a.force_category]!r} and {a.name!r} are forced for "
                                     f"{a.force_category} -- only one can be")
                forced[a.force_category] = a.name
            if not a.name.strip() or not a.when.strip() or not a.logic.strip():
                raise ValueError("Every AOP needs a name, a 'when to use it', and logic")
            if a.id in seen_ids or a.name.lower() in seen_names:
                raise ValueError(f"Two AOPs are named {a.name!r}")
            seen_ids.add(a.id)
            seen_names.add(a.name.lower())
        problems = self.reference_problems() if check_refs else {}
        if problems:
            by_name = {a.id: a.name for a in self.aops}
            detail = "; ".join(f"{by_name[i]}: {', '.join(p)}" for i, p in problems.items())
            raise ValueError(f"Broken AOP references -- {detail}")

    # -- runtime ----------------------------------------------------------

    def active_aops(self) -> list[AOP]:
        return [a for a in self.aops if a.enabled]

    def forced_aop(self, category: str | None) -> AOP | None:
        return next((a for a in self.active_aops() if category and a.force_category == category), None)

    def uses_coded_returns(self) -> bool:
        return self.forced_aop("returns_refunds") is None

    def compiled_system_prompt(self, channel: str = "chat") -> str:
        """The system prompt the model sees: the base prompt plus a catalog
        of enabled AOPs (name + when). Steps are loaded via use_aop, so the
        prompt stays small as AOPs grow -- and constant for a conversation."""
        active = self.active_aops()
        parts = [self.system_prompt.rstrip()]
        if channel == "email":
            parts += ["", "This conversation is over email: reply as a complete, self-contained email "
                          "with a greeting and sign-off, not a chat message."]
        if not active:
            return self.system_prompt if channel != "email" else "\n".join(parts) + "\n"
        parts += [
            "",
            "Agent Operating Procedures (AOPs). Before you reply or call any other tool, check the "
            "customer's latest message against the AOPs below. If one matches, your FIRST action must be "
            f"calling {USE_AOP_TOOL} with its name -- even if you think you already know what to do, "
            "because the AOP may differ from your defaults -- then follow the steps it returns. If an "
            "AOP's steps send you to another AOP, load that one too. AOPs never override the hard rules "
            "above. Values for {{ field }} placeholders come from the verified session context; if one "
            "is missing, don't guess it.",
        ]
        for a in active:
            parts.append(f"- {a.name}: {a.when.strip()}")
        return "\n".join(parts) + "\n"

    def render_aop(self, name: str, channel: str = "chat") -> str | None:
        aop = next((a for a in self.active_aops() if a.name.lower() == name.strip().lower()), None)
        if aop is None:
            return None
        text = aop.logic_for(channel)
        text = _TOOL_REF.sub(lambda m: f"the `{m.group(1)}` tool", text)
        names = [a.name for a in self.active_aops()]
        for n in sorted(names, key=len, reverse=True):
            text = re.sub("#" + re.escape(n), f'the "{n}" AOP (load it with {USE_AOP_TOOL})', text, flags=re.I)
        return f'AOP "{aop.name}" -- follow these steps:\n{text}'

    def aops_using_tool(self, tool: str) -> list[str]:
        return [a.name for a in self.active_aops() if tool in tool_refs(a.all_text())]

    def loop_tools(self) -> list[dict[str, Any]]:
        """The tool list is the same on every turn (changing it mid-
        conversation would invalidate earlier thinking blocks); AOP scoping
        is enforced when a tool is called -- see agent._scope_refusal."""
        enabled = set(self.enabled_tools)
        scoped = set(self.aop_scoped_tools)
        tools = []
        for t in LOOP_TOOL_SCHEMAS:
            if t["name"] not in enabled:
                continue
            if t["name"] in scoped:
                users = self.aops_using_tool(t["name"])
                note = (f" Only available while following an AOP that uses it ({', '.join(users)}): "
                        f"load that AOP with {USE_AOP_TOOL} first.") if users else " Not available: no AOP uses it."
                t = {**t, "description": t["description"] + note}
            tools.append(t)
        active = self.active_aops()
        if active:
            tools.append({
                "name": USE_AOP_TOOL,
                "description": "Load the step-by-step instructions for one of the Agent Operating Procedures listed in your system prompt.",
                "input_schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "enum": [a.name for a in active]}},
                    "required": ["name"],
                },
            })
        return tools
