# Changelog

All notable changes to the `claude-codex-skill` are documented here. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
