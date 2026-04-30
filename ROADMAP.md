# Roadmap — `codex` skill

Items recorded as future evolution. Do not implement until the start
criterion is clear.

## 1. Background agents (non-blocking) — **IMPLEMENTED (Phase 1)**

Implemented in `scripts/codex_bg.py` with 5 subcommands
(`start | status | output | cancel | list`). State persisted in
`cache/bg_runs/<run_id>/` (meta.json + output.json + stderr.log +
cancelled.flag). Default limit of 5 concurrent runs, configurable via
`--max-concurrent` or `CODEX_BG_MAX_CONCURRENT`. Automatic cleanup of
terminated runs older than 7 days (mtime). Heartbeat redirected to the
run's own `stderr.log` (nothing flows to the Claude session's stderr).
See `SKILL.md` (section "Background modes").

Known caveat: `bg-start delegate` still allows launching `delegate` in
the background without the synchronous `delegate`'s 5-field protocol.
Risk consciously accepted by the author; revisit when there is a safe
asynchronous confirmation flow.

## 2. Multi-turn Claude ↔ Codex dialogue — **IMPLEMENTED (Phase 2)**

Implemented in `scripts/codex_dialogue.py` (start | next-turn | status |
finish | abort) + `scripts/analyze_plan_complexity.py` (auto-suggestion
heuristic). Default 3 turns, configurable via `--max-turns N` or
`CODEX_DIALOGUE_MAX_TURNS` (range 1-20). State in
`$TEMP/codex_dialogue_<id>/`. Stop criteria: convergence (2 consecutive
clean turns), turn limit, divergence (same `high` finding in 2 turns),
manual abort. `finish` consolidates `final_plan.md` + `dialogue_log.md`.

The auto-suggestion is triggered before any `plan-review`: Claude runs
`analyze_plan_complexity.py`; if the score is ≥ 3 (signals: size >4KB,
>5 mentioned files, >2 phases, sensitive keywords, cross-module), it
shows a single suggestion line and asks before proceeding. See
`SKILL.md` (modes `plan-review` and `plan-review-iter`).

Per-turn transparency rule kept: every turn presents findings 1-by-1
translated, opens `AskUserQuestion` to approve/reject each, and only
then produces the revised plan. High severity changes the tone of the
final question, never the order in which findings are disclosed.

## 3. Public-repo polish

The repo is public at
https://github.com/lucas-lima-s/claude-codex-skill, but only with the
minimum (MIT LICENSE, README, `.gitignore`). The items below take the
repo from "personal code dropped on GitHub" to "skill installable by
another user without having to ask anything".

### Current state

- ✅ LICENSE (MIT), README.md (English), `.gitignore`, public repo.
- ✅ README/SETUP in English.
- ❌ CI, CHANGELOG, CONTRIBUTING.
- ❌ Issue/PR templates, GitHub topics, English repo description.
- ❌ `.gitattributes` to normalise line endings.
- ❌ Audit of hardcoded paths and stray pt-BR/English mixing outside
  the points authorised by `CLAUDE.md`.

### Tooling and quality

- **GitHub Actions CI:** lint (ruff/black/mypy where applicable) + the
  mocked suite (`tests/test_codex_skill.py`) cross-platform
  (`ubuntu-latest` and `windows-latest`) on push and PR. **Exclude**
  `test_codex_live.py` from CI (it spends tokens against the real
  Codex) or use `FAKE_CODEX_BEHAVIOR` to mock it.
- **Issue templates + PR template:** basic versions
  (`.github/ISSUE_TEMPLATE/bug_report.md`, `feature_request.md`,
  `.github/PULL_REQUEST_TEMPLATE.md`).
- **`.gitattributes`** with `* text=auto eol=lf` (or similar) to
  prevent CRLF/LF confusion between Windows and Linux contributors.

### Documentation and identity

- **CHANGELOG.md (retroactive)** covering the previous phases
  (Phase 0–3 of skill development).
- **CONTRIBUTING.md** with local setup, how to run mocked tests, how
  to add a new mode, commit policy.
- **GitHub topics:** `claude-code`, `codex`, `skill`, `cli`,
  `automation`, `python`.
- **Repo description (one-liner, English):**
  *"Claude Code skill that delegates plan reviews, verifications, and
  tasks to OpenAI Codex CLI."*

### Acceptance criteria

- Another user (not Lucas) can clone, configure `$SKILLS_PYTHON`, and
  run the test suite in <10 min following only the docs.
- Green CI on both platforms for the mocked suite.
- No paths hardcoded to the personal machine.
- No pt-BR string outside the points where `CLAUDE.md` authorises it
  (skill description, pt-BR trigger phrases in `SKILL.md`, intentional
  user-facing messages keyed by locale).

### Suggested order

1. Audit hardcoded paths and pt-BR/English strings (blind Grep across
   the repo).
2. `.gitattributes` (before any other file change).
3. CHANGELOG.md (retroactive Phase 0–3).
4. CONTRIBUTING.md.
5. GitHub Actions workflow (lint + mocked suite).
6. Issue templates + PR template.
7. GitHub topics + repo description (manual GitHub action).

### Open decisions

- Add a `README.pt-BR.md` as a secondary translation, or stick with
  English-only?
- CI now or only after items 1–2 of this roadmap stabilise?
- Accept external contributions via PR or keep the repo "read-only for
  the public, write-only for the author"?

## 4. (Open) Other items

- `codex exec --json` event streaming with a robust parser (today the
  flag is opt-in but the output is treated as raw — would benefit
  heartbeat and partial parsing).
- Review caching by packet fingerprint — avoids re-reviewing an
  identical plan already seen.
- `repro` mode that, given a `run_id`, replays prompt + output into a
  human-readable folder for debugging.
