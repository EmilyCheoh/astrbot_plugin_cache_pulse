# Changelog

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
