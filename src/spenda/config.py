from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _default_opencode_database() -> Path:
    """Return OpenCode's conventional SQLite location without requiring it exists."""
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return Path(os.environ.get("OPENCODE_DB") or data_home / "opencode" / "opencode.db").expanduser()


def _default_claude_home() -> Path:
    """Return Claude Code's configuration directory without requiring it exists."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def _default_cursor_home() -> Path:
    """Return Cursor's agent history directory without requiring it exists."""
    return Path(os.environ.get("CURSOR_HOME") or Path.home() / ".cursor").expanduser()


def _default_cursor_user_dir() -> Path:
    """Return the Cursor editor's per-user data directory for this platform."""
    configured = os.environ.get("CURSOR_USER_DIR")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Cursor" / "User"
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "Cursor" / "User"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "Cursor" / "User"


CLAUDE_BILLING_MODES = ("auto", "subscription", "api")


def _default_claude_billing() -> str:
    """Return how Anthropic-direct Claude calls are billed when transcripts cannot say."""
    value = os.environ.get("SPENDA_CLAUDE_BILLING", "auto").strip().lower()
    return value if value in CLAUDE_BILLING_MODES else "auto"


@dataclass(frozen=True, slots=True)
class Settings:
    codex_home: Path
    database: Path
    keep_preview: bool = True
    preview_chars: int = 240
    running_window_seconds: int = 120
    # Keep source paths last so the existing positional constructor remains compatible.
    opencode_database: Path = field(default_factory=_default_opencode_database)
    claude_home: Path = field(default_factory=_default_claude_home)
    cursor_home: Path = field(default_factory=_default_cursor_home)
    cursor_user_dir: Path = field(default_factory=_default_cursor_user_dir)
    # "auto" derives subscription-vs-API-key from the Claude home's login
    # profile; "subscription" or "api" forces it, e.g. for copied project dirs.
    claude_billing: str = field(default_factory=_default_claude_billing)

    def validate(self) -> Settings:
        """Reject configurations that could write into source-owned state."""
        if self.claude_billing not in CLAUDE_BILLING_MODES:
            raise ValueError(f"claude_billing must be one of {CLAUDE_BILLING_MODES}: {self.claude_billing}")
        codex_home = self.codex_home.resolve()
        database = self.database.resolve()
        opencode_database = self.opencode_database.resolve()
        claude_home = self.claude_home.resolve()
        try:
            database.relative_to(codex_home)
        except ValueError:
            pass
        else:
            raise ValueError(f"dashboard database must be outside CODEX_HOME: {database}")
        if database == opencode_database:
            raise ValueError(f"dashboard database must differ from the OpenCode source database: {database}")
        try:
            database.relative_to(claude_home)
        except ValueError:
            pass
        else:
            raise ValueError(f"dashboard database must be outside CLAUDE_CONFIG_DIR: {database}")
        for label, directory in (
            ("CURSOR_HOME", self.cursor_home.resolve()),
            ("CURSOR_USER_DIR", self.cursor_user_dir.resolve()),
        ):
            try:
                database.relative_to(directory)
            except ValueError:
                pass
            else:
                raise ValueError(f"dashboard database must be outside {label}: {database}")
        return self

    @classmethod
    def load(
        cls,
        codex_home: str | Path | None = None,
        database: str | Path | None = None,
        keep_preview: bool = True,
        opencode_database: str | Path | None = None,
        claude_home: str | Path | None = None,
        cursor_home: str | Path | None = None,
        cursor_user_dir: str | Path | None = None,
        claude_billing: str | None = None,
    ) -> Settings:
        home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
        data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        db = Path(
            database
            or os.environ.get("SPENDA_DB")
            or data_home / "spenda" / "dashboard.sqlite"
        ).expanduser()
        opencode_db = Path(opencode_database).expanduser() if opencode_database else _default_opencode_database()
        claude = Path(claude_home).expanduser() if claude_home else _default_claude_home()
        cursor = Path(cursor_home).expanduser() if cursor_home else _default_cursor_home()
        cursor_user = Path(cursor_user_dir).expanduser() if cursor_user_dir else _default_cursor_user_dir()
        return cls(
            home.resolve(), db.resolve(), keep_preview,
            opencode_database=opencode_db.resolve(), claude_home=claude.resolve(),
            cursor_home=cursor.resolve(), cursor_user_dir=cursor_user.resolve(),
            claude_billing=claude_billing or _default_claude_billing(),
        ).validate()
