"""Detect how a local Claude Code installation is billed, without reading secrets.

Claude Code transcripts identify Vertex AI and Bedrock calls by their message
id prefixes, but an Anthropic-direct call looks the same whether it was paid
per token with an API key or covered by a claude.ai subscription.  That
distinction only exists in the installation's login profile, so this module
reads a small allow-list of non-secret scalar fields from ``.claude.json`` and
``settings.json``.  ``.credentials.json`` holds tokens and is never opened;
API keys are detected by the presence of a variable name, never its value.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BACKEND_VERTEX = "vertex"
BACKEND_BEDROCK = "bedrock"
BACKEND_ANTHROPIC = "anthropic"
BACKEND_ANTHROPIC_OAUTH = "anthropic-oauth"
BACKEND_ANTHROPIC_API = "anthropic-api"
BACKEND_MIXED = "mixed"
BACKEND_UNKNOWN = "unknown"

# Every backend value a Claude row can carry, in display order.
BACKENDS = (
    BACKEND_ANTHROPIC_OAUTH, BACKEND_ANTHROPIC_API, BACKEND_ANTHROPIC, BACKEND_VERTEX, BACKEND_BEDROCK,
    BACKEND_MIXED, BACKEND_UNKNOWN,
)
BACKEND_LABELS = {
    BACKEND_ANTHROPIC_OAUTH: "Claude subscription",
    BACKEND_ANTHROPIC_API: "Anthropic API",
    BACKEND_ANTHROPIC: "Anthropic (unverified)",
    BACKEND_VERTEX: "Vertex AI",
    BACKEND_BEDROCK: "Bedrock",
    BACKEND_MIXED: "Mixed backends",
    BACKEND_UNKNOWN: "Unknown",
}

_API_KEY_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_VERTEX_VARIABLES = ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION")
_BEDROCK_VARIABLES = ("CLAUDE_CODE_USE_BEDROCK",)
_SCALAR_LIMIT = 100


def message_backend(message_id: Any, request_id: Any = None) -> str:
    """Classify one assistant message by the provider prefix of its identifiers."""

    message = message_id if isinstance(message_id, str) else ""
    request = request_id if isinstance(request_id, str) else ""
    if message.startswith("msg_vrtx_") or request.startswith("req_vrtx_"):
        return BACKEND_VERTEX
    if message.startswith("msg_bdrk_"):
        return BACKEND_BEDROCK
    if message.startswith("msg_"):
        return BACKEND_ANTHROPIC
    return BACKEND_UNKNOWN


@dataclass(frozen=True, slots=True)
class ClaudeAuthProfile:
    """Non-secret summary of how a Claude Code home authenticates."""

    oauth: bool = False
    billing_type: str | None = None
    organization_type: str | None = None
    organization_name: str | None = None
    # A key configured right now (environment, settings env, apiKeyHelper).
    api_key_hint: bool = False
    # A key was approved at some point; Claude Code keeps the approval list
    # after the key is gone, so this only matters when nothing else is known.
    api_key_history: bool = False
    vertex_hint: bool = False
    bedrock_hint: bool = False
    config_path: Path | None = None
    override: str = "auto"

    @property
    def anthropic_backend(self) -> str:
        """Backend recorded for ``msg_`` calls that transcripts cannot classify."""

        if self.override == "subscription":
            return BACKEND_ANTHROPIC_OAUTH
        if self.override == "api":
            return BACKEND_ANTHROPIC_API
        # Claude Code's documented credential precedence puts an active API
        # key/token/helper ahead of the subscription OAuth login.  A stale
        # approval-history entry is not evidence that a key is still active.
        if self.api_key_hint:
            return BACKEND_ANTHROPIC_API
        if self.oauth:
            return BACKEND_ANTHROPIC_OAUTH
        return BACKEND_ANTHROPIC

    @property
    def ambiguous(self) -> bool:
        return self.override == "auto" and self.oauth and self.api_key_hint

    @property
    def login(self) -> str:
        if self.oauth:
            parts = (self.organization_type, self.billing_type, self.organization_name)
            detail = ", ".join(item for item in parts if item)
            return f"oauth ({detail})" if detail else "oauth"
        if self.api_key_hint:
            return "api-key"
        if self.api_key_history:
            return "api-key (previously approved)"
        return "unknown"


def _scalar(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:_SCALAR_LIMIT] or None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _config_candidates(home: Path) -> tuple[Path, ...]:
    # ``CLAUDE_CONFIG_DIR`` keeps the profile inside the home; the default
    # layout stores it as ``~/.claude.json`` next to ``~/.claude``.
    return (home / ".claude.json", home.parent / ".claude.json")


def read_auth_profile(
    home: Path, environ: Mapping[str, str] | None = None, *, override: str = "auto"
) -> ClaudeAuthProfile:
    """Read the allow-listed login and backend hints of a Claude Code home."""

    environ = os.environ if environ is None else environ
    oauth = False
    billing_type = organization_type = organization_name = None
    api_key_hint = api_key_history = False
    config_path = None
    for candidate in _config_candidates(home):
        data = _load_json(candidate)
        if data is None:
            continue
        config_path = candidate
        account = data.get("oauthAccount")
        if isinstance(account, dict) and account:
            oauth = True
            billing_type = _scalar(account.get("billingType"))
            organization_type = _scalar(account.get("organizationType"))
            organization_name = _scalar(account.get("organizationName"))
        responses = data.get("customApiKeyResponses")
        if isinstance(responses, dict) and isinstance(responses.get("approved"), list):
            api_key_history = bool(responses["approved"])
        break

    vertex_hint = bedrock_hint = False
    settings = _load_json(home / "settings.json") or {}
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    if isinstance(settings.get("apiKeyHelper"), str) and settings["apiKeyHelper"].strip():
        api_key_hint = True
    for source in (env, environ):
        if any(name in source for name in _API_KEY_VARIABLES):
            api_key_hint = True
        if _truthy(source.get("CLAUDE_CODE_USE_VERTEX")) or any(
            name in source for name in _VERTEX_VARIABLES[1:]
        ):
            vertex_hint = True
        if any(_truthy(source.get(name)) for name in _BEDROCK_VARIABLES):
            bedrock_hint = True
    return ClaudeAuthProfile(
        oauth=oauth, billing_type=billing_type, organization_type=organization_type,
        organization_name=organization_name, api_key_hint=api_key_hint, api_key_history=api_key_history,
        vertex_hint=vertex_hint, bedrock_hint=bedrock_hint, config_path=config_path,
        override=override if override in {"auto", "subscription", "api"} else "auto",
    )
