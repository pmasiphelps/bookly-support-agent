"""Load project settings consistently for the server and diagnostic scripts."""

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


# Settings where this project's .env wins over the terminal environment.
# ANTHROPIC_MODEL is also read by Claude Code, so it can leak into a
# terminal's environment and silently pin this app to a different model.
PROJECT_PINNED = ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL")


def load_environment(env_file: Path = ENV_FILE) -> None:
    load_dotenv(env_file)
    # This project's explicitly configured values win over anything
    # inherited from the terminal. Other settings keep normal env precedence.
    values = dotenv_values(env_file)
    for name in PROJECT_PINNED:
        if values.get(name):
            os.environ[name] = values[name]
