"""Settings: read the .env file and environment variables.

Secrets (API keys, client secrets) live only in .env or the real environment,
never in code. Code asks for what it needs through `require`, so a missing
setting produces one clear error message.

Which sources to fetch is NOT a setting: it lives in the `sources` table,
seeded from data/sources.csv.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


class ConfigError(Exception):
    """A required setting (such as an API key) is missing or invalid."""


def load_env_file(path: Path = ENV_FILE) -> None:
    """Read KEY=VALUE lines from a .env file into os.environ.

    Same behaviour as in youtube_channel.py: variables already set in the real
    environment win, so `export YOUTUBE_API_KEY=...` overrides the file.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def require(name: str) -> str:
    """Return a setting that must exist, or raise ConfigError."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. Add it to {ENV_FILE} or export it.")
    return value
