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
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
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

    def _notify_on_expire(self) -> bool:
        return bool(self.config.get("notify_on_expire", True))

    def _preset_count(self) -> int:
        return int(self.config.get("preset_message_count", 2))

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
        return "anthropic" in adapter or "anthropic" in name

    def _is_claude_model(self, model: Any) -> bool:
        """Return True only if the model string looks like a Claude model."""
        model_name = str(model or "").lower()
        return "claude" in model_name

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

        # Read exposed metadata from runner patch
        extra = getattr(getattr(run_context, "context", None), "extra", None) or {}
        tools = extra.get("cache_pulse_func_tool")
        model = extra.get("cache_pulse_model")
        session_id = extra.get("cache_pulse_session_id")

        # Only pulse for Claude on an Anthropic-format provider.
        #
        # 1. Provider must be Anthropic-format — otherwise skip.
        # 2. If the runner patch exposed a model name, verify it's Claude.
        #    If model is None (patch didn't expose it), allow it through —
        #    DS sessions always expose their model in extra, so a missing
        #    model on an Anthropic provider reliably indicates Claude on QQ.
        if not self._is_anthropic_provider(provider_id):
            self.sessions.pop(umo, None)
            return

        if model is not None and not self._is_claude_model(model):
            self.sessions.pop(umo, None)
            return

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
                        # Nudge Felis Abyssalis before the cache goes cold
                        if self._notify_on_expire():
                            try:
                                chain = MessageChain().message(
                                    "‼️ Cache is expiring soon. 💸"
                                )
                                await self.context.send_message(umo, chain)
                            except Exception as exc:
                                logger.warning(
                                    "[🔄 Cache Pulse] notify failed, err=%s",
                                    exc,
                                )
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

        # Trim to the second-to-last *user* message to align closely
        # with the real request's cache breakpoint (which the patched
        # anthropic_source places on the second-to-last user message).
        # Then append two pairs of fake (asst "OK", user KEEPALIVE):
        #
        #   [...stable prefix..., second_to_last_user]
        #     + fake_asst "OK"                          ┐ ~22 tokens
        #     + fake_user₁ KEEPALIVE  ← breakpoint      ┘ cache creation
        #     + fake_asst "OK"        ┐
        #     + fake_user₂ KEEPALIVE  ┘ uncached input ~22 tokens
        #
        # The first (KEEPALIVE → OK) pair provides an in-context
        # example so the model responds with just "OK" (~4 tokens).
        # Cache creation waste: only ~22 tokens per batch (the gap
        # between the real breakpoint and the pulse's fake_user₁).
        msgs = list(state["messages"])

        user_indices = [
            i for i, m in enumerate(msgs)
            if getattr(m, "role", None) == "user"
        ]
        if len(user_indices) >= 2:
            msgs = msgs[: user_indices[-2] + 1]
        else:
            preset_n = self._preset_count()
            msgs = msgs[:preset_n] if preset_n > 0 else []

        KEEPALIVE = (
            "[System: This is an automatic cache keepalive message. "
            "Reply with 'OK' verbatim.]"
        )
        msgs.append({"role": "assistant", "content":
            "[Entering cache keepalive mode.]"})
        msgs.append({"role": "user", "content": KEEPALIVE})
        msgs.append({"role": "assistant", "content": "OK"})
        msgs.append({"role": "user", "content": KEEPALIVE})

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
