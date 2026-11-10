import os

from app.config import load_environment


def test_project_key_and_model_override_stale_terminal_values(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-terminal-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "terminal-model")
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=project-key\nANTHROPIC_MODEL=project-model\n")

    load_environment(env_file)

    assert os.environ["ANTHROPIC_API_KEY"] == "project-key"
    # The project's model wins too: ANTHROPIC_MODEL leaks in from Claude Code.
    assert os.environ["ANTHROPIC_MODEL"] == "project-model"


def test_other_settings_keep_normal_environment_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_ROUTER_MODEL", "terminal-router")
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_ROUTER_MODEL=project-router\n")

    load_environment(env_file)

    assert os.environ["ANTHROPIC_ROUTER_MODEL"] == "terminal-router"


def test_environment_key_still_works_without_project_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "terminal-key")

    load_environment(tmp_path / ".env")

    assert os.environ["ANTHROPIC_API_KEY"] == "terminal-key"


def test_loading_project_env_does_not_depend_on_working_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-terminal-key")
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=project-key\n")
    another_directory = tmp_path / "elsewhere"
    another_directory.mkdir()
    monkeypatch.chdir(another_directory)

    load_environment(env_file)

    assert os.environ["ANTHROPIC_API_KEY"] == "project-key"
