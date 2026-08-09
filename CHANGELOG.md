# Changelog

All notable changes to the `claude-codex-skill` are documented here. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Explicit model selection: `wrapper.model` (default `auto`) resolves the
  strongest model the Codex CLI advertises and passes it as `codex exec -m`,
  instead of inheriting whatever `~/.codex/config.toml` happens to hold.
  Overridable per mode (`wrapper.mode_model`) or per run (`CODEX_WRAPPER_MODEL`).
- Fast service tier (`service_tier=priority`) on every mode, with
  `CODEX_WRAPPER_SERVICE_TIER=default` to opt out and a one-shot retry without
  the flag if an account is refused the tier.
- Idle guard: runs are supervised through the `codex exec --json` event stream
  and killed after `wrapper.idle_timeout_seconds` (default 180) of complete
  silence, which is what makes the raised wall-clock ceilings safe.
- Heartbeat now reports liveness (`idle=`, `events=`, `last=`) instead of only
  elapsed time and output size.
- `coverage` in the review schema: `plan-review` and `verify` sweep a fixed
  checklist of categories and report the finding count of each, including the
  empty ones.
- `codex_config.subprocess_timeout_for(mode)`: single source for how long a
  caller should wait for the wrapper.
- `CODEX_WRAPPER_IDLE_TIMEOUT_SECONDS` and
  `CODEX_WRAPPER_HEARTBEAT_INTERVAL_SECONDS`.
- `fake_codex.py` behaviours `stream_slow` and `idle_stall`, plus real JSONL
  emission under `--json`.

### Changed

- Reasoning effort per mode: `plan-review`, `insight` and `delegate` to `max`,
  `verify` to `high`. `max` and `ultra` are now valid `--reasoning-effort`
  values (they were rejected despite the model supporting them).
- Wall-clock ceilings raised: `plan-review` 300s → 900s, `verify` 180s → 600s,
  `ask` 120s → 300s, `insight` 420s → 1200s, `delegate` 300s → 900s. Telemetry
  showed `plan-review` runs dying exactly at the old 300s ceiling.
- Both attempts of a run now share one deadline (`total_deadline_multiplier`),
  so a retry can no longer silently double the wall-clock cost.
- `codex exec --json` is on by default; `CODEX_WRAPPER_USE_JSON_STREAM=0` opts
  out (it used to be `=1` to opt in).
- Review prompts require exhaustiveness explicitly and forbid self-imposed
  caps, ranking limits and merging distinct issues into one finding.
- `codex_dialogue.py` and `codex_batch.py` derive their timeouts from the
  wrapper's budget instead of hardcoding 600s and 900s.

### Fixed

- `codex_batch.py` ignored its own `ITEM_SAFETY_TIMEOUT` and
  `MAX_PARALLEL_CEILING` constants, using literals instead.
- With the event stream on, an empty output file fell back to the whole JSONL
  transcript; it now falls back to the last agent message.

## [0.4.0] — 2026-04-30

### Phase 3 — Background runner + iterative review + complexity hint

- `scripts/codex_bg.py`: detached background runner for the wrapper. Default
  cap of 5 concurrent jobs (override via `CODEX_BG_MAX_CONCURRENT`); supports
  `start`, `status`, `logs`, `cancel`.
- `plan-review-iter` multi-turn dialogue mode (`scripts/codex_dialogue.py`):
  iterative back-and-forth between Claude and Codex on a single plan, with
  per-turn artefacts (`final_plan.md`, `dialogue_log.md`) and configurable
  `--max-turns`.
- `--accepted-by-user` guard rail on `codex_dialogue.py start`: refuses to
  initialise an iterative dialogue without explicit user acceptance, to
  avoid surprise Codex review charges.
- `scripts/analyze_plan_complexity.py`: heuristic that scores a plan and
  emits `suggest_iterative=true` when the plan is large, cross-module, or
  hits sensitive keywords. The skill uses this to one-time-offer
  `plan-review-iter` for non-trivial plans.
- `--reasoning-effort` override on the wrapper: per-invocation override of
  `model_reasoning_effort` (low / medium / high / xhigh). Invalid values
  warn on stderr and fall back to the per-mode default.

### Phase 2 — Setup wizard + locale-aware Codex prompt

- `scripts/setup.py`: interactive first-run wizard. Picks locale, surfaces
  Python and Codex CLI presence, and points to `config.local.json` for
  credential propagation overrides.
- `prompt.language_directive` propagated to Codex: when `CODEX_LOCALE` is
  set, all free-text fields in Codex JSON output (summary, findings,
  questions) are required to be in that locale.
- All user-facing markdown (`README.md`, `SETUP.md`, `ROADMAP.md`,
  `SKILL.md`) and the test suite migrated to English, except for
  documented pt-BR trigger phrases inside `SKILL.md`.

### Phase 1 — Centralised config + i18n

- `config.default.json` (committed, source of truth) +
  `config.local.json` (gitignored, per-user override) with a deep-merge
  resolver in `scripts/codex_config.py`.
- Locale-aware `t()` lookup with pt-BR / en-US bundles. `CODEX_LOCALE`
  env var or `settings.locale` chooses the active bundle; missing keys
  fall back to the literal key.
- Hardcoded pt-BR strings across the scripts replaced with `t()` lookups
  so the locale switch is uniform.
- `_doc` field in `config.default.json` translated to English so external
  contributors can read the schema commentary.

### Phase 0 — Initial release

- Seven Codex modes via `scripts/invoke_codex_with_claude.py`:
  `plan-review`, `verify`, `ask`, `insight`, `delegate`, `batch-ask`,
  `batch-delegate`. Each mode has a curated prompt, a per-mode timeout,
  and JSON-shaped output normalised by `scripts/normalize_codex_result.py`.
- Self-contained mocked test suite (`tests/test_codex_skill.py`) backed
  by `tests/fake_codex.py`. Reproduces every documented use case offline,
  no third-party deps. Behaviours selectable via `FAKE_CODEX_BEHAVIOR`
  (`success`, `invalid_json`, `timeout`, `nonzero`, `partial`,
  `noisy_stderr`, `delay_short`, `delay_long`, `needs_input`,
  `delegate_ok`).
- Telemetry: per-run JSONL in `cache/runs.jsonl` with rotation at 5 MB
  (rotates to `runs.jsonl.1`).
- PowerShell forwarding stub `invoke_codex_with_claude.ps1` for Windows
  callers that prefer pwsh.

[Unreleased]: https://github.com/lucas-lima-s/claude-codex-skill/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/lucas-lima-s/claude-codex-skill/releases/tag/v0.4.0
