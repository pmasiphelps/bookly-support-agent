"""Check the agent definition in this repo before it ships.

Runs in GitHub Actions on every pull request (see
.github/workflows/validate.yml) and locally with `python scripts/validate.py`.
Self-contained -- needs only PyYAML -- so CI doesn't need the Studio code.

Checks:
- every AOP file is listed in agent/agent.yaml, and every listed AOP exists
- each AOP has a name, a "when", and logic
- @tool references name a tool from agent/catalog.yaml that's turned on
- #AOP references name another AOP (included in the release, if this one is)
- {{ field }} references name a known customer metadata field
- at most one enabled AOP is forced per scope-gate category
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "agent"
TOOL_REF = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")
META_REF = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
AOP_REF_START = re.compile(r"#(?=[A-Za-z])")
FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)

errors: list[tuple[str, str]] = []


def error(path: Path, message: str) -> None:
    errors.append((str(path.relative_to(ROOT)), message))


def load_aop(path: Path) -> dict | None:
    text = path.read_text()
    m = FRONT_MATTER.match(text)
    if not m:
        error(path, "missing the --- front matter block (name, when, enabled)")
        return None
    meta = yaml.safe_load(m.group(1)) or {}
    meta["_text"] = text[m.end():]
    return meta


def aop_refs(text: str, names: list[str]) -> tuple[set[str], list[str]]:
    """Names can contain spaces, so match the longest known name at each '#'."""
    found, unknown = set(), []
    for m in AOP_REF_START.finditer(text):
        rest = text[m.end():]
        hit = next((n for n in sorted(names, key=len, reverse=True) if rest.lower().startswith(n.lower())), None)
        if hit:
            found.add(hit)
        else:
            unknown.append(re.match(r"[\w-]+(?: [\w-]+)?", rest).group(0))
    return found, unknown


def main() -> int:
    agent = yaml.safe_load((AGENT / "agent.yaml").read_text())
    catalog = yaml.safe_load((AGENT / "catalog.yaml").read_text())
    tools = {t["name"] for t in catalog["tools"]}
    fields = set(catalog["metadata_fields"])
    enabled_tools = set(agent.get("enabled_tools", []))

    listed = agent.get("aops", [])
    files = {p.stem: p for p in sorted((AGENT / "aops").glob("*.md"))}
    for stem in sorted(set(files) - set(listed)):
        error(files[stem], "not listed under aops: in agent/agent.yaml, so it won't be loaded")
    for aop_id in listed:
        if aop_id not in files:
            error(AGENT / "agent.yaml", f"lists AOP {aop_id!r} but agent/aops/{aop_id}.md doesn't exist")

    aops = {stem: load_aop(path) for stem, path in files.items()}
    aops = {k: v for k, v in aops.items() if v is not None}
    names = [a.get("name", "") for a in aops.values()]
    enabled_names = {a.get("name") for a in aops.values() if a.get("enabled", True)}
    forced: dict[str, str] = {}

    for stem, a in aops.items():
        path = files[stem]
        for key in ("name", "when"):
            if not str(a.get(key, "")).strip():
                error(path, f"front matter needs a {key!r}")
        if not a["_text"].strip():
            error(path, "has no logic -- write the steps below the front matter")
        text = "\n".join([str(a.get("when", "")), a["_text"]])
        for tool in sorted(set(TOOL_REF.findall(text))):
            if tool not in tools:
                error(path, f"@{tool} isn't a tool the agent can call")
            elif tool not in enabled_tools:
                error(path, f"@{tool} is turned off (add it to enabled_tools in agent/agent.yaml)")
        found, unknown = aop_refs(text, names)
        for u in unknown:
            error(path, f"#{u}... doesn't match any AOP")
        if a.get("enabled", True):
            for n in sorted(found - enabled_names):
                error(path, f"#{n} isn't included in the release (enabled: false)")
        for f in sorted(set(META_REF.findall(text)) - fields):
            error(path, f"{{{{ {f} }}}} isn't a known metadata field")
        category = a.get("force_category")
        if category and a.get("enabled", True):
            if category in forced:
                error(path, f"is forced for {category}, but so is {forced[category]!r}")
            forced[category] = a.get("name")

    for path, message in errors:
        print(f"::error file={path}::{message}")
        print(f"  {path}: {message}", file=sys.stderr)
    if errors:
        print(f"\n{len(errors)} problem(s) found.", file=sys.stderr)
        return 1
    print(f"OK: {len(aops)} AOPs, {len(enabled_tools)} tools enabled, all references resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
