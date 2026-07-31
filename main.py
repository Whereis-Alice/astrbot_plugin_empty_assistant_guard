"""Diagnose and optionally repair empty assistant messages in AstrBot payloads."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import time
from collections.abc import Iterable
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register


PLUGIN_ID = "astrbot_plugin_empty_assistant_guard"
PLUGIN_VERSION = "0.1.4"
PLUGIN_DESC = "定位并可选修复 OpenAI 兼容请求中的空 assistant 消息"
PLUGIN_REPO = "https://github.com/Whereis-Alice/astrbot_plugin_empty_assistant_guard"

EARLY_PRIORITY = 10000
LATE_PRIORITY = -10000
STATE_EXTRA_KEY = f"{PLUGIN_ID}.state"
STATE_ATTR = f"_{PLUGIN_ID}_state"
PREVIEW_FALLBACK = "-"

_ACTIVE_PLUGIN: "EmptyAssistantGuardPlugin | None" = None
_PATCHED = False
_RUNNER_PATCHED = False
_PATCH_ORIGINALS: dict[str, Any] = {}
_PATCH_CLASSES: dict[str, Any] = {}
_PATCH_WRAPPERS: dict[str, Any] = {}


@dataclass
class MessageFinding:
    index: int
    reason: str
    content_preview: str
    prev_role: str
    next_role: str
    following_tools: list[str] = field(default_factory=list)
    likely_cause: str = ""


@dataclass
class PhaseRecord:
    phase: str
    message_count: int
    bad_count: int
    roles: dict[str, int]
    total_chars: int
    findings: list[MessageFinding] = field(default_factory=list)
    previews: list[str] = field(default_factory=list)
    when: float = field(default_factory=time.time)


@dataclass
class MutationRecord:
    channel: str
    action: str
    source: str
    count: int
    preview: str
    introduced_bad_assistant: bool = False
    when: float = field(default_factory=time.time)


@dataclass
class ToolTrace:
    phase: str
    name: str
    source: str
    args_preview: str = ""
    result_preview: str = ""
    result_empty: bool = False
    when: float = field(default_factory=time.time)


@dataclass
class RepairRecord:
    phase: str
    action: str
    before_messages: int
    after_messages: int
    changes: list[str]
    when: float = field(default_factory=time.time)


@dataclass
class AuditState:
    request_id: str
    started_at: float
    umo: str
    session_id: str
    dump_dir: str
    phases: list[PhaseRecord] = field(default_factory=list)
    mutations: list[MutationRecord] = field(default_factory=list)
    tools: list[ToolTrace] = field(default_factory=list)
    repairs: list[RepairRecord] = field(default_factory=list)
    provider_model: str = ""
    provider_action: str = "repair"
    blocked: bool = False


class TrackedRequestList(list[Any]):
    """Track request list mutations made by later on_llm_request hooks."""

    def __init__(
        self,
        values: Iterable[Any],
        *,
        channel: str,
        owner: "EmptyAssistantGuardPlugin",
        state: AuditState,
    ) -> None:
        super().__init__(values)
        self._channel = channel
        self._owner = owner
        self._state = state

    def append(self, item: Any) -> None:  # type: ignore[override]
        super().append(item)
        self._record("append", [item])

    def extend(self, values: Iterable[Any]) -> None:  # type: ignore[override]
        values_list = list(values)
        super().extend(values_list)
        self._record("extend", values_list)

    def insert(self, index: int, item: Any) -> None:  # type: ignore[override]
        super().insert(index, item)
        self._record("insert", [item])

    def pop(self, index: int = -1) -> Any:  # type: ignore[override]
        item = super().pop(index)
        self._record("pop", [item])
        return item

    def clear(self) -> None:  # type: ignore[override]
        removed = list(self)
        super().clear()
        self._record("clear", removed)

    def __iadd__(self, values: Iterable[Any]) -> "TrackedRequestList":
        self.extend(values)
        return self

    def __setitem__(self, index: Any, value: Any) -> None:
        if isinstance(index, slice):
            values = list(value)
            super().__setitem__(index, values)
        else:
            values = [value]
            super().__setitem__(index, value)
        self._record("setitem", values)

    def _record(self, action: str, values: list[Any]) -> None:
        self._owner.record_mutation(self._state, self._channel, action, values)


@register(PLUGIN_ID, "Whereis-Alice", PLUGIN_DESC, PLUGIN_VERSION, PLUGIN_REPO)
class EmptyAssistantGuardPlugin(Star):
    """Find empty assistant messages and optionally repair unsafe provider payloads."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | dict[str, Any] | None = None,
    ) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self._data_dir = Path(StarTools.get_data_dir(PLUGIN_ID))
        self._requests_dir = self._data_dir / "requests"
        self._last_state_by_umo: dict[str, AuditState] = {}
        self._last_state_by_session: dict[str, AuditState] = {}
        self._recent_states_by_umo: dict[str, deque[AuditState]] = {}
        self._recent_tools_by_umo: dict[str, deque[ToolTrace]] = {}
        self._plugin_file = Path(__file__).resolve()

    async def initialize(self) -> None:
        self._requests_dir.mkdir(parents=True, exist_ok=True)
        self._set_active()
        if self._cfg_bool("patch_openai_provider", True):
            self._apply_provider_patches()
        if self._cfg_bool("patch_agent_runner", True):
            self._apply_runner_patches()
        if self._provider_action() == "report_only":
            logger.warning(
                "[%s] 当前配置 provider_action=report_only：只记录，不会修复空 assistant；"
                "要解决上游 400 请改为 repair。",
                PLUGIN_ID,
            )
        logger.info("[%s] initialized", PLUGIN_ID)

    async def terminate(self) -> None:
        self._restore_provider_patches()
        self._clear_active()
        logger.info("[%s] terminated", PLUGIN_ID)

    @filter.on_agent_begin(priority=EARLY_PRIORITY)
    async def inspect_agent_begin(self, event: AstrMessageEvent, run_context: Any) -> None:
        if not self._cfg_bool("enabled", True):
            return
        if not self._cfg_bool("inspect_agent_context", True):
            return

        state = self._new_state(event.unified_msg_origin, session_id="")
        self._set_state_on_event(event, state)
        self._record_phase(
            state,
            "agent_begin",
            getattr(run_context, "messages", []) or [],
        )
        self._remember_state(state)

    @filter.on_llm_request(priority=EARLY_PRIORITY)
    async def inspect_request_early(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self._cfg_bool("enabled", True):
            return
        if not self._cfg_bool("inspect_request_context", True):
            return

        session_id = self._session_key(getattr(req, "session_id", "") or "")
        state = self._event_state(event)
        if state is None or self._state_has_request_phase(state):
            state = self._new_state(event.unified_msg_origin, session_id=session_id)
        else:
            state.session_id = session_id
        self._set_state_on_event(event, state)
        self._set_state_on_request(req, state)
        self._attach_recent_tools(state, event.unified_msg_origin)

        contexts = getattr(req, "contexts", []) or []
        self._record_phase(state, "request_early", contexts)

        req.contexts = TrackedRequestList(
            contexts,
            channel="request.contexts",
            owner=self,
            state=state,
        )
        extra_parts = getattr(req, "extra_user_content_parts", []) or []
        req.extra_user_content_parts = TrackedRequestList(
            extra_parts,
            channel="request.extra_user_content_parts",
            owner=self,
            state=state,
        )
        self._remember_state(state)

    @filter.on_llm_request(priority=LATE_PRIORITY)
    async def inspect_request_late(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self._cfg_bool("enabled", True):
            return
        if not self._cfg_bool("inspect_request_context", True):
            return

        state = self._get_state(event, req)
        if state is None:
            return
        self._record_phase(state, "request_late", getattr(req, "contexts", []) or [])
        self._remember_state(state)

    @filter.on_using_llm_tool(priority=EARLY_PRIORITY)
    async def record_tool_start(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: dict | None,
    ) -> None:
        if not self._cfg_bool("enabled", True):
            return
        if not self._cfg_bool("record_tool_events", True):
            return

        trace = ToolTrace(
            phase="tool_start",
            name=self._tool_name(tool),
            source=self._tool_source(tool),
            args_preview=self._preview_value(tool_args, limit=self._cfg_int("tool_preview_chars", 240)),
        )
        self._remember_tool(event.unified_msg_origin, trace)
        self._append_tool_to_current_state(event, trace)

    @filter.on_llm_tool_respond(priority=LATE_PRIORITY)
    async def record_tool_result(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: dict | None,
        tool_result: Any,
    ) -> None:
        if not self._cfg_bool("enabled", True):
            return
        if not self._cfg_bool("record_tool_events", True):
            return

        text = self._tool_result_text(tool_result)
        trace = ToolTrace(
            phase="tool_result",
            name=self._tool_name(tool),
            source=self._tool_source(tool),
            args_preview=self._preview_value(tool_args, limit=self._cfg_int("tool_preview_chars", 240)),
            result_preview=self._preview_text(text, limit=self._cfg_int("tool_preview_chars", 240)),
            result_empty=not bool(text.strip()),
        )
        self._remember_tool(event.unified_msg_origin, trace)
        self._append_tool_to_current_state(event, trace)

    @filter.on_agent_done(priority=LATE_PRIORITY)
    async def inspect_agent_done(
        self,
        event: AstrMessageEvent,
        run_context: Any,
        _response: Any,
    ) -> None:
        if not self._cfg_bool("enabled", True):
            return
        if not self._cfg_bool("inspect_agent_context", True):
            return

        state = self._last_state_by_umo.get(event.unified_msg_origin)
        if state is None:
            state = self._new_state(event.unified_msg_origin, session_id="")
            self._set_state_on_event(event, state)
        messages = getattr(run_context, "messages", []) or []
        self._record_phase(state, "agent_done", messages)

        if self._cfg_bool("mark_empty_agent_messages_no_save", False):
            marked = self._mark_bad_agent_messages_no_save(messages)
            if marked:
                repair = RepairRecord(
                    phase="agent_done",
                    action="mark_no_save",
                    before_messages=len(list(messages)),
                    after_messages=len(list(messages)),
                    changes=[f"marked {marked} empty assistant message(s) as _no_save"],
                )
                state.repairs.append(repair)
                self._append_dump_event(state, "agent_done_mark_no_save", asdict(repair))
                logger.warning("[%s] marked %s empty assistant message(s) as _no_save", PLUGIN_ID, marked)

        self._remember_state(state)

    @filter.command("empty_assistant_guard_status")
    async def empty_assistant_guard_status(self, event: AstrMessageEvent):
        """Show the latest matching empty-assistant audit for this conversation."""
        state = self._state_for_status(event.unified_msg_origin)
        if state is None:
            yield event.plain_result(self._missing_status_message(event.unified_msg_origin))
            return
        yield event.plain_result(self._format_status(state))

    @filter.command("empty_assistant_guard_dump")
    async def empty_assistant_guard_dump(self, event: AstrMessageEvent):
        """Show the dump path for the latest matching audit in this conversation."""
        state = self._state_for_status(event.unified_msg_origin)
        if state is None:
            yield event.plain_result(self._missing_status_message(event.unified_msg_origin))
            return
        yield event.plain_result(f"EmptyAssistantGuard dump: {state.dump_dir}")

    def record_mutation(
        self,
        state: AuditState,
        channel: str,
        action: str,
        values: list[Any],
    ) -> None:
        source = self._infer_mutation_source()
        if source.startswith(f"{PLUGIN_ID}/"):
            return
        findings = self._find_bad_assistant_messages(values)
        record = MutationRecord(
            channel=channel,
            action=action,
            source=source,
            count=len(values),
            preview=self._preview_value(values[0] if values else None),
            introduced_bad_assistant=bool(findings),
        )
        state.mutations.append(record)
        self._append_dump_event(state, "request_mutation", asdict(record))

    def record_assignment(
        self,
        req: ProviderRequest,
        name: str,
        value: Any,
    ) -> None:
        state = getattr(req, STATE_ATTR, None)
        if not isinstance(state, AuditState):
            return
        source = self._infer_mutation_source()
        if source.startswith(f"{PLUGIN_ID}/"):
            return

        values = value if isinstance(value, list) else [value]
        findings = self._find_bad_assistant_messages(values)
        record = MutationRecord(
            channel=f"request.{name}",
            action="setattr",
            source=source,
            count=len(values),
            preview=self._preview_value(value),
            introduced_bad_assistant=bool(findings),
        )
        state.mutations.append(record)
        self._append_dump_event(state, "request_assignment", asdict(record))

    def sanitize_runner_contexts(
        self,
        runner: Any,
        contexts: Any,
    ) -> Any:
        if not self._cfg_bool("enabled", True):
            return contexts

        state = self._state_for_runner(runner) or self._new_runner_state(runner)
        provider_id = self._runner_provider_id(runner)
        action = self._provider_action()
        if state is not None:
            state.provider_model = provider_id or state.provider_model
            state.provider_action = action
            self._remember_state(state)

        context_list = list(contexts or [])
        findings = self._find_bad_assistant_messages(context_list)
        if not findings:
            return contexts

        if state is not None:
            self._record_phase(state, "runner_contexts_before_provider", context_list)
            self._remember_state(state)

        if action not in {"repair", "fix"}:
            logger.warning(
                "[%s] runner contexts contain %s empty assistant message(s), but provider_action=%s provider=%s",
                PLUGIN_ID,
                len(findings),
                action,
                provider_id or PREVIEW_FALLBACK,
            )
            return contexts

        repaired_messages, changes = self._repair_payload_messages(
            context_list,
            strategy=self._cfg_str("repair_strategy", "drop").strip().lower(),
        )
        if not changes:
            return contexts

        if state is not None:
            repair = RepairRecord(
                phase="runner_contexts_before_provider",
                action="repair:runner_contexts",
                before_messages=len(context_list),
                after_messages=len(repaired_messages),
                changes=changes,
            )
            state.repairs.append(repair)
            self._append_dump_event(state, "runner_contexts_repair", asdict(repair))
            self._record_phase(state, "runner_contexts_repaired", repaired_messages)
            self._remember_state(state)

        logger.warning(
            "[%s] repaired runner contexts before provider=%s: before=%s after=%s changes=%s",
            PLUGIN_ID,
            provider_id or PREVIEW_FALLBACK,
            len(context_list),
            len(repaired_messages),
            " | ".join(changes),
        )
        return repaired_messages

    async def cleanup_runner_after_complete(
        self,
        runner: Any,
        before_count: int,
    ) -> None:
        if not self._cfg_bool("enabled", True):
            return
        messages = getattr(getattr(runner, "run_context", None), "messages", None)
        if not isinstance(messages, list) or len(messages) <= before_count:
            return

        removed = 0
        while len(messages) > before_count:
            last = messages[-1]
            if not self._is_bad_assistant_message(self._ensure_message_dict(last)):
                break
            messages.pop()
            removed += 1

        if not removed:
            return

        state = self._state_for_runner(runner)
        provider_id = self._runner_provider_id(runner)
        if state is not None:
            repair = RepairRecord(
                phase="runner_complete",
                action="remove_empty_assistant_append",
                before_messages=before_count + removed,
                after_messages=before_count,
                changes=[f"removed {removed} empty assistant message(s) appended by runner"],
            )
            state.repairs.append(repair)
            self._append_dump_event(state, "runner_complete_repair", asdict(repair))
            self._remember_state(state)

        logger.warning(
            "[%s] removed %s empty assistant message(s) appended by ToolLoopAgentRunner provider=%s",
            PLUGIN_ID,
            removed,
            provider_id or PREVIEW_FALLBACK,
        )

    def on_provider_payload_prepared(
        self,
        provider: Any,
        payloads: dict[str, Any],
        context_query: list[Any],
    ) -> None:
        state = self._state_for_provider(provider)
        if state is None:
            return

        state.provider_model = str(payloads.get("model") or "")
        state.provider_action = self._provider_action()
        messages = payloads.get("messages", []) or []
        phase = self._record_phase(state, "provider_prepare", messages)
        self._remember_state(state)

        if phase.bad_count <= 0:
            return

        self._log_bad_payload(state, phase)
        self._repair_or_block_payload(
            state=state,
            payloads=payloads,
            context_query=context_query,
            phase="provider_prepare",
            before_messages=messages,
        )

    def on_provider_payload_sanitize(
        self,
        payloads: dict[str, Any],
    ) -> None:
        if not self._cfg_bool("enabled", True):
            return

        state = self._latest_state()
        self._repair_or_block_payload(
            state=state,
            payloads=payloads,
            context_query=None,
            phase="provider_sanitize",
            before_messages=payloads.get("messages", []) or [],
        )

    def on_provider_api_error(
        self,
        provider: Any,
        error: Exception,
        payloads: dict[str, Any],
        context_query: list[Any],
        func_tool: Any,
        chosen_key: str,
        available_api_keys: list[str],
        image_fallback_used: bool,
    ) -> tuple[Any, ...] | None:
        if not self._cfg_bool("enabled", True):
            return None
        if not self._is_empty_assistant_error(error):
            return None

        state = self._state_for_provider(provider) or self._latest_state()
        before_messages = payloads.get("messages", []) or []
        changed = self._repair_or_block_payload(
            state=state,
            payloads=payloads,
            context_query=context_query,
            phase="provider_error_retry",
            before_messages=before_messages,
        )
        if not changed:
            return None

        logger.warning(
            "[%s] repaired payload after empty-assistant API error; retrying request",
            PLUGIN_ID,
        )
        return (
            False,
            chosen_key,
            available_api_keys,
            payloads,
            context_query,
            func_tool,
            image_fallback_used,
        )

    def _repair_or_block_payload(
        self,
        *,
        state: AuditState | None,
        payloads: dict[str, Any],
        context_query: list[Any] | None,
        phase: str,
        before_messages: Any,
    ) -> bool:
        messages = payloads.get("messages", []) or []
        findings = self._find_bad_assistant_messages(messages)
        if not findings:
            return False

        if state is not None:
            state.provider_action = self._provider_action()
            self._append_dump_event(
                state,
                f"{phase}_unsafe",
                {
                    "bad_count": len(findings),
                    "findings": [asdict(item) for item in findings],
                },
            )

        action = self._provider_action()
        if action in {"repair", "fix"}:
            strategy = self._cfg_str("repair_strategy", "drop").strip().lower()
            repaired_messages, changes = self._repair_payload_messages(
                messages,
                strategy=strategy,
            )
            if changes:
                payloads["messages"] = repaired_messages
                if (
                    self._cfg_bool("sync_context_query_after_repair", True)
                    and isinstance(context_query, list)
                ):
                    context_query[:] = copy.deepcopy(repaired_messages)
                repair = RepairRecord(
                    phase=phase,
                    action=f"repair:{strategy}",
                    before_messages=len(list(before_messages or [])),
                    after_messages=len(repaired_messages),
                    changes=changes,
                )
                if state is not None:
                    state.repairs.append(repair)
                    self._append_dump_event(state, f"{phase}_repair", asdict(repair))
                    self._record_phase(state, f"{phase}_repaired", repaired_messages)
                    self._remember_state(state)
                logger.warning(
                    "[%s] repaired unsafe provider payload: before=%s after=%s changes=%s",
                    PLUGIN_ID,
                    repair.before_messages,
                    repair.after_messages,
                    " | ".join(changes),
                )
                return True
            return False

        if action == "block":
            if state is not None:
                state.blocked = True
                self._append_dump_event(
                    state,
                    f"{phase}_block",
                    {
                        "reason": "empty assistant messages detected",
                        "findings": [asdict(item) for item in findings],
                    },
                )
                self._remember_state(state)
            raise RuntimeError(
                "EmptyAssistantGuard blocked unsafe provider payload"
                + (f"; dump={state.dump_dir}" if state is not None else "")
            )

        logger.warning(
            "[%s] detected %s empty assistant message(s), but provider_action=%s",
            PLUGIN_ID,
            len(findings),
            action,
        )
        return False

    def _new_state(self, umo: str, *, session_id: str) -> AuditState:
        request_id = f"{int(time.time() * 1000)}-{hashlib.sha1(str(time.time()).encode()).hexdigest()[:8]}"
        dump_dir = self._request_dump_dir(umo, request_id)
        dump_dir.mkdir(parents=True, exist_ok=True)
        return AuditState(
            request_id=request_id,
            started_at=time.time(),
            umo=umo,
            session_id=session_id,
            dump_dir=str(dump_dir),
        )

    def _remember_state(self, state: AuditState) -> None:
        self._last_state_by_umo[state.umo] = state
        if state.session_id:
            self._last_state_by_session[state.session_id] = state
        self._remember_recent_state(state)
        self._trim_state_cache()

    def _remember_recent_state(self, state: AuditState) -> None:
        limit = max(1, self._cfg_int("recent_request_limit", 20))
        queue = self._recent_states_by_umo.get(state.umo)
        if queue is None or queue.maxlen != limit:
            existing = list(queue)[-limit:] if queue is not None else []
            queue = deque(existing, maxlen=limit)
            self._recent_states_by_umo[state.umo] = queue
        if any(item.request_id == state.request_id for item in queue):
            return
        queue.append(state)

    def _trim_state_cache(self) -> None:
        max_items = self._cfg_int("remember_last_sessions", 50)
        if max_items <= 0:
            self._last_state_by_umo.clear()
            self._last_state_by_session.clear()
            self._recent_states_by_umo.clear()
            return
        while len(self._last_state_by_umo) > max_items:
            first_key = next(iter(self._last_state_by_umo))
            removed = self._last_state_by_umo.pop(first_key)
            if (
                removed.session_id
                and self._last_state_by_session.get(removed.session_id) is removed
            ):
                self._last_state_by_session.pop(removed.session_id, None)
            self._recent_states_by_umo.pop(first_key, None)

    def _set_state_on_event(self, event: AstrMessageEvent, state: AuditState) -> None:
        try:
            event.set_extra(STATE_EXTRA_KEY, state)
        except Exception:
            pass

    def _set_state_on_request(self, req: ProviderRequest, state: AuditState) -> None:
        try:
            setattr(req, STATE_ATTR, state)
        except Exception:
            pass

    def _event_state(self, event: AstrMessageEvent) -> AuditState | None:
        try:
            state = event.get_extra(STATE_EXTRA_KEY, None)
            if isinstance(state, AuditState):
                return state
        except Exception:
            pass
        return None

    def _state_has_request_phase(self, state: AuditState) -> bool:
        request_phases = {
            "request_early",
            "request_late",
            "provider_prepare",
            "provider_repaired",
        }
        return any(phase.phase in request_phases for phase in state.phases)

    def _get_state(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest | None,
    ) -> AuditState | None:
        if req is not None:
            state = getattr(req, STATE_ATTR, None)
            if isinstance(state, AuditState):
                return state
        state = self._event_state(event)
        if state is not None:
            return state
        if req is not None:
            session_id = self._session_key(getattr(req, "session_id", "") or "")
            if session_id:
                return self._last_state_by_session.get(session_id)
        return self._last_state_by_umo.get(event.unified_msg_origin)

    def _state_for_provider(self, provider: Any) -> AuditState | None:
        session_id = self._session_key(
            getattr(provider, f"_{PLUGIN_ID}_current_session_id", "") or ""
        )
        if session_id:
            state = self._last_state_by_session.get(session_id)
            if state is not None:
                return state
        if self._last_state_by_umo:
            last_key = next(reversed(self._last_state_by_umo))
            return self._last_state_by_umo.get(last_key)
        return None

    def _state_for_runner(self, runner: Any) -> AuditState | None:
        req = getattr(runner, "req", None)
        state = getattr(req, STATE_ATTR, None)
        if isinstance(state, AuditState):
            return state
        return None

    def _runner_event(self, runner: Any) -> AstrMessageEvent | None:
        event = getattr(
            getattr(getattr(runner, "run_context", None), "context", None),
            "event",
            None,
        )
        if isinstance(event, AstrMessageEvent):
            return event
        return event

    def _new_runner_state(self, runner: Any) -> AuditState | None:
        req = getattr(runner, "req", None)
        event = self._runner_event(runner)
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return None
        state = self._new_state(
            umo,
            session_id=self._session_key(getattr(req, "session_id", "") or ""),
        )
        if event is not None:
            self._set_state_on_event(event, state)
        if req is not None:
            self._set_state_on_request(req, state)
        self._attach_recent_tools(state, umo)
        self._remember_state(state)
        return state

    def _runner_provider_id(self, runner: Any) -> str:
        provider = getattr(runner, "provider", None)
        provider_config = getattr(provider, "provider_config", None)
        values: list[str] = []
        if isinstance(provider_config, dict):
            for key in ("id", "provider", "model", "model_name"):
                value = provider_config.get(key)
                if value:
                    values.append(str(value))
        get_model = getattr(provider, "get_model", None)
        if callable(get_model):
            try:
                value = get_model()
            except Exception:
                value = ""
            if value:
                values.append(str(value))
        for attr in ("model", "model_name"):
            value = getattr(provider, attr, "")
            if value:
                values.append(str(value))
        req = getattr(runner, "req", None)
        value = getattr(req, "model", "")
        if value:
            values.append(str(value))

        unique: list[str] = []
        for value in values:
            folded = value.casefold()
            if any(folded in existing.casefold() for existing in unique):
                continue
            unique = [existing for existing in unique if existing.casefold() not in folded]
            unique.append(value)
        return "/".join(unique)

    def _latest_state(self) -> AuditState | None:
        if not self._last_state_by_umo:
            return None
        last_key = next(reversed(self._last_state_by_umo))
        return self._last_state_by_umo.get(last_key)

    def _state_for_status(self, umo: str) -> AuditState | None:
        keywords = self._status_model_keywords()
        if not keywords:
            return self._last_state_by_umo.get(umo)
        states = self._recent_states_by_umo.get(umo, ())
        for state in reversed(states):
            model = state.provider_model.casefold()
            if model and any(keyword in model for keyword in keywords):
                return state
        return None

    def _status_model_keywords(self) -> list[str]:
        raw = self._cfg_str("status_model_keywords", "kimi,moonshot")
        normalized = raw.replace("，", ",").replace(";", ",").replace("；", ",")
        return [item.strip().casefold() for item in normalized.split(",") if item.strip()]

    def _missing_status_message(self, umo: str) -> str:
        keywords = self._status_model_keywords()
        if not keywords:
            return "EmptyAssistantGuard: 当前会话还没有记录。"
        latest = self._last_state_by_umo.get(umo)
        latest_model = latest.provider_model if latest is not None else PREVIEW_FALLBACK
        return (
            "EmptyAssistantGuard: 当前会话还没有匹配模型筛选的记录。\n"
            f"status_model_filter: {', '.join(keywords)}\n"
            f"latest_overall_model: {latest_model or PREVIEW_FALLBACK}"
        )

    def _record_phase(
        self,
        state: AuditState,
        phase: str,
        messages: Any,
    ) -> PhaseRecord:
        message_list = list(messages) if isinstance(messages, Iterable) and not isinstance(messages, (str, bytes, dict)) else []
        normalized = [self._ensure_message_dict(item) for item in message_list]
        roles = Counter(str(item.get("role", "unknown")) for item in normalized)
        findings = self._find_bad_assistant_messages(normalized)
        record = PhaseRecord(
            phase=phase,
            message_count=len(normalized),
            bad_count=len(findings),
            roles=dict(roles),
            total_chars=sum(self._message_char_len(item) for item in normalized),
            findings=findings,
            previews=[
                f"{item.get('role', 'unknown')}:{self._preview_value(item.get('content'))}"
                for item in normalized[: self._cfg_int("detail_preview_limit", 8)]
            ],
        )
        state.phases.append(record)
        self._append_dump_event(state, phase, asdict(record))
        if findings:
            logger.warning(
                "[%s] found %s empty assistant message(s) at %s; dump=%s",
                PLUGIN_ID,
                len(findings),
                phase,
                state.dump_dir,
            )
        return record

    def _provider_action(self) -> str:
        action = self._cfg_str("provider_action", "repair").strip().lower()
        if action in {"repair", "fix", "block", "report_only"}:
            return action
        return "repair"

    def _is_empty_assistant_error(self, error: Exception) -> bool:
        text = str(error).lower()
        return (
            "assistant messages must contain text" in text
            and "tool_calls" in text
        )

    def _find_bad_assistant_messages(self, messages: Any) -> list[MessageFinding]:
        if not isinstance(messages, list):
            try:
                messages = list(messages)
            except Exception:
                return []
        normalized = [self._ensure_message_dict(item) for item in messages]
        findings: list[MessageFinding] = []
        for index, item in enumerate(normalized):
            if not self._is_bad_assistant_message(item):
                continue
            prev_role = str(normalized[index - 1].get("role", "")) if index > 0 else ""
            next_role = str(normalized[index + 1].get("role", "")) if index + 1 < len(normalized) else ""
            following_tools = self._following_tool_names(normalized, index)
            findings.append(
                MessageFinding(
                    index=index,
                    reason="assistant message has no text, reasoning_content, function_call, or tool_calls",
                    content_preview=self._preview_value(item.get("content")),
                    prev_role=prev_role,
                    next_role=next_role,
                    following_tools=following_tools,
                    likely_cause=self._likely_cause_for_bad_message(prev_role, next_role, following_tools),
                )
            )
        return findings

    def _is_bad_assistant_message(self, message: dict[str, Any]) -> bool:
        if str(message.get("role", "")).lower() != "assistant":
            return False
        if self._content_has_text(message.get("content")):
            return False
        if self._content_has_text(message.get("reasoning_content")):
            return False
        if self._content_has_text(message.get("reasoning")):
            return False
        if self._has_non_empty_value(message.get("tool_calls")):
            return False
        if self._has_non_empty_value(message.get("function_call")):
            return False
        return True

    def _repair_payload_messages(
        self,
        messages: Any,
        *,
        strategy: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        original = [copy.deepcopy(self._ensure_message_dict(item)) for item in list(messages or [])]
        repaired: list[dict[str, Any]] = []
        changes: list[str] = []
        drop_orphan_tools = self._cfg_bool("drop_orphan_tool_messages", True)
        placeholder = self._cfg_str(
            "assistant_placeholder_text",
            "[Empty assistant message removed before provider request.]",
        )
        tool_group_open = False
        index = 0

        while index < len(original):
            item = original[index]
            role = str(item.get("role", "")).lower()

            if self._is_bad_assistant_message(item):
                if strategy == "placeholder":
                    item["content"] = placeholder
                    repaired.append(item)
                    changes.append(f"replaced empty assistant at index {index} with placeholder")
                else:
                    changes.append(f"dropped empty assistant at index {index}")
                tool_group_open = False
                index += 1

                if drop_orphan_tools:
                    while index < len(original) and str(original[index].get("role", "")).lower() == "tool":
                        changes.append(
                            f"dropped orphan tool message after empty assistant at index {index}"
                        )
                        index += 1
                continue

            if role == "assistant":
                tool_group_open = self._has_non_empty_value(item.get("tool_calls"))
                repaired.append(item)
                index += 1
                continue

            if role == "tool":
                if drop_orphan_tools and not tool_group_open:
                    changes.append(f"dropped orphan tool message at index {index}")
                    index += 1
                    continue
                repaired.append(item)
                index += 1
                continue

            tool_group_open = False
            repaired.append(item)
            index += 1

        return repaired, changes

    def _mark_bad_agent_messages_no_save(self, messages: Any) -> int:
        marked = 0
        for message in list(messages or []):
            item = self._ensure_message_dict(message)
            if not self._is_bad_assistant_message(item):
                continue
            try:
                setattr(message, "_no_save", True)
                marked += 1
            except Exception:
                continue
        return marked

    def _following_tool_names(self, messages: list[dict[str, Any]], index: int) -> list[str]:
        names: list[str] = []
        for item in messages[index + 1 :]:
            if str(item.get("role", "")).lower() != "tool":
                break
            names.append(self._tool_message_name(item))
        return names

    def _likely_cause_for_bad_message(
        self,
        prev_role: str,
        next_role: str,
        following_tools: list[str],
    ) -> str:
        if str(next_role).lower() == "tool" or following_tools:
            tools = ", ".join(following_tools) if following_tools else "unknown tool"
            return f"assistant tool_calls were likely stripped before tool result(s): {tools}"
        if str(prev_role).lower() == "tool":
            return "model returned an empty final assistant message after a tool result"
        return "empty assistant was already in conversation history or was introduced by a hook"

    def _tool_message_name(self, item: dict[str, Any]) -> str:
        for key in ("name", "tool_name", "tool_call_id"):
            value = item.get(key)
            if value:
                return str(value)
        return "unknown_tool"

    def _ensure_message_dict(self, message: Any) -> dict[str, Any]:
        if isinstance(message, dict):
            return message
        for method_name in ("model_dump", "dict", "model_dump_for_context"):
            method = getattr(message, method_name, None)
            if callable(method):
                try:
                    dumped = method()
                    if isinstance(dumped, dict):
                        return dumped
                except Exception:
                    pass

        item: dict[str, Any] = {
            "role": getattr(message, "role", "unknown"),
            "content": getattr(message, "content", None),
        }
        for key in ("reasoning_content", "reasoning", "tool_calls", "function_call", "name", "tool_call_id"):
            if hasattr(message, key):
                item[key] = getattr(message, key, None)
        return item

    def _content_has_text(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, list):
            return any(self._content_has_text(item) for item in value)
        if isinstance(value, dict):
            for key in ("text", "think", "reasoning", "reasoning_content", "content"):
                if self._content_has_text(value.get(key)):
                    return True
            return False
        for attr in ("text", "think", "reasoning", "reasoning_content"):
            if hasattr(value, attr) and self._content_has_text(getattr(value, attr, None)):
                return True
        return False

    def _has_non_empty_value(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, set, dict)):
            return bool(value)
        return True

    def _message_char_len(self, message: dict[str, Any]) -> int:
        total = self._content_char_len(message.get("content"))
        total += self._content_char_len(message.get("reasoning_content"))
        total += self._content_char_len(message.get("reasoning"))
        if message.get("tool_calls"):
            total += len(self._safe_json(message.get("tool_calls")))
        if message.get("function_call"):
            total += len(self._safe_json(message.get("function_call")))
        return total

    def _content_char_len(self, value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, str):
            return len(value)
        if isinstance(value, list):
            return sum(self._content_char_len(item) for item in value)
        if isinstance(value, dict):
            total = 0
            for key in ("text", "think", "reasoning", "reasoning_content", "content"):
                total += self._content_char_len(value.get(key))
            return total or len(self._safe_json(value))
        for attr in ("text", "think", "reasoning", "reasoning_content"):
            if hasattr(value, attr):
                return self._content_char_len(getattr(value, attr, None))
        return len(str(value))

    def _append_dump_event(
        self,
        state: AuditState,
        phase: str,
        payload: dict[str, Any],
    ) -> None:
        dump_dir = Path(state.dump_dir)
        dump_dir.mkdir(parents=True, exist_ok=True)
        event_path = dump_dir / "events.jsonl"
        entry = {
            "phase": phase,
            "at": time.time(),
            "request_id": state.request_id,
            **payload,
        }
        with event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def _request_dump_dir(self, umo: str, request_id: str) -> Path:
        digest = hashlib.sha1(umo.encode("utf-8")).hexdigest()[:10]
        return self._requests_dir / f"{digest}-{request_id}"

    def _attach_recent_tools(self, state: AuditState, umo: str) -> None:
        recent = self._recent_tools_by_umo.get(umo)
        if not recent:
            return
        state.tools.extend(list(recent)[-self._cfg_int("recent_tool_limit", 12) :])

    def _remember_tool(self, umo: str, trace: ToolTrace) -> None:
        limit = max(1, self._cfg_int("recent_tool_limit", 12))
        queue = self._recent_tools_by_umo.setdefault(umo, deque(maxlen=limit))
        queue.append(trace)

    def _append_tool_to_current_state(self, event: AstrMessageEvent, trace: ToolTrace) -> None:
        state = self._last_state_by_umo.get(event.unified_msg_origin)
        if state is None:
            return
        state.tools.append(trace)
        self._append_dump_event(state, trace.phase, asdict(trace))
        self._remember_state(state)

    def _tool_name(self, tool: Any) -> str:
        return str(getattr(tool, "name", None) or getattr(tool, "__class__", type(tool)).__name__)

    def _tool_source(self, tool: Any) -> str:
        try:
            path = Path(inspect.getfile(tool.__class__)).resolve()
            return self._format_source(path, 0, self._tool_name(tool))
        except Exception:
            module = str(getattr(tool, "__module__", "") or "")
            return module or PREVIEW_FALLBACK

    def _tool_result_text(self, tool_result: Any) -> str:
        if tool_result is None:
            return ""
        if isinstance(tool_result, str):
            return tool_result
        content = getattr(tool_result, "content", None)
        if content is None and isinstance(tool_result, dict):
            content = tool_result.get("content")
        if content is not None:
            return self._preview_value(content, limit=2000)
        return str(tool_result)

    def _infer_mutation_source(self) -> str:
        for frame_info in inspect.stack()[2:18]:
            try:
                path = Path(frame_info.filename).resolve()
            except Exception:
                continue
            if path == self._plugin_file:
                continue
            if path.name == "inspect.py":
                continue
            return self._format_source(path, frame_info.lineno, frame_info.function)
        return "unknown"

    def _format_source(self, path: Path, line: int, function: str) -> str:
        parts = list(path.parts)
        plugin_part = ""
        for part in reversed(parts):
            if part.startswith("astrbot_plugin_"):
                plugin_part = part
                break
        suffix = f":{line}:{function}" if line > 0 else f":{function}"
        if plugin_part:
            return f"{plugin_part}/{path.name}{suffix}"

        normalized = path.as_posix().lower()
        marker = "/astrbot/core/"
        if marker in normalized:
            return f"astrbot-core/{path.name}{suffix}"
        return f"{path.name}{suffix}"

    def _log_bad_payload(self, state: AuditState, phase: PhaseRecord) -> None:
        sources = self._source_hint(state)
        logger.warning(
            "[%s] unsafe payload model=%s bad=%s action=%s source_hint=%s",
            PLUGIN_ID,
            state.provider_model or PREVIEW_FALLBACK,
            phase.bad_count,
            state.provider_action,
            sources,
        )

    def _source_hint(self, state: AuditState) -> str:
        mutation_sources = [
            item.source for item in state.mutations if item.introduced_bad_assistant
        ] or [item.source for item in state.mutations]
        if mutation_sources:
            top = Counter(mutation_sources).most_common(3)
            return "request hook mutation: " + ", ".join(f"{source} x{count}" for source, count in top)

        bad_phases = [phase.phase for phase in state.phases if phase.bad_count > 0]
        if bad_phases:
            first = bad_phases[0]
            if first in {"agent_begin", "request_early"}:
                return "bad assistant already existed before request hooks; check saved conversation history or previous agent run"
            if first == "provider_prepare":
                return "bad assistant appeared during provider serialization; check provider compatibility and recent tools"

        if state.tools:
            recent = ", ".join(f"{item.name}({item.phase})" for item in state.tools[-4:])
            return f"recent tools: {recent}"
        return "unknown"

    def _format_status(self, state: AuditState) -> str:
        latest_bad = next((phase for phase in reversed(state.phases) if phase.bad_count > 0), None)
        last_phase = state.phases[-1] if state.phases else None
        lines = [
            "EmptyAssistantGuard",
            f"version: {PLUGIN_VERSION}",
            f"status_model_filter: {', '.join(self._status_model_keywords()) or 'all'}",
            f"request_id: {state.request_id}",
            f"model: {state.provider_model or PREVIEW_FALLBACK}",
            f"last_phase: {(last_phase.phase if last_phase else PREVIEW_FALLBACK)}",
            f"bad_messages: {(latest_bad.bad_count if latest_bad else 0)}",
            f"provider_action: {state.provider_action}",
            (
                "patches: "
                f"runner=config:{self._cfg_bool('patch_agent_runner', True)},installed:{_RUNNER_PATCHED}; "
                f"openai_provider=config:{self._cfg_bool('patch_openai_provider', True)},installed:{_PATCHED}"
            ),
            f"source_hint: {self._source_hint(state)}",
        ]
        if latest_bad:
            for finding in latest_bad.findings[: self._cfg_int("status_finding_limit", 3)]:
                lines.append(
                    "finding: "
                    f"index={finding.index}, prev={finding.prev_role or PREVIEW_FALLBACK}, "
                    f"next={finding.next_role or PREVIEW_FALLBACK}, cause={finding.likely_cause}"
                )
        if state.tools:
            recent_tools = ", ".join(
                f"{tool.name}/{tool.phase}" for tool in state.tools[-self._cfg_int("status_tool_limit", 4) :]
            )
            lines.append(f"recent_tools: {recent_tools}")
        if state.mutations:
            top = Counter(item.source for item in state.mutations).most_common(3)
            lines.append("mutations: " + ", ".join(f"{source} x{count}" for source, count in top))
        if state.repairs:
            last_repair = state.repairs[-1]
            lines.append(
                f"last_repair: {last_repair.action}, {last_repair.before_messages}->{last_repair.after_messages}"
            )
        lines.append(f"dump: {state.dump_dir}")
        return "\n".join(lines)

    def _set_active(self) -> None:
        global _ACTIVE_PLUGIN
        _ACTIVE_PLUGIN = self

    def _clear_active(self) -> None:
        global _ACTIVE_PLUGIN
        if _ACTIVE_PLUGIN is self:
            _ACTIVE_PLUGIN = None

    def _apply_provider_patches(self) -> None:
        global _PATCHED
        if _PATCHED:
            return

        try:
            from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        except Exception as exc:
            logger.warning("[%s] failed to patch OpenAI provider: %s", PLUGIN_ID, exc)
            return

        _PATCH_CLASSES["ProviderOpenAIOfficial"] = ProviderOpenAIOfficial
        _PATCH_ORIGINALS["text_chat"] = ProviderOpenAIOfficial.text_chat
        _PATCH_ORIGINALS["text_chat_stream"] = ProviderOpenAIOfficial.text_chat_stream
        _PATCH_ORIGINALS["_prepare_chat_payload"] = ProviderOpenAIOfficial._prepare_chat_payload
        _PATCH_ORIGINALS["_sanitize_assistant_messages"] = (
            ProviderOpenAIOfficial._sanitize_assistant_messages
        )
        _PATCH_ORIGINALS["_handle_api_error"] = ProviderOpenAIOfficial._handle_api_error
        _PATCH_ORIGINALS["ProviderRequest.__setattr__"] = getattr(ProviderRequest, "__setattr__")
        _PATCH_CLASSES["ProviderRequest"] = ProviderRequest

        async def text_chat_wrapper(provider: Any, *args: Any, **kwargs: Any) -> Any:
            session_id = kwargs.get("session_id")
            if session_id is None and len(args) > 1:
                session_id = args[1]
            setattr(provider, f"_{PLUGIN_ID}_current_session_id", session_id or "")
            try:
                return await _PATCH_ORIGINALS["text_chat"](provider, *args, **kwargs)
            finally:
                setattr(provider, f"_{PLUGIN_ID}_current_session_id", "")

        async def text_chat_stream_wrapper(provider: Any, *args: Any, **kwargs: Any):
            session_id = kwargs.get("session_id")
            if session_id is None and len(args) > 1:
                session_id = args[1]
            setattr(provider, f"_{PLUGIN_ID}_current_session_id", session_id or "")
            try:
                async for chunk in _PATCH_ORIGINALS["text_chat_stream"](provider, *args, **kwargs):
                    yield chunk
            finally:
                setattr(provider, f"_{PLUGIN_ID}_current_session_id", "")

        async def prepare_chat_payload_wrapper(provider: Any, *args: Any, **kwargs: Any) -> tuple[Any, Any]:
            payloads, context_query = await _PATCH_ORIGINALS["_prepare_chat_payload"](
                provider,
                *args,
                **kwargs,
            )
            plugin = _ACTIVE_PLUGIN
            if plugin is not None:
                plugin.on_provider_payload_prepared(provider, payloads, context_query)
            return payloads, context_query

        def sanitize_assistant_messages_wrapper(payloads: dict[str, Any]) -> None:
            _PATCH_ORIGINALS["_sanitize_assistant_messages"](payloads)
            plugin = _ACTIVE_PLUGIN
            if plugin is not None:
                plugin.on_provider_payload_sanitize(payloads)

        async def handle_api_error_wrapper(
            provider: Any,
            error: Exception,
            payloads: dict,
            context_query: list,
            func_tool: Any,
            chosen_key: str,
            available_api_keys: list[str],
            retry_cnt: int,
            max_retries: int,
            image_fallback_used: bool = False,
        ) -> tuple[Any, ...]:
            plugin = _ACTIVE_PLUGIN
            if plugin is not None:
                retry_result = plugin.on_provider_api_error(
                    provider=provider,
                    error=error,
                    payloads=payloads,
                    context_query=context_query,
                    func_tool=func_tool,
                    chosen_key=chosen_key,
                    available_api_keys=available_api_keys,
                    image_fallback_used=image_fallback_used,
                )
                if retry_result is not None:
                    return retry_result

            return await _PATCH_ORIGINALS["_handle_api_error"](
                provider,
                error,
                payloads,
                context_query,
                func_tool,
                chosen_key,
                available_api_keys,
                retry_cnt,
                max_retries,
                image_fallback_used=image_fallback_used,
            )

        original_setattr = _PATCH_ORIGINALS["ProviderRequest.__setattr__"]

        def request_setattr_wrapper(req: ProviderRequest, name: str, value: Any) -> None:
            original_setattr(req, name, value)
            if name not in {"contexts", "extra_user_content_parts", "prompt", "system_prompt"}:
                return
            plugin = _ACTIVE_PLUGIN
            if plugin is not None:
                plugin.record_assignment(req, name, value)

        ProviderOpenAIOfficial.text_chat = text_chat_wrapper
        ProviderOpenAIOfficial.text_chat_stream = text_chat_stream_wrapper
        ProviderOpenAIOfficial._prepare_chat_payload = prepare_chat_payload_wrapper
        ProviderOpenAIOfficial._sanitize_assistant_messages = staticmethod(
            sanitize_assistant_messages_wrapper
        )
        ProviderOpenAIOfficial._handle_api_error = handle_api_error_wrapper
        ProviderRequest.__setattr__ = request_setattr_wrapper

        _PATCH_WRAPPERS["text_chat"] = text_chat_wrapper
        _PATCH_WRAPPERS["text_chat_stream"] = text_chat_stream_wrapper
        _PATCH_WRAPPERS["_prepare_chat_payload"] = prepare_chat_payload_wrapper
        _PATCH_WRAPPERS["_sanitize_assistant_messages"] = sanitize_assistant_messages_wrapper
        _PATCH_WRAPPERS["_handle_api_error"] = handle_api_error_wrapper
        _PATCH_WRAPPERS["ProviderRequest.__setattr__"] = request_setattr_wrapper
        _PATCHED = True
        logger.info("[%s] patched ProviderOpenAIOfficial", PLUGIN_ID)

    def _apply_runner_patches(self) -> None:
        global _RUNNER_PATCHED
        try:
            from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        except Exception as exc:
            logger.warning("[%s] failed to patch ToolLoopAgentRunner: %s", PLUGIN_ID, exc)
            return

        if "ToolLoopAgentRunner" not in _PATCH_CLASSES:
            _PATCH_CLASSES["ToolLoopAgentRunner"] = ToolLoopAgentRunner

        if "_runner_sanitize_contexts_for_provider" not in _PATCH_ORIGINALS:
            _PATCH_ORIGINALS["_runner_sanitize_contexts_for_provider"] = (
                ToolLoopAgentRunner._sanitize_contexts_for_provider
            )

            def sanitize_contexts_for_provider_wrapper(runner: Any, contexts: Any) -> Any:
                sanitized = _PATCH_ORIGINALS["_runner_sanitize_contexts_for_provider"](
                    runner,
                    contexts,
                )
                plugin = _ACTIVE_PLUGIN
                if plugin is None:
                    return sanitized
                return plugin.sanitize_runner_contexts(runner, sanitized)

            ToolLoopAgentRunner._sanitize_contexts_for_provider = sanitize_contexts_for_provider_wrapper
            _PATCH_WRAPPERS["_runner_sanitize_contexts_for_provider"] = (
                sanitize_contexts_for_provider_wrapper
            )

        if "_runner_complete_with_assistant_response" not in _PATCH_ORIGINALS:
            _PATCH_ORIGINALS["_runner_complete_with_assistant_response"] = (
                ToolLoopAgentRunner._complete_with_assistant_response
            )

            async def complete_with_assistant_response_wrapper(
                runner: Any,
                llm_resp: Any,
            ) -> None:
                messages = getattr(getattr(runner, "run_context", None), "messages", None)
                before_count = len(messages) if isinstance(messages, list) else 0
                await _PATCH_ORIGINALS["_runner_complete_with_assistant_response"](
                    runner,
                    llm_resp,
                )
                plugin = _ACTIVE_PLUGIN
                if plugin is not None:
                    await plugin.cleanup_runner_after_complete(runner, before_count)

            ToolLoopAgentRunner._complete_with_assistant_response = (
                complete_with_assistant_response_wrapper
            )
            _PATCH_WRAPPERS["_runner_complete_with_assistant_response"] = (
                complete_with_assistant_response_wrapper
            )

        _RUNNER_PATCHED = True
        logger.info("[%s] patched ToolLoopAgentRunner", PLUGIN_ID)

    def _restore_provider_patches(self) -> None:
        global _PATCHED, _RUNNER_PATCHED
        provider_cls = _PATCH_CLASSES.get("ProviderOpenAIOfficial")
        if provider_cls is not None:
            for name in (
                "text_chat",
                "text_chat_stream",
                "_prepare_chat_payload",
                "_sanitize_assistant_messages",
                "_handle_api_error",
            ):
                wrapper = _PATCH_WRAPPERS.get(name)
                original = _PATCH_ORIGINALS.get(name)
                if wrapper is not None and original is not None and getattr(provider_cls, name, None) is wrapper:
                    if name == "_sanitize_assistant_messages":
                        setattr(provider_cls, name, staticmethod(original))
                    else:
                        setattr(provider_cls, name, original)

        runner_cls = _PATCH_CLASSES.get("ToolLoopAgentRunner")
        if runner_cls is not None:
            runner_pairs = {
                "_sanitize_contexts_for_provider": "_runner_sanitize_contexts_for_provider",
                "_complete_with_assistant_response": "_runner_complete_with_assistant_response",
            }
            for attr_name, key in runner_pairs.items():
                wrapper = _PATCH_WRAPPERS.get(key)
                original = _PATCH_ORIGINALS.get(key)
                if wrapper is not None and original is not None and getattr(runner_cls, attr_name, None) is wrapper:
                    setattr(runner_cls, attr_name, original)

        request_cls = _PATCH_CLASSES.get("ProviderRequest")
        wrapper = _PATCH_WRAPPERS.get("ProviderRequest.__setattr__")
        original = _PATCH_ORIGINALS.get("ProviderRequest.__setattr__")
        if request_cls is not None and wrapper is not None and original is not None:
            if getattr(request_cls, "__setattr__", None) is wrapper:
                request_cls.__setattr__ = original

        _PATCHED = False
        _RUNNER_PATCHED = False

    def _safe_json(self, value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            return str(value)

    def _preview_text(self, value: Any, *, limit: int | None = None) -> str:
        if value is None:
            return PREVIEW_FALLBACK
        text = str(value)
        if not text:
            return PREVIEW_FALLBACK
        max_chars = limit or self._cfg_int("preview_chars", 160)
        if len(text) <= max_chars:
            return text
        head = max(16, max_chars // 2)
        tail = max(16, max_chars - head - 3)
        return f"{text[:head]}...{text[-tail:]}"

    def _preview_value(self, value: Any, *, limit: int | None = None) -> str:
        if value is None:
            return PREVIEW_FALLBACK
        if isinstance(value, dict):
            if "role" in value:
                return f"{value.get('role')}:{self._preview_value(value.get('content'), limit=limit)}"
            for key in ("text", "content", "name", "tool_call_id"):
                if key in value:
                    return self._preview_value(value.get(key), limit=limit)
            return self._preview_text(self._safe_json(value), limit=limit)
        if isinstance(value, list):
            if not value:
                return "[]"
            preview = ", ".join(self._preview_value(item, limit=limit) for item in value[:3])
            suffix = "..." if len(value) > 3 else ""
            return f"[{preview}{suffix}]"
        for attr in ("text", "content"):
            if hasattr(value, attr):
                return self._preview_value(getattr(value, attr), limit=limit)
        return self._preview_text(value, limit=limit)

    def _session_key(self, value: str) -> str:
        return str(value or "").strip()

    def _cfg(self, key: str, default: Any) -> Any:
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        return default

    def _cfg_int(self, key: str, default: int) -> int:
        value = self._cfg(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _cfg_str(self, key: str, default: str) -> str:
        value = self._cfg(key, default)
        if value is None:
            return default
        return str(value)
