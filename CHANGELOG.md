# Changelog

## 0.2.0 — 2026-10-11

**Fix: empty output pulses and API errors now trigger immediate pause**

Empty output pulses (`output=0`) were silently logged as successful,
resetting the TTL timer while the cache had already gone cold — causing
a guaranteed full cache rebuild on the next real request. API errors
(403, 429, etc.) triggered retries every 5 seconds until `max_tries`,
potentially extending rate-limit bans.

- Added `EmptyPulseOutputError` — raised when a pulse returns
  `usage.output <= 0` or `usage` is missing.
- Added `paused_due_error` session field — pauses all further pulses
  for the affected session on first failure.
- Added `_pause_after_failure()` helper — sets pause flag and sends a
  one-time QQ notification (empty output vs. API error).
- Moved "pulse ok" log to after `usage.output` validation — no longer
  prints on zero-output completions.
- Error branches no longer increment `tries` or update
  `last_cache_write_at` — no silent timer resets on failure.
- `on_agent_done` clears `paused_due_error` on next successful real
  response, automatically resuming the pulse cycle.

## 0.1.1 — 2026-08-07

**Fix: cache TTL timing drift caused by response generation time**

Anthropic's cache TTL (300s) counts from prefill completion, not from
response end. For high-reasoning Opus responses (60-90s generation),
measuring idle time from response end silently consumed the TTL budget,
causing pulses to arrive 14-42s too late.

- Added `last_cache_write_at` field — tracks estimated cache write time
  (user message arrival for real turns, request dispatch for pulses).
- Pulse loop now measures elapsed time from cache write, not response end.
- Removed dead variables `last_llm_done_at` and `last_pulse_at`.

## 0.1.0 — 2026-07-24

Initial release.
