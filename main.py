"""Anthropic Cache Pulse — keep prompt cache warm between messages.

When Felis Abyssalis pauses between messages, Anthropic's 5-minute cache
TTL can expire and force a full re-write on the next turn.  This plugin
silently pings the Anthropic API with max_tokens=0 to refresh the cache
without generating any text, sending any QQ message, or touching the
conversation history.

Requires a small patch to:
  - astr_agent_context.py        (extra: dict[str, Any])
  - tool_loop_agent_runner.py    (expose func_tool/model/session_id in extra)
  - anthropic_source.py          (cache_pulse=True support in _query/text_chat)
"""

import asyncio
import copy
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


PLUGIN_NAME = "astrbot_plugin_cache_pulse"


@register(
    PLUGIN_NAME,
    "Felis Abyssalis & Noir & Abyss AI",
    "Keep Anthropic prompt cache warm for active sessions.",
    "0.1.0",
)
class CachePulsePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.sessions: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = asyncio.create_task(self._pulse_loop())

    # ── config helpers ──────────────────────────────────────────────

    def _enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def _interval(self) -> float:
        return float(self.config.get("interval_seconds", 270))

    def _max_tries(self) -> int:
        return int(self.config.get("max_tries", 5))

    def _check_interval(self) -> float:
        return float(self.config.get("check_interval_seconds", 5))

    def _debug(self) -> bool:
        return bool(self.config.get("debug_log", True))

    # ── provider detection ──────────────────────────────────────────

    def _is_anthropic_provider(self, provider_id: str) -> bool:
        """Return True if the provider looks like an Anthropic adapter."""
        prov = self.context.get_provider_by_id(provider_id)
        if not prov:
            return False
        cfg = getattr(prov, "provider_config", None)
        if not isinstance(cfg, dict):
            return False
        # Check adapter type, provider name, and model
        adapter = str(cfg.get("type", "") or "").lower()
        name = str(cfg.get("id", "") or provider_id).lower()
        model = str(cfg.get("model", "") or "").lower()
        return (
            "anthropic" in adapter
            or "anthropic" in name
            or "claude" in model
        )

    # ── event listeners ─────────────────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """Record real user activity and reset pulse counter."""
        if not self._enabled():
            return
        umo = event.unified_msg_origin
        now = time.monotonic()
        state = self.sessions.get(umo)
        if state:
            state["last_user_at"] = now
            state["tries"] = 0
            if self._debug():
                logger.debug("[🔄 Cache Pulse] user activity reset umo=%s", umo)
        else:
            # Skeleton entry — snapshot will be filled by on_agent_done
            self.sessions[umo] = {
                "last_user_at": now,
                "last_llm_done_at": 0.0,
                "last_pulse_at": 0.0,
                "tries": 0,
                "inflight": False,
            }

    @filter.on_agent_done()
    async def on_agent_done(self, event: AstrMessageEvent, run_context, response):
        """Snapshot the completed LLM context for future pulse replay."""
        if not self._enabled():
            return

        umo = event.unified_msg_origin

        # Retrieve provider ID for this session
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo)
        except Exception:
            if self._debug():
                logger.warning("[🔄 Cache Pulse] could not resolve provider for umo=%s", umo)
            return

        if not self._is_anthropic_provider(provider_id):
            if self._debug():
                logger.debug("[🔄 Cache Pulse] skip non-Anthropic provider=%s", provider_id)
            return

        # Read exposed metadata from runner patch
        extra = getattr(getattr(run_context, "context", None), "extra", None) or {}
        tools = extra.get("cache_pulse_func_tool")
        model = extra.get("cache_pulse_model")
        session_id = extra.get("cache_pulse_session_id")

        # Deep-copy messages so later mutations don't affect our snapshot
        try:
            messages = copy.deepcopy(run_context.messages)
        except Exception:
            if self._debug():
                logger.warning("[🔄 Cache Pulse] failed to deepcopy messages for umo=%s", umo)
            return

        now = time.monotonic()
        self.sessions[umo] = {
            "provider_id": provider_id,
            "messages": messages,
            "tools": tools,
            "model": model,
            "session_id": session_id,
            "last_user_at": now,
            "last_llm_done_at": now,
            "last_pulse_at": 0.0,
            "tries": 0,
            "inflight": False,
        }

        if self._debug():
            logger.info(
                "[🔄 Cache Pulse] snapshot saved, %d messages captured",
                len(messages),
            )

    # ── background pulse loop ───────────────────────────────────────

    async def _pulse_loop(self):
        """Periodically check sessions and fire keepalive pulses."""
        while True:
            await asyncio.sleep(self._check_interval())
            if not self._enabled():
                continue

            now = time.monotonic()
            for umo, state in list(self.sessions.items()):
                if state.get("inflight"):
                    continue
                # Need a full snapshot to pulse
                provider_id = state.get("provider_id")
                messages = state.get("messages")
                if not provider_id or not messages:
                    continue
                # Respect max tries
                if state.get("tries", 0) >= self._max_tries():
                    if not state.get("_max_logged"):
                        logger.info(
                            "[🔄 Cache Pulse] max tries (%d) reached, pausing until next message",
                            self._max_tries(),
                        )
                        state["_max_logged"] = True
                    continue
                # Check if enough idle time has passed since last LLM activity
                last_activity = max(
                    float(state.get("last_llm_done_at") or 0),
                    float(state.get("last_pulse_at") or 0),
                )
                if last_activity <= 0:
                    continue
                idle = now - last_activity
                if idle < self._interval():
                    continue

                # Fire pulse
                state["inflight"] = True
                try:
                    await self._do_pulse(umo, state)
                    state["tries"] = state.get("tries", 0) + 1
                    state["last_pulse_at"] = time.monotonic()
                except Exception as exc:
                    logger.warning(
                        "[🔄 Cache Pulse] pulse failed umo=%s err=%s",
                        umo, exc, exc_info=True,
                    )
                    state["tries"] = state.get("tries", 0) + 1
                    state["last_pulse_at"] = time.monotonic()
                finally:
                    state["inflight"] = False

    async def _do_pulse(self, umo: str, state: dict[str, Any]):
        """Send a single cache-warming pulse via llm_generate."""
        provider_id = state["provider_id"]

        # Append a minimal user message so the conversation ends with
        # a user turn — preserves the full cached prefix (including the
        # last assistant reply) while satisfying API format requirements.
        msgs = list(state["messages"])
        msgs.append({"role": "user", "content":
            "[System: cache keepalive pulse — not a real message. "
            "Reply with exactly: 1]"
        })

        resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            contexts=msgs,
            tools=state.get("tools"),
            model=state.get("model"),
            session_id=state.get("session_id"),
            cache_pulse=True,
        )

        if self._debug():
            usage = getattr(resp, "usage", None)
            logger.info(
                "[🔄 Cache Pulse] pulse ok (try %d) usage = %s",
                state.get("tries", 0) + 1, usage,
            )

    # ── cleanup ─────────────────────────────────────────────────────

    async def terminate(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
