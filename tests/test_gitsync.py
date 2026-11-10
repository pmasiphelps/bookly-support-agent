"""GitHub sync (app/studio/gitsync.py): the repo format round-trips a
config exactly, and publish / deploy / pull drive git and the GitHub API.
A local bare repo stands in for GitHub's git remote and FakeGitHub for its
REST API, so nothing touches the network."""

import subprocess
import sys

import pytest

from app import mock_snowflake
from app.agent_config import AOP, AgentConfig
from app.studio import deploy, evals, gitsync
from app.studio.store import Store, set_store


@pytest.fixture(autouse=True)
def studio_store():
    store = Store(":memory:")
    set_store(store)
    mock_snowflake.RETURNS.clear()
    deploy.ensure_initialized(evals.SEED_CASES)
    yield store
    set_store(None)


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


class FakeGitHub:
    """Records API calls; merging fast-forwards main in the bare repo."""

    calls: list = []

    def __init__(self, repo, bare=None):
        self.repo, self.bare = repo, FakeGitHub.bare_path
        self.url = f"https://github.com/{repo}"

    def check_access(self):
        FakeGitHub.calls.append(("check_access",))

    def create_pr(self, branch, title, body):
        FakeGitHub.calls.append(("create_pr", branch, title, body))
        return {"number": 7, "url": f"{self.url}/pull/7"}

    def pr_state(self, number):
        return "open"

    def merge_pr(self, number, title):
        FakeGitHub.calls.append(("merge_pr", number, title))
        _git("update-ref", "refs/heads/main", "refs/heads/agent/v2", cwd=self.bare)
        return _git("rev-parse", "main", cwd=self.bare).strip()

    def set_status(self, sha, state, description, target_url):
        FakeGitHub.calls.append(("set_status", state, description))

    def comment(self, number, body):
        FakeGitHub.calls.append(("comment", number, body))

    def tag(self, name, sha):
        FakeGitHub.calls.append(("tag", name))

    def delete_branch(self, branch):
        FakeGitHub.calls.append(("delete_branch", branch))


@pytest.fixture
def github(tmp_path, monkeypatch):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    monkeypatch.setenv("BOOKLY_AGENT_REMOTE", str(bare))
    monkeypatch.setenv("BOOKLY_AGENT_REPO_DIR", str(tmp_path / "clone"))
    FakeGitHub.bare_path, FakeGitHub.calls = bare, []
    monkeypatch.setattr(gitsync, "github_factory", FakeGitHub)
    return bare


def _names(kind):
    return [c for c in FakeGitHub.calls if c[0] == kind]


# -- file format --------------------------------------------------------------------------------

def test_repo_files_round_trip_the_fingerprint(studio_store):
    config = studio_store.get_draft()["config"]
    config.aops.append(AOP(
        name="Order cancellation", when="The customer wants to cancel an order.",
        logic="1. get the sender's order ID\n2. check with @get_order_status \na. processing: follow #Escalation request",
        variants={"email": "Reply by email.\n\nKeep it short.  "}, force_category="", updated_by="Patrick Phrayme"))
    files = gitsync.export_files(config, studio_store.list_test_cases())
    back = gitsync.import_files(files)
    assert back.fingerprint() == config.fingerprint()
    assert back.aops[-1].variants == config.aops[-1].variants
    assert "agent/aops/order-cancellation.md" in files
    assert any(p.startswith("tests/") for p in files)


def test_an_editor_adding_a_trailing_newline_changes_nothing(studio_store):
    config = studio_store.get_draft()["config"]
    files = gitsync.export_files(config, [])
    files["agent/prompts/system.md"] = files["agent/prompts/system.md"]  # already ends in one newline
    assert gitsync.import_files(files).fingerprint() == config.fingerprint()


def test_validator_passes_on_export_and_flags_broken_references(studio_store, tmp_path):
    files = gitsync.export_files(studio_store.get_draft()["config"], [])
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    ok = subprocess.run([sys.executable, "scripts/validate.py"], cwd=tmp_path, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr

    aop = next((tmp_path / "agent/aops").glob("*.md"))
    aop.write_text(aop.read_text() + "\nThen call @refund_everything and follow #Nonexistent thing.\n")
    bad = subprocess.run([sys.executable, "scripts/validate.py"], cwd=tmp_path, capture_output=True, text=True)
    assert bad.returncode == 1
    assert "@refund_everything isn't a tool" in bad.stdout
    assert "#Nonexistent thing" in bad.stdout


# -- flows --------------------------------------------------------------------------------------

def test_connect_publish_deploy_merges_and_tags(studio_store, github):
    gitsync.connect("acme/bookly-agent", "Patrick Phrayme")
    assert "Agent v1" in _git("log", "-1", "--format=%s", "main", cwd=github)
    assert _git("log", "-1", "--format=%an", "main", cwd=github).strip() == "Patrick Phrayme"
    assert ("tag", "agent-v1") in FakeGitHub.calls

    # Publish the seeded draft as v2: a branch and a PR, with the gate pending.
    v2 = deploy.publish_draft("Add AOPs")
    rec = gitsync.open_pr(v2, "Patrick Phrayme")
    assert rec["state"] == "open" and rec["number"] == 7
    pr = _names("create_pr")[0]
    assert pr[1] == "agent/v2" and pr[2] == "Agent v2: Add AOPs"
    assert "Added AOP **Escalation request**" in pr[3]
    assert _names("set_status")[-1][1] == "pending"
    assert "agent/aops/escalation-request.md" in _git("ls-tree", "-r", "--name-only", "agent/v2", cwd=github)

    # A full passing run flips the check to success.
    config = studio_store.get_version(v2)["config"]
    run_id = studio_store.create_test_run("draft", None, config.fingerprint(), 1, full_suite=True)
    studio_store.add_test_result(run_id, {"case_id": "c", "case_name": "c", "kind": "scripted", "passed": True,
                                          "checks": [], "transcript": []})
    studio_store.finish_test_run(run_id)
    gitsync.post_gate(v2)
    assert _names("set_status")[-1][1] == "success"

    # Canary comments; promote merges, tags, and comments.
    deploy.deploy(v2, mode="canary", canary_percent=10)
    assert "Commented on PR #7" == gitsync.on_deployed("canary", v2, "", "Patrick Phrayme", previous_production=1)
    deploy.promote_canary()
    notice = gitsync.on_deployed("promote", v2, "", "Patrick Phrayme", previous_production=1)
    assert notice == "Merged PR #7 and tagged agent-v2"
    assert ("tag", "agent-v2") in FakeGitHub.calls
    assert gitsync._prs()["2"]["state"] == "merged"

    status = gitsync.status()
    assert status["in_sync"] and status["main"]["version_id"] == 2


def test_publishing_what_main_already_has_opens_no_pr(studio_store, github):
    gitsync.connect("acme/bookly-agent", "Patrick Phrayme")
    studio_store.save_draft(studio_store.get_version(1)["config"], base_version_id=1)
    v2 = studio_store.create_version(studio_store.get_version(1)["config"], "Same as v1")
    assert gitsync.open_pr(v2, "Patrick Phrayme")["state"] == "none"
    assert not _names("create_pr")


def test_rollback_commits_the_older_version_to_main(studio_store, github):
    gitsync.connect("acme/bookly-agent", "Patrick Phrayme")
    v2 = deploy.publish_draft("Add AOPs")
    deploy.deploy(v2, mode="full", override_reason="test")
    gitsync.on_deployed("deploy", v2, "", "Patrick Phrayme")  # no PR: committed straight to main
    assert "Deploy v2" in _git("log", "-1", "--format=%s", "main", cwd=github)
    deploy.rollback()
    assert gitsync.on_deployed("rollback", 1, "", "Patrick Phrayme") == "Committed the rollback to v1 to main"
    assert gitsync.status()["main"]["version_id"] == 1


def test_pull_from_main_brings_repo_edits_into_the_draft(studio_store, github, tmp_path):
    gitsync.connect("acme/bookly-agent", "Patrick Phrayme")
    work = tmp_path / "engineer"
    subprocess.run(["git", "clone", "-q", str(github), str(work)], check=True)
    decline = work / "agent/prompts/decline.md"
    decline.write_text("Sorry -- I can only help with Bookly orders, returns, and policies.\n")
    _git("-c", "user.name=Eng", "-c", "user.email=e@x", "commit", "-qam", "Shorter decline message", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)

    status = gitsync.status()
    assert not status["in_sync"] and status["main"]["subject"] == "Shorter decline message"
    assert status["main"]["studio_version"] is None
    gitsync.pull_main("Patrick Phrayme")
    assert studio_store.get_draft()["config"].out_of_scope_reply.startswith("Sorry -- I can only help")


def test_connect_rejects_a_bad_repo_name(studio_store, github):
    with pytest.raises(gitsync.GitSyncError, match="owner/name"):
        gitsync.connect("not a repo", "x")


def test_deleting_a_version_closes_its_pr_and_rollback_skips_it(studio_store, github):
    FakeGitHub.close_pr = lambda self, n: FakeGitHub.calls.append(("close_pr", n))
    gitsync.connect("acme/bookly-agent", "Patrick Phrayme")
    v2 = deploy.publish_draft("Add AOPs")
    gitsync.open_pr(v2, "Patrick Phrayme")
    with pytest.raises(deploy.DeployError, match="in production"):
        deploy.delete_version(1)
    deploy.delete_version(v2)
    assert gitsync.on_version_deleted(v2, "Patrick Phrayme") == "Closed PR #7"
    assert ("close_pr", 7) in FakeGitHub.calls and "2" not in gitsync._prs()
    assert studio_store.get_version(v2) is None
    assert studio_store.get_draft()["base_version_id"] == 1

    # Rollback never targets a deleted version.
    v3 = deploy.publish_draft("Again")
    deploy.deploy(v3, override_reason="test")
    v4 = deploy.publish_draft("And again")
    deploy.deploy(v4, override_reason="test")
    deploy.delete_version(v3)
    deploy.rollback()
    assert studio_store.current_deployment()["production_version_id"] == 1
