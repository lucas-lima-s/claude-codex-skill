# Roadmap — `codex` skill

Items recorded as future evolution. Do not implement until the start
criterion is clear.

## Other open work

- **Robust `codex exec --json` event-stream parser.** The opt-in flag
  `CODEX_WRAPPER_USE_JSON_STREAM=1` already appends `--json` to the
  Codex command (see `scripts/invoke_codex_with_claude.py`), but the
  output is still consumed as a raw blob. A real streaming parser
  would benefit the heartbeat (richer phase signals) and enable
  partial-result rescue if the Codex run is killed mid-flight.
- **Review caching by packet fingerprint.** The canonical JSON output
  already exposes a `fingerprint` field, but there is no cache that
  uses it to short-circuit a re-review of an identical plan/diff
  already seen. Useful for `plan-review-iter` when the user re-runs
  the same plan tweak twice.
- **`repro` mode.** Given a `run_id`, replay the exact prompt + Codex
  output into a human-readable folder for debugging — useful when
  triaging an unexpected finding or a wrapper bug after the fact.
