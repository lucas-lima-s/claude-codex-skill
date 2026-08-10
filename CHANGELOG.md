# Changelog

All notable changes to the `claude-codex-skill` are documented here. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- **A failed run could be reported as a clean review.** Four independent
  defects composed into one silent failure: `plan-review` over a 38.5 KB plan
  produced zero findings across three attempts, and none of them surfaced an
  error. All four are closed:
  - Every entry point now mirrors `status` in its exit code (`0` ok, `2`
    error, `3` needs_input) instead of always returning `0` — the wrapper,
    `codex_bg.py`, `codex_dialogue.py` and `codex_batch.py`, the last of which
    also treats `partial` as non-zero because some item was not done. The JSON
    still goes to stdout first, so parsing callers are unaffected. Shared
    contract in `codex_config.exit_code_for_status`.
  - `codex_bg.py start` validates the mode against `wrapper.modes` before
    spawning anything. `plan-review-iter` and `batch-*` are composed flows,
    not wrapper modes, and are now refused with `reason=invalid_mode` instead
    of dying inside the wrapper's argparse where nothing observed it.
  - `codex_bg.py start` probes the spawned process for
    `background.startup_probe_seconds` (3s) and returns
    `reason=died_on_startup` with the captured `stderr_tail` rather than a
    `run_id` for a process that is already gone.
  - `codex_dialogue.py` no longer emits envelope `status: ok` over a turn
    whose wrapper failed, and `_stop_signal` no longer counts a failed turn
    as clean — two consecutive timeouts used to be read as convergence.

### Added

- **Wall-clock ceiling scales with the target.** `wrapper.mode_timeouts` is now
  a floor; the wrapper raises its own ceiling with the byte size of the prompt
  it assembled, up to `wrapper.scale_ceiling_multiplier` (4x). New keys:
  `wrapper.scale_free_bytes`, `wrapper.scale_bytes_per_unit`,
  `wrapper.scale_ceiling_multiplier`. `codex_config.subprocess_timeout_for`
  budgets against that ceiling, so a supervisor never kills a wrapper that is
  still inside its own budget.
- **`plan-review-split`: large plans reviewed per phase, in parallel.**
  `split_plan_by_phase.py` slices a phased plan into one file per phase plus a
  **coherence slice** carrying the whole plan under a structure-only
  instruction — ordering, cross-phase dependencies, contradictions, boundary
  gaps — which is what plain slicing would lose. A plan with no phase headings
  returns `not_splittable` and falls back to the monolithic review.
- **`batch-plan-review` sub-mode** in `codex_batch.py`, running each slice as a
  real `plan-review` (not `ask`, which would drop the checklist and the
  reasoning effort). Adds an `aggregate` block: findings deduplicated by
  `(title, location)` keeping the highest severity and recording every source
  slice, `coverage` unioned, `block_recommended` as `any()`.
- `analyze_plan_complexity.py` now returns a `metrics` block with the raw
  counts (`size_bytes`, `distinct_files`, `phases`, `sensitive_hits`,
  `cross_module_hits`) and a `suggest_split` flag, driven by
  `complexity.split_phases_threshold` (4) and
  `complexity.split_size_threshold_bytes` (16 KB).
- `wrapper.modes` in the config as the single canonical mode list, read by both
  the wrapper and `codex_bg.py`.

- **Quota exhaustion is a classified failure**, not a generic non-zero exit.
  `error_class: quota_exhausted` with the reset date lifted out of the Codex
  refusal and into the user-facing summary. Previously this reached the user as
  "Codex exited with non-zero status (1)", with the actionable part
  ("try again at Aug 15th") left in the stderr nobody reads. A `batch-*` round
  now stops dispatching once any item hits the wall: every item draws on the
  same quota, so the remaining ones can only reproduce the same failure more
  slowly. New `fake_codex.py` behaviour `quota_exhausted` reproduces the real
  refusal string.
- **Interactive budget for `plan-review-split`.** The splitter routes phase
  slices as `interactive` under `split.phase_reasoning_effort`, and the
  coherence slice as `background` under the higher
  `split.coherence_reasoning_effort`. The coherence slice reads the whole plan
  and is always the slowest, so keeping it on the critical path is what pushes
  a round past `split.interactive_budget_seconds` (300s). It now goes to
  `bg-start` while the phase findings are presented immediately.
- The splitter writes a ready-to-run `batch_input.json` containing only the
  interactive slices, and `codex_batch.py` forwards a per-task
  `reasoning_effort` to the wrapper.
- `split.effort_calibrated` is `false`: the per-effort durations have not been
  measured against the real Codex yet, so the routing is sound but the specific
  effort values are an estimate.

### Changed

- `split.max_parallel` defaults to 3, down from the batcher's 4. A 7-way round
  over a 47 KB plan was observed degrading badly (656s, 828s, 961s for slices
  of equivalent size) and exhausting the account quota in one go.
- `codex_bg.py` honours `CODEX_WRAPPER_CACHE_DIR` for its `bg_runs/`
  directories, mirroring the wrapper. `tests/test_codex_bg.py` now points at a
  temp sandbox: its runs against `fake_codex.py` were being written into the
  real `cache/runs.jsonl`, indistinguishable from production rows.

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

### Fixed — from the first exhaustive self-review

The new review pass was run against this very change and returned 34 findings;
these are the ones that were real defects.

- `status` and `questions` were missing from the output schema, so the
  `needs_input` contract the prompt asks for was impossible to satisfy under
  strict structured output. The mocked suite passed only because the fake
  ignored `--output-schema`.
- The service-tier fallback ran a second full attempt outside the shared
  deadline, and applied to `delegate` — a refused tier could re-run a task
  that had already created, edited or deleted files. It now shares the
  deadline and never repeats a run that executed anything.
- Tier refusal was detected by substring, so any failure that merely
  mentioned the setting triggered a retry. It now requires an explicit
  rejection and also reads the error events off the stream.
- `_kill_process_tree` was a no-op outside Windows: children of a killed
  Codex kept running on Linux and macOS. The child now leads its own session
  and the whole group is signalled.
- One global 180s idle limit was applied to every mode, including `delegate`,
  which legitimately runs long silent commands. Idle limits are per mode.
- `subprocess_timeout_for` ignored `CODEX_WRAPPER_TIMEOUT_SECONDS`, so raising
  the wrapper's timeout made the sub-runners kill it earlier.
- `test_telemetry_rotation` wrote 5 MB into the real `cache/runs.jsonl` and
  rotated the history away. The whole suite now writes to a scratch cache via
  `CODEX_WRAPPER_CACHE_DIR`.
- `coverage` was an unchecked self-declaration; it is now reconciled against
  the checklist the prompt actually asked for, with `coverage_mismatch` when
  the declared counts disagree with the findings.
- Telemetry recorded neither the resolved model, effort and service tier nor
  how a run ended, so an `auto`-selected model could change silently — the
  exact problem the change set out to fix.
- `SETUP.md` now declares the minimum Codex CLI version, and `setup.py` warns
  when the installed one is older.

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
