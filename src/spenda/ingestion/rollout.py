from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..models import TokenUsage


@dataclass(slots=True)
class ParserContext:
    owner_thread_id: str | None = None
    turn_id: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    provider: str | None = None
    service_tier: str | None = None
    previous_cumulative: TokenUsage | None = None
    recent_atomic: tuple[int, ...] | None = None
    pending_call_label: str | None = None
    pending_call_priority: int = 0


@dataclass(slots=True)
class UsageRecord:
    identity: str
    thread_id: str
    turn_id: str | None
    response_id: str | None
    timestamp: str
    model: str
    provider: str
    reasoning_effort: str | None
    usage: TokenUsage
    ordinal: int | None
    event_type: str
    call_label: str | None
    service_tier: str | None = None


@dataclass(slots=True)
class ParseResult:
    usage: UsageRecord | None = None
    session_meta: dict[str, Any] | None = None
    session_timestamp: str | None = None
    warning: tuple[str, str] | None = None
    ignored_type: str | None = None


def _identity(*parts: Any) -> str:
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()


class RolloutParser:
    """Parser for observed ordinary Codex rollout records.

    It intentionally accepts dictionaries rather than binding to one Codex
    version's complete JSON schema.
    """

    def __init__(self, context: ParserContext | None = None):
        self.context = context or ParserContext()

    def _remember_call_label(self, label: str | None, priority: int) -> None:
        if label and priority >= self.context.pending_call_priority:
            self.context.pending_call_label = label
            self.context.pending_call_priority = priority

    def _take_call_label(self) -> str | None:
        label = self.context.pending_call_label
        self.context.pending_call_label = None
        self.context.pending_call_priority = 0
        return label

    def parse(self, record: Any, source_key: str) -> ParseResult:
        if not isinstance(record, dict):
            return ParseResult(warning=("malformed_record", "JSON value is not an object"))
        kind = record.get("type")
        payload = record.get("payload")
        ordinal = record.get("ordinal") if isinstance(record.get("ordinal"), int) else None
        timestamp = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None

        if kind == "session_meta" and isinstance(payload, dict):
            thread_id = payload.get("id")
            if isinstance(thread_id, str) and (self.context.owner_thread_id is None or ordinal == 0):
                self.context.owner_thread_id = thread_id
            provider = payload.get("model_provider")
            if isinstance(provider, str):
                self.context.provider = provider
            return ParseResult(session_meta=payload, session_timestamp=timestamp)

        if kind == "event_msg" and isinstance(payload, dict) and payload.get("type") == "thread_settings_applied":
            # The tier applies until the thread's settings change again; Codex
            # writes it here only (``service_tier`` "default", "priority", ...).
            settings = payload.get("thread_settings")
            thread_id = payload.get("thread_id")
            owner = self.context.owner_thread_id
            if isinstance(settings, dict) and (not isinstance(thread_id, str) or not owner or thread_id == owner):
                tier = settings.get("service_tier")
                self.context.service_tier = tier if isinstance(tier, str) else None
            return ParseResult(ignored_type="event_msg:thread_settings_applied")

        if kind == "turn_context" and isinstance(payload, dict):
            self._take_call_label()
            if isinstance(payload.get("turn_id"), str):
                self.context.turn_id = payload["turn_id"]
            self.context.model = payload.get("model") if isinstance(payload.get("model"), str) else None
            effort = payload.get("reasoning_effort")
            self.context.reasoning_effort = effort if isinstance(effort, str) else None
            return ParseResult()

        if kind == "response_item" and isinstance(payload, dict):
            item_type = payload.get("type")
            if item_type == "message" and payload.get("role") == "assistant":
                phase = payload.get("phase")
                if phase == "final_answer":
                    self._remember_call_label("Final response", 100)
                elif phase == "commentary":
                    self._remember_call_label("Assistant update", 10)
            elif item_type in {"custom_tool_call", "function_call", "local_shell_call"}:
                raw_input = payload.get("input", payload.get("arguments", payload.get("action")))
                self._remember_call_label(_tool_call_label(payload.get("name") or item_type, raw_input), 60)
            return ParseResult(ignored_type=f"response_item:{item_type or '<missing>'}")

        if kind == "event_msg" and isinstance(payload, dict) and payload.get("type") == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "FileChange":
                    self._remember_call_label("Apply file change", 90)
                elif item_type == "ImageView":
                    self._remember_call_label("Inspect image", 90)
                elif item_type == "CommandExecution":
                    self._remember_call_label(
                        _tool_call_label("exec", item.get("parsed_cmd", item.get("command"))), 70
                    )
                elif item_type == "Extension":
                    self._remember_call_label(_extension_label(item), 65)
                elif item_type == "AgentMessage":
                    phase = item.get("phase")
                    if phase == "final_answer":
                        self._remember_call_label("Final response", 100)
                    elif phase == "commentary":
                        self._remember_call_label("Assistant update", 10)
            return ParseResult(ignored_type="event_msg:item_completed")

        if kind == "token_usage_record" and isinstance(payload, dict):
            usage = TokenUsage.from_mapping(payload.get("usage"))
            thread_id = payload.get("thread_id") or self.context.owner_thread_id
            if usage is None or not isinstance(thread_id, str):
                return ParseResult(warning=("invalid_atomic_usage", "atomic usage lacks usage or thread_id"))
            response_id = payload.get("response_id") if isinstance(payload.get("response_id"), str) else None
            turn_id = payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else self.context.turn_id
            identity = f"response:{response_id}" if response_id else _identity(
                "atomic", thread_id, turn_id, timestamp, ordinal, usage.fingerprint()
            )
            self.context.recent_atomic = usage.fingerprint()
            call_label = self._take_call_label()
            return ParseResult(usage=UsageRecord(
                identity, thread_id, turn_id, response_id, timestamp or _timestamp_fallback(),
                self.context.model or "unknown-model", self.context.provider or "unknown-provider",
                self.context.reasoning_effort,
                usage, ordinal, "token_usage_record", call_label, self.context.service_tier,
            ))

        if kind == "event_msg" and isinstance(payload, dict) and payload.get("type") == "token_count":
            info = payload.get("info")
            if not isinstance(info, dict):
                self._take_call_label()
                return ParseResult()
            last = TokenUsage.from_mapping(info.get("last_token_usage"))
            event_type = "token_count:last"
            reset_warning = None
            total = TokenUsage.from_mapping(info.get("total_token_usage"))
            if last is not None and self.context.recent_atomic == last.fingerprint():
                if total is not None:
                    self.context.previous_cumulative = total
                self.context.recent_atomic = None
                self._take_call_label()
                return ParseResult(ignored_type="duplicate-token-count")
            usage = last
            if total is not None:
                previous = self.context.previous_cumulative
                if previous is None:
                    usage = last or total
                else:
                    delta, reset = total.delta_from(previous)
                    if reset:
                        # After a process/session counter reset, last usage is
                        # the least ambiguous atomic value when available.
                        usage = last or total
                        reset_warning = ("counter_reset", "cumulative token counter decreased; per-response usage used")
                    elif delta.total_tokens == 0:
                        self.context.previous_cumulative = total
                        self.context.recent_atomic = None
                        self._take_call_label()
                        return ParseResult(ignored_type="duplicate-token-count")
                    else:
                        # Older Codex versions can repeat last_token_usage even
                        # when the cumulative snapshot has not advanced. The
                        # categorized cumulative delta is the safe source.
                        usage = delta
                        event_type = "token_count:cumulative_delta"
            if total is not None:
                self.context.previous_cumulative = total
            if usage is None:
                self._take_call_label()
                return ParseResult()
            if usage.total_tokens == 0:
                self._take_call_label()
                return ParseResult(ignored_type="empty-token-count")
            thread_id = self.context.owner_thread_id
            if not thread_id:
                return ParseResult(warning=("usage_without_thread", "token_count appeared before session metadata"))
            identity = _identity(
                "fallback", thread_id, self.context.turn_id, timestamp, ordinal, usage.fingerprint()
            )
            parsed = UsageRecord(
                identity, thread_id, self.context.turn_id, None, timestamp or _timestamp_fallback(),
                self.context.model or "unknown-model", self.context.provider or "unknown-provider",
                self.context.reasoning_effort,
                usage, ordinal, event_type, self._take_call_label(), self.context.service_tier,
            )
            return ParseResult(usage=parsed, warning=reset_warning)

        return ParseResult(ignored_type=str(kind or "<missing>"))


def _timestamp_fallback() -> str:
    # Only used for malformed historical data; it intentionally does not claim
    # current time, which would make repeated imports non-deterministic.
    return datetime.min.isoformat() + "Z"


def _tool_call_label(name: Any, raw_input: Any) -> str:
    tool = name if isinstance(name, str) else ""
    try:
        raw = raw_input if isinstance(raw_input, str) else json.dumps(raw_input, separators=(",", ":"))
    except (TypeError, ValueError):
        raw = str(raw_input or "")
    haystack = f"{tool} {raw}".lower()
    if "apply_patch" in haystack or tool in {"apply_patch", "patch"}:
        return "Apply file change"
    if "view_image" in haystack or tool in {"view_image", "image_view"}:
        return "Inspect image"
    if any(
        marker in haystack
        for marker in ("pytest", "unittest", "run-unit-tests", "run-functional-tests", " tox ", " nox ")
    ):
        return "Run tests"
    if "compileall" in haystack or " py_compile" in haystack:
        return "Check Python syntax"
    if tool in {"wait", "wait_agent"}:
        return "Wait for activity"
    if "web__run" in haystack or tool in {"web_search", "search_query"}:
        return "Search the web"
    if tool in {"spawn_agent", "create_agent"} or "spawn_agent" in haystack:
        return "Start subagent"
    if tool in {"send_message", "followup_task"} or "send_message" in haystack or "followup_task" in haystack:
        return "Message subagent"
    if tool == "exec" or "exec_command" in haystack or tool in {"exec_command", "local_shell_call"}:
        if "rg " in haystack or "ripgrep" in haystack or "grep " in haystack:
            return "Search files"
        if any(marker in haystack for marker in ("sed -n", "head ", "tail ", "read_mcp_resource")):
            return "Read files"
        if "git status" in haystack or "git diff" in haystack or "git log" in haystack:
            return "Inspect repository"
        return "Run command"
    if tool:
        readable = tool.replace("__", " ").replace("_", " ").replace("-", " ").strip()
        return f"Use {readable}"
    return "Use tool"


def _extension_label(item: dict[str, Any]) -> str:
    action = item.get("action")
    kind = item.get("kind")
    text = " ".join(value for value in (action, kind) if isinstance(value, str)).lower()
    if "search" in text:
        return "Search files"
    if "read" in text:
        return "Read files"
    return "Use extension"


def context_from_row(row: Any) -> ParserContext:
    previous = None
    recent = None
    try:
        if row and row["previous_cumulative_json"]:
            previous = TokenUsage.from_mapping(json.loads(row["previous_cumulative_json"]))
        if row and row["recent_atomic_json"]:
            recent = tuple(json.loads(row["recent_atomic_json"]))
    except (json.JSONDecodeError, TypeError, KeyError):
        pass
    return ParserContext(
        owner_thread_id=row["owner_thread_id"] if row else None,
        turn_id=row["current_turn_id"] if row else None,
        model=row["current_model"] if row else None,
        reasoning_effort=row["current_reasoning_effort"] if row else None,
        provider=row["current_provider"] if row else None,
        service_tier=row["current_service_tier"] if row and "current_service_tier" in row.keys() else None,
        previous_cumulative=previous,
        recent_atomic=recent,
        pending_call_label=row["pending_call_label"] if row and "pending_call_label" in row.keys() else None,
        pending_call_priority=(row["pending_call_priority"] or 0)
        if row and "pending_call_priority" in row.keys() else 0,
    )


def context_json(usage: TokenUsage | None) -> str | None:
    if usage is None:
        return None
    return json.dumps({field: getattr(usage, field) for field in usage.__dataclass_fields__}, separators=(",", ":"))
