"""Runtime settings for ALPHAONE, read from environment variables.

Every value has a sensible default so the only things you must provide are
the API key and the SMTP credentials (see README.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Who gets the newsletter
    recipient: str = field(default_factory=lambda: _env("ALPHAONE_TO", "harkomal.design@gmail.com"))
    sender_name: str = "ALPHAONE"

    # Claude
    model: str = field(default_factory=lambda: _env("ALPHAONE_MODEL", "claude-opus-5-5"))
    research_effort: str = field(default_factory=lambda: _env("ALPHAONE_EFFORT", "high"))
    editor_effort: str = field(default_factory=lambda: _env("ALPHAONE_EDITOR_EFFORT", "medium"))
    max_searches: int = field(default_factory=lambda: _env_int("ALPHAONE_MAX_SEARCHES", 12))
    max_fetches: int = field(default_factory=lambda: _env_int("ALPHAONE_MAX_FETCHES", 14))
    fetch_max_tokens: int = field(default_factory=lambda: _env_int("ALPHAONE_FETCH_MAX_TOKENS", 25000))

    # What counts as "new"
    lookback_hours: int = field(default_factory=lambda: _env_int("ALPHAONE_LOOKBACK_HOURS", 24))
    max_news_candidates: int = field(default_factory=lambda: _env_int("ALPHAONE_MAX_NEWS", 80))
    max_paper_candidates: int = field(default_factory=lambda: _env_int("ALPHAONE_MAX_PAPERS", 40))

    # Delivery
    timezone: str = field(default_factory=lambda: _env("ALPHAONE_TIMEZONE", "UTC"))
    skip_quiet_hours: bool = field(default_factory=lambda: _env_bool("ALPHAONE_SKIP_QUIET", False))
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST", "smtp.gmail.com"))
    smtp_port: int = field(default_factory=lambda: _env_int("SMTP_PORT", 465))
    smtp_username: str = field(default_factory=lambda: _env("SMTP_USERNAME", ""))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD", ""))

    # Files
    state_path: Path = field(
        default_factory=lambda: Path(_env("ALPHAONE_STATE", str(HERE / ".state" / "state.json")))
    )
    out_dir: Path = field(default_factory=lambda: Path(_env("ALPHAONE_OUT", str(HERE / "out"))))
