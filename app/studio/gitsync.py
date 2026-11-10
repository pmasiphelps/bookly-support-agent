"""
GitHub sync: the agent definition as code.

Studio stays the editor; a GitHub repo becomes the record of what shipped.

- Connect: the production version is committed to `main` and tagged.
- Publish: the new version is committed to a branch `agent/vN` and opened
  as a pull request. Studio posts the release gate on it as a commit
  status ("Bookly Studio / release gate") and updates it when a full test
  run finishes. The repo's own GitHub Action checks AOP references.
- Deploy: a canary comments on the PR; a full deploy or promote merges it
  and tags `agent-vN`. Rollbacks commit the older version to `main`. So
  `main` tracks production.
- Pull from main: edits made in the repo come back into the draft.

The repo layout (see repo_template/README.md) round-trips exactly: a
config exported and re-imported has the same fingerprint, so tests that
passed in Studio still count for what's in git.

Git runs locally in a working clone (data/agent-repo by default); GitHub
API calls go through the `gh` CLI, which also supplies git credentials.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from ..config import ENV_FILE
from ..agent_config import AOP, CHANNELS, LOOP_TOOL_SCHEMAS, METADATA_FIELDS, AgentConfig, slugify
from . import deploy
from .store import get_store, now

TEMPLATE = Path(__file__).with_name("repo_template")
GATE_CONTEXT = "Bookly Studio / release gate"
STUDIO_TRAILER = "Studio-Version"
_REPO_NAME = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_lock = threading.RLock()


class GitSyncError(Exception):
    pass


def _repo_dir() -> Path:
    return Path(os.environ.get("BOOKLY_AGENT_REPO_DIR", "data/agent-repo"))


def _studio_url() -> str:
    return os.environ.get("BOOKLY_STUDIO_URL", "http://localhost:8000/studio").rstrip("/")


def _remote_url(repo: str) -> str:
    # BOOKLY_AGENT_REMOTE overrides the URL (tests point it at a local bare repo).
    return os.environ.get("BOOKLY_AGENT_REMOTE") or f"https://github.com/{repo}.git"


# =============================================================================
# config <-> files
# =============================================================================

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_VARIANT = re.compile(r"\n<!-- variant: (\w+) -->\n## [^\n]*\n\n")


def _dump(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000, default_flow_style=False)


def _write_text(text: str) -> str:
    return text + "\n"  # always one trailing newline, so editors that add one change nothing


def _read_text(text: str) -> str:
    return text[:-1] if text.endswith("\n") else text


def aop_file(a: AOP) -> str:
    meta: dict[str, Any] = {"name": a.name, "when": a.when, "enabled": a.enabled}
    if a.force_category:
        meta["force_category"] = a.force_category
    for key in ("created_by", "updated_by", "updated_at"):
        if getattr(a, key):
            meta[key] = getattr(a, key)
    out = f"---\n{_dump(meta)}---\n\n{_write_text(a.logic)}"
    for channel in CHANNELS:
        if channel in a.variants:
            out += f"\n<!-- variant: {channel} -->\n## {channel.title()} variant\n\n{_write_text(a.variants[channel])}"
    return out


def parse_aop(aop_id: str, text: str) -> AOP:
    m = _FRONT_MATTER.match(text)
    if not m:
        raise GitSyncError(f"agent/aops/{aop_id}.md has no --- front matter block")
    meta = yaml.safe_load(m.group(1)) or {}
    body = text[m.end():]
    body = body[1:] if body.startswith("\n") else body
    parts = _VARIANT.split(body)
    variants = {parts[i]: _read_text(parts[i + 1]) for i in range(1, len(parts) - 1, 2)}
    return AOP(
        id=aop_id, name=str(meta.get("name", aop_id)), when=str(meta.get("when", "")), logic=_read_text(parts[0]),
        variants=variants, enabled=bool(meta.get("enabled", True)), force_category=str(meta.get("force_category") or ""),
        created_by=str(meta.get("created_by", "")), updated_by=str(meta.get("updated_by", "")),
        updated_at=str(meta.get("updated_at", "")),
    )


def export_files(config: AgentConfig, cases: list[dict[str, Any]]) -> dict[str, str]:
    """The repo contents for one agent config, as {path: text}."""
    agent = {
        "agent_model": config.agent_model,
        "router_model": config.router_model,
        "max_tool_iterations": config.max_tool_iterations,
        "enabled_tools": list(config.enabled_tools),
        "aop_only_tools": list(config.aop_scoped_tools),
        "aops": [a.id for a in config.aops],
    }
    files = {
        "agent/agent.yaml": "# Written by Bookly Studio. AOPs load in the order listed under aops:.\n" + _dump(agent),
        "agent/prompts/system.md": _write_text(config.system_prompt),
        "agent/prompts/scope_gate.md": _write_text(config.router_prompt),
        "agent/prompts/decline.md": _write_text(config.out_of_scope_reply),
        "agent/catalog.yaml": "# What the platform provides. Tools are code (app/tools.py in the Studio repo).\n" + _dump({
            "tools": [{"name": s["name"], "description": s["description"]} for s in LOOP_TOOL_SCHEMAS],
            "metadata_fields": dict(METADATA_FIELDS),
        }),
        "README.md": (TEMPLATE / "README.md").read_text(),
        "scripts/validate.py": (TEMPLATE / "validate.py").read_text(),
        ".github/workflows/validate.yml": (TEMPLATE / "validate.yml").read_text(),
    }
    for a in config.aops:
        files[f"agent/aops/{a.id}.md"] = aop_file(a)
    for case in cases:
        files[f"tests/{slugify(case['id'])}.yaml"] = _dump(case)
    return files


def import_files(files: dict[str, str]) -> AgentConfig:
    """Rebuild an agent config from repo contents ({path: text})."""
    try:
        agent = yaml.safe_load(files["agent/agent.yaml"]) or {}
    except KeyError:
        raise GitSyncError("The repo has no agent/agent.yaml") from None
    aop_files = {p[len("agent/aops/"):-3]: t for p, t in files.items() if p.startswith("agent/aops/") and p.endswith(".md")}
    order = [i for i in agent.get("aops", []) if i in aop_files] + sorted(set(aop_files) - set(agent.get("aops", [])))
    prompts = {name: _read_text(files.get(f"agent/prompts/{name}.md", "")) for name in ("system", "scope_gate", "decline")}
    defaults = AgentConfig()
    return AgentConfig.from_dict({
        "agent_model": agent.get("agent_model", defaults.agent_model),
        "router_model": agent.get("router_model", defaults.router_model),
        "max_tool_iterations": int(agent.get("max_tool_iterations", defaults.max_tool_iterations)),
        "enabled_tools": list(agent.get("enabled_tools", [])),
        "aop_scoped_tools": list(agent.get("aop_only_tools", [])),
        "system_prompt": prompts["system"] or defaults.system_prompt,
        "router_prompt": prompts["scope_gate"] or defaults.router_prompt,
        "out_of_scope_reply": prompts["decline"] or defaults.out_of_scope_reply,
        "aops": [parse_aop(i, aop_files[i]) for i in order],
    }, check_refs=False)


# =============================================================================
# git (local working clone) and GitHub (gh api)
# =============================================================================

def _author_env(author: str) -> dict[str, str]:
    name = author.strip() or "Bookly Studio"
    email = f"{slugify(name).replace('-', '.')}@bookly-demo.com"
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}


class Git:
    def __init__(self, path: Path, remote: str):
        self.path, self.remote = path, remote

    def run(self, *args: str, author: str = "", check: bool = True, binary: bool = False) -> Any:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **_author_env(author)}
        cmd = ["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
               "-c", "init.defaultBranch=main", "-C", str(self.path), *args]
        res = subprocess.run(cmd, capture_output=True, env=env, timeout=60, text=not binary)
        if check and res.returncode != 0:
            err = res.stderr if isinstance(res.stderr, str) else res.stderr.decode(errors="replace")
            raise GitSyncError(f"git {args[0]} failed: {err.strip()[:400]}")
        return res

    def ensure(self) -> None:
        if not (self.path / ".git").exists():
            self.path.mkdir(parents=True, exist_ok=True)
            self.run("init", "-q")
            self.run("remote", "add", "origin", self.remote)
        else:
            self.run("remote", "set-url", "origin", self.remote)

    def remote_main(self) -> str | None:
        out = self.run("ls-remote", "origin", "refs/heads/main").stdout.split()
        return out[0] if out else None

    def fetch(self) -> bool:
        """Fetch origin/main; False if the remote has no main yet."""
        if not self.remote_main():
            return False
        self.run("fetch", "-q", "--prune", "--tags", "--force", "origin", "+refs/heads/main:refs/remotes/origin/main")
        return True

    def start_branch(self, branch: str, has_main: bool) -> None:
        if has_main:
            self.run("checkout", "-q", "-f", "-B", branch, "origin/main")
            self.run("reset", "-q", "--hard", "origin/main")
            self.run("clean", "-qfd")
        else:
            self.run("checkout", "-q", "--orphan", branch)
            self.run("rm", "-rq", "--cached", "--ignore-unmatch", ".")

    def write(self, files: dict[str, str]) -> None:
        for managed in ("agent", "tests"):
            shutil.rmtree(self.path / managed, ignore_errors=True)
        for rel, text in files.items():
            p = self.path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)

    def commit(self, message: str, author: str) -> str | None:
        self.run("add", "-A")
        if self.run("diff", "--cached", "--quiet", check=False).returncode == 0:
            return None
        self.run("commit", "-q", "-m", message, author=author)
        return self.run("rev-parse", "HEAD").stdout.strip()

    def push(self, refspec: str, force: bool = False) -> None:
        self.run("push", "-q", *(["--force"] if force else []), "origin", refspec)

    def files_at(self, ref: str) -> dict[str, str]:
        data = self.run("archive", "--format=tar", ref, binary=True).stdout
        out = {}
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for m in tar.getmembers():
                if m.isfile():
                    out[m.name] = tar.extractfile(m).read().decode("utf-8", errors="replace")
        return out

    def head_info(self, ref: str) -> dict[str, str]:
        fmt = "%H%x1f%an%x1f%s%x1f%cI%x1f%B"
        sha, author, subject, date, body = self.run("log", "-1", f"--format={fmt}", ref).stdout.split("\x1f", 4)
        trailer = re.search(rf"^{STUDIO_TRAILER}: v(\d+)", body, re.M)
        return {"sha": sha, "short": sha[:7], "author": author, "subject": subject, "date": date.strip(),
                "studio_version": int(trailer.group(1)) if trailer else None}


class GitHub:
    """The few GitHub REST calls Studio needs, through `gh api`."""

    def __init__(self, repo: str):
        self.repo = repo
        self.url = f"https://github.com/{repo}"

    def api(self, method: str, path: str, body: dict[str, Any] | None = None, check: bool = True) -> Any:
        cmd = ["gh", "api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
        if body is not None:
            cmd += ["--input", "-"]
        res = subprocess.run(cmd, input=json.dumps(body) if body is not None else None,
                             capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            if not check:
                return None
            detail = res.stdout.strip() or res.stderr.strip()
            raise GitSyncError(f"GitHub {method} {path} failed: {detail[:400]}")
        return json.loads(res.stdout) if res.stdout.strip() else None

    def check_access(self) -> None:
        info = self.api("GET", f"repos/{self.repo}")
        if not (info.get("permissions") or {}).get("push"):
            raise GitSyncError(f"Your GitHub account can't push to {self.repo}.")

    def create_pr(self, branch: str, title: str, body: str) -> dict[str, Any]:
        pr = self.api("POST", f"repos/{self.repo}/pulls", {"title": title, "head": branch, "base": "main", "body": body})
        return {"number": pr["number"], "url": pr["html_url"]}

    def pr_state(self, number: int) -> str:
        pr = self.api("GET", f"repos/{self.repo}/pulls/{number}", check=False)
        if not pr:
            return "unknown"
        return "merged" if pr.get("merged") else pr.get("state", "unknown")

    def close_pr(self, number: int) -> None:
        self.api("PATCH", f"repos/{self.repo}/pulls/{number}", {"state": "closed"})

    def merge_pr(self, number: int, title: str) -> str:
        res = self.api("PUT", f"repos/{self.repo}/pulls/{number}/merge", {"merge_method": "squash", "commit_title": title})
        return res["sha"]

    def set_status(self, sha: str, state: str, description: str, target_url: str) -> None:
        self.api("POST", f"repos/{self.repo}/statuses/{sha}", {
            "state": state, "context": GATE_CONTEXT, "description": description[:140], "target_url": target_url})

    def comment(self, number: int, body: str) -> None:
        self.api("POST", f"repos/{self.repo}/issues/{number}/comments", {"body": body})

    def tag(self, name: str, sha: str) -> None:
        # 422 = tag already exists (e.g. a version deployed twice): keep the first.
        self.api("POST", f"repos/{self.repo}/git/refs", {"ref": f"refs/tags/{name}", "sha": sha}, check=False)

    def delete_branch(self, branch: str) -> None:
        self.api("DELETE", f"repos/{self.repo}/git/refs/heads/{branch}", check=False)


# Swapped out in tests.
github_factory = GitHub


# =============================================================================
# state
# =============================================================================

def _settings() -> dict[str, Any] | None:
    return get_store().get_meta("git")


def is_connected() -> bool:
    return bool(_settings())


def _prs() -> dict[str, dict[str, Any]]:
    return get_store().get_meta("git_prs", {}) or {}


def _save_pr(version_id: int, rec: dict[str, Any]) -> None:
    prs = _prs()
    prs[str(version_id)] = rec
    get_store().set_meta("git_prs", prs)


def _set_error(message: str | None) -> None:
    get_store().set_meta("git_error", {"message": message, "at": now()} if message else None)


def _connected() -> tuple[Git, GitHub, dict[str, Any]]:
    settings = _settings()
    if not settings:
        raise GitSyncError("GitHub isn't connected.")
    git = Git(_repo_dir(), _remote_url(settings["repo"]))
    git.ensure()
    return git, github_factory(settings["repo"]), settings


def _cases() -> list[dict[str, Any]]:
    return get_store().list_test_cases()


def _production() -> dict[str, Any] | None:
    store = get_store()
    dep = store.current_deployment()
    return store.get_version(dep["production_version_id"]) if dep else None


def _version(version_id: int) -> dict[str, Any]:
    v = get_store().get_version(version_id)
    if v is None:
        raise GitSyncError(f"No version {version_id}")
    return v


# =============================================================================
# change summaries (commit messages and PR bodies)
# =============================================================================

def change_summary(old: AgentConfig | None, new: AgentConfig) -> list[str]:
    if old is None:
        return [f"Baseline: {len(new.aops)} AOPs, {len(new.enabled_tools)} tools"]
    lines = []
    before, after = {a.id: a for a in old.aops}, {a.id: a for a in new.aops}
    for i in (i for i in after if i not in before):
        lines.append(f"➕ Added AOP **{after[i].name}**")
    for i in (i for i in before if i not in after):
        lines.append(f"➖ Removed AOP **{before[i].name}**")
    for i in (i for i in after if i in before):
        if before[i].behavior() != after[i].behavior():
            lines.append(f"✏️ Changed AOP **{after[i].name}**")
    for key, label in (("agent_model", "Agent model"), ("router_model", "Scope-gate model"),
                       ("max_tool_iterations", "Max tool round-trips")):
        if getattr(old, key) != getattr(new, key):
            lines.append(f"⚙️ {label}: `{getattr(old, key)}` → `{getattr(new, key)}`")
    for key, label in (("system_prompt", "System prompt"), ("router_prompt", "Scope-gate prompt"),
                       ("out_of_scope_reply", "Decline message")):
        if getattr(old, key) != getattr(new, key):
            lines.append(f"📝 Edited the {label.lower()}")
    on, off = set(new.enabled_tools) - set(old.enabled_tools), set(old.enabled_tools) - set(new.enabled_tools)
    lines += [f"🔧 Turned on `{t}`" for t in sorted(on)] + [f"🔧 Turned off `{t}`" for t in sorted(off)]
    if set(old.aop_scoped_tools) != set(new.aop_scoped_tools):
        lines.append("🔒 Changed which tools are limited to AOPs")
    return lines or ["No behavior change"]


def _gate_line(version_id: int) -> tuple[str, str]:
    """(commit-status state, one-line description) for a version's release gate."""
    gate = deploy.gate_status(version_id)
    run = gate["run"]
    if gate["passed"]:
        return "success", f"Full suite passed: {run['passed']}/{run['total']} (run #{run['id']})"
    if run:
        return "failure", f"{run['failed']} of {run['total']} tests failed (run #{run['id']})"
    return "pending", "Waiting for a full test-suite run in Studio"


# =============================================================================
# flows
# =============================================================================

def status() -> dict[str, Any]:
    settings = _settings()
    error = get_store().get_meta("git_error")
    if not settings:
        suggested = os.environ.get("BOOKLY_AGENT_REPO") or dotenv_values(ENV_FILE).get("BOOKLY_AGENT_REPO") or ""
        return {"connected": False, "suggested_repo": suggested,
                "gh_installed": shutil.which("gh") is not None, "error": error}
    out: dict[str, Any] = {"connected": True, "repo": settings["repo"], "url": f"https://github.com/{settings['repo']}",
                           "connected_by": settings.get("connected_by"), "prs": _prs(), "error": error}
    try:
        with _lock:
            git, _, _ = _connected()
            if not git.fetch():
                out["main"] = None
                return out
            info = git.head_info("origin/main")
            fingerprint = import_files(git.files_at("origin/main")).fingerprint()
    except (GitSyncError, subprocess.SubprocessError) as exc:
        out["main"] = None
        out["error"] = {"message": str(exc), "at": now()}
        return out
    versions = get_store().list_versions()
    match = next((v["id"] for v in versions if v["fingerprint"] == fingerprint), None)
    prod = _production()
    info.update(fingerprint=fingerprint, version_id=match, url=f"{out['url']}/commit/{info['sha']}")
    out["main"] = info
    out["in_sync"] = bool(prod and prod["fingerprint"] == fingerprint)
    out["draft_matches_main"] = get_store().get_draft()["config"].fingerprint() == fingerprint
    return out


def connect(repo: str, author: str) -> dict[str, Any]:
    repo = repo.strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if not _REPO_NAME.match(repo):
        raise GitSyncError("Enter the repo as owner/name, e.g. acme/support-agent.")
    prod = _production()
    if prod is None:
        raise GitSyncError("Deploy a version first -- main starts from what's in production.")
    github = github_factory(repo)
    github.check_access()
    with _lock:
        git = Git(_repo_dir(), _remote_url(repo))
        git.ensure()
        has_main = git.fetch()
        git.start_branch("main", has_main)
        git.write(export_files(prod["config"], _cases()))
        message = (f"Agent v{prod['id']}: {prod['note'] or 'production'}\n\n"
                   f"Baseline from Bookly Studio: the version in production when the repo was connected.\n\n"
                   f"{STUDIO_TRAILER}: v{prod['id']}\nConfig-Fingerprint: {prod['fingerprint']}")
        sha = git.commit(message, author)
        if sha:
            git.push("main:refs/heads/main")
            github.tag(f"agent-v{prod['id']}", sha)
    get_store().set_meta("git", {"repo": repo, "connected_at": now(), "connected_by": author})
    _set_error(None)
    return status()


def disconnect() -> None:
    get_store().set_meta("git", None)
    get_store().set_meta("git_prs", {})
    _set_error(None)


def open_pr(version_id: int, author: str) -> dict[str, Any]:
    """Commit a version to agent/vN and open a pull request for it."""
    version = _version(version_id)
    prod = _production()
    git, github, _ = _connected()
    branch = f"agent/v{version_id}"
    summary = change_summary(prod["config"] if prod else None, version["config"])
    with _lock:
        has_main = git.fetch()
        git.start_branch(branch, has_main)
        git.write(export_files(version["config"], _cases()))
        message = (f"Agent v{version_id}: {version['note']}\n\n" + "\n".join(f"- {s}" for s in summary)
                   + f"\n\n{STUDIO_TRAILER}: v{version_id}\nConfig-Fingerprint: {version['fingerprint']}")
        sha = git.commit(message, author)
        if sha is None:
            rec = {"state": "none", "note": "main already matches this version"}
            _save_pr(version_id, rec)
            return rec
        git.push(f"{branch}:refs/heads/{branch}", force=True)
    state, gate = _gate_line(version_id)
    icon = {"success": "✅", "failure": "❌", "pending": "⏳"}[state]
    body = "\n".join([
        f"Published from **Bookly Studio** by {author or 'Bookly Studio'}.",
        "",
        f"### What changed vs production (v{prod['id']})" if prod else "### What's in it",
        *[f"- {s}" for s in summary],
        "",
        "### Release gate",
        f"{icon} {gate}. Studio updates the **{GATE_CONTEXT}** check when a full run finishes.",
        "",
        f"Deploying v{version_id} to 100% from Studio merges this PR and tags `agent-v{version_id}`. "
        f"A canary adds a comment here.",
        "",
        f"[Open v{version_id} in Studio]({_studio_url()}#build/releases) · config `{version['fingerprint']}`",
    ])
    pr = github.create_pr(branch, f"Agent v{version_id}: {version['note']}", body)
    rec = {**pr, "branch": branch, "head_sha": sha, "state": "open", "opened_by": author}
    _save_pr(version_id, rec)
    try:
        post_gate(version_id)
    except GitSyncError as exc:  # the PR exists; a missing check shouldn't hide it
        _set_error(f"Opened PR #{pr['number']} but couldn't post the release-gate check: {exc}")
    return _prs()[str(version_id)]


def post_gate(version_id: int) -> None:
    rec = _prs().get(str(version_id))
    if not rec or rec.get("state") != "open":
        return
    state, description = _gate_line(version_id)
    _, github, _ = _connected()
    github.set_status(rec["head_sha"], state, description, f"{_studio_url()}#tests")
    rec["gate"] = state
    _save_pr(version_id, rec)


def notify_run_finished(fingerprint: str) -> None:
    """After a test run: refresh the gate check on open PRs with this config."""
    if not _settings():
        return

    def work() -> None:
        try:
            versions = {str(v["id"]): v for v in get_store().list_versions()}
            for vid, rec in _prs().items():
                if rec.get("state") == "open" and vid in versions and versions[vid]["fingerprint"] == fingerprint:
                    post_gate(int(vid))
        except Exception as exc:  # noqa: BLE001 -- never break a test run over GitHub
            _set_error(f"Couldn't update the release-gate check: {exc}")

    threading.Thread(target=work, daemon=True).start()


def _sync_main(version_id: int, message: str, author: str) -> str | None:
    """Commit a version straight to main (rollbacks, versions without a PR)."""
    git, github, _ = _connected()
    version = _version(version_id)
    with _lock:
        has_main = git.fetch()
        git.start_branch("main", has_main)
        git.write(export_files(version["config"], _cases()))
        sha = git.commit(f"{message}\n\n{STUDIO_TRAILER}: v{version_id}\nConfig-Fingerprint: {version['fingerprint']}", author)
        if sha:
            git.push("main:refs/heads/main")
    return sha


def on_deployed(action: str, version_id: int, note: str, author: str, previous_production: int | None = None) -> str | None:
    """Mirror a deploy action in GitHub. Returns a one-line notice for the UI."""
    if not _settings():
        return None
    git, github, _ = _connected()
    rec = _prs().get(str(version_id)) or {}
    who = author or "Bookly Studio"
    if action == "canary":
        pct = (get_store().current_deployment() or {}).get("canary_percent")
        if rec.get("state") == "open":
            github.comment(rec["number"], f"🚦 **Canary**: v{version_id} is serving {pct}% of new conversations "
                                          f"(production stays on v{previous_production}). Started by {who}."
                                          + (f"\n\n> {note}" if note else ""))
            return f"Commented on PR #{rec['number']}"
        return None
    if action == "abort_canary":
        if rec.get("state") == "open":
            github.comment(rec["number"], f"🛑 **Canary aborted** by {who}. v{version_id} gets no traffic; the PR stays open.")
            return f"Commented on PR #{rec['number']}"
        return None
    if action in ("deploy", "promote"):
        if rec.get("state") == "open" and github.pr_state(rec["number"]) == "open":
            title = f"Agent v{version_id}: {_version(version_id)['note']} (#{rec['number']})"
            sha = github.merge_pr(rec["number"], title)
            github.tag(f"agent-v{version_id}", sha)
            github.comment(rec["number"], f"🚀 **Deployed** to 100% of new conversations by {who}. Tagged `agent-v{version_id}`."
                                          + (f"\n\n> {note}" if note else ""))
            github.delete_branch(rec["branch"])
            _save_pr(version_id, {**rec, "state": "merged", "merge_sha": sha})
            with _lock:
                git.fetch()
            return f"Merged PR #{rec['number']} and tagged agent-v{version_id}"
        sha = _sync_main(version_id, f"Deploy v{version_id}: {_version(version_id)['note']}", author)
        if sha:
            github.tag(f"agent-v{version_id}", sha)
            return f"Committed v{version_id} to main and tagged agent-v{version_id}"
        return None
    if action == "rollback":
        sha = _sync_main(version_id, f"Roll back to v{version_id}" + (f": {note}" if note else ""), author)
        return f"Committed the rollback to v{version_id} to main" if sha else None
    return None


def on_version_deleted(version_id: int, author: str) -> str | None:
    """Close the deleted version's open pull request, if it has one."""
    prs = _prs()
    rec = prs.pop(str(version_id), None)
    if rec is None:
        return None
    get_store().set_meta("git_prs", prs)
    if rec.get("state") != "open" or not _settings():
        return None
    _, github, _ = _connected()
    github.comment(rec["number"], f"🗑️ v{version_id} was deleted in Bookly Studio by {author or 'Bookly Studio'}. Closing.")
    github.close_pr(rec["number"])
    github.delete_branch(rec["branch"])
    return f"Closed PR #{rec['number']}"


def pull_main(author: str) -> dict[str, Any]:
    """Load main's agent definition into the draft."""
    git, _, _ = _connected()
    with _lock:
        if not git.fetch():
            raise GitSyncError("The repo's main branch is empty.")
        info = git.head_info("origin/main")
        config = import_files(git.files_at("origin/main"))
    store = get_store()
    store.save_draft(config, store.get_draft()["base_version_id"])
    return {"commit": info, "fingerprint": config.fingerprint()}
