# Anthropic Cache Pulse

Keep Anthropic's 5-minute prompt cache alive between messages.

## Problem

Anthropic's prompt caching has a 5-minute TTL. When Felis Abyssalis pauses between messages (typing slowly, thinking, getting distracted by a bat), the cache expires and the next message pays the full cache-write cost again.

## Solution

After the LLM finishes responding, the plugin starts a timer. If no new user message arrives within the configured interval (default: 4m30s), it sends a silent `max_tokens=0` request to the Anthropic API with the same `tools + system + messages` prefix. This refreshes the cache TTL without generating any text, sending any QQ message, or modifying conversation history.

The plugin will pulse up to `max_tries` times (default: 5) before going quiet. Any real user message resets the counter.

## How It Works

1. `on_agent_done` — snapshots the completed LLM context (messages, tools, model, session_id)
2. Background loop checks every `check_interval_seconds` (default: 5s)
3. When idle time exceeds `interval_seconds`, fires a pulse via `Context.llm_generate(cache_pulse=True)`
4. The patched `anthropic_source.py` handles `cache_pulse=True` by setting `max_tokens=0`, stripping thinking config, and accepting empty content as success

## Configuration

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enable/disable the plugin |
| `interval_seconds` | int | `270` | Seconds after last LLM response before pulsing |
| `max_tries` | int | `5` | Max consecutive pulses without user activity |
| `check_interval_seconds` | int | `5` | Background loop check frequency |
| `debug_log` | bool | `true` | Log pulse details |

## Required Core Patches

This plugin requires three small patches to AstrBot core:

1. **`astr_agent_context.py`** — `extra: dict[str, str]` → `dict[str, Any]`
2. **`tool_loop_agent_runner.py`** — expose `func_tool`, `model`, `session_id` in `run_context.context.extra` at end of `reset()`
3. **`anthropic_source.py`** — `_query()` and `text_chat()` support `cache_pulse=True` (sets `max_tokens=0`, strips thinking, accepts empty content)

## Notes

- `max_tokens=0` is an official Anthropic feature designed for cache warming
- Cache prefix is content-based (`tools → system → messages`), not parameter-based — so pulse without thinking still refreshes the same cache that thinking-enabled requests use
- Pulse never triggers tool calls, never writes to conversation history, never sends QQ messages
