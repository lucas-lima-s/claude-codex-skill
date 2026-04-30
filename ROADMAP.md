# Roadmap — `codex` skill

Items recorded as future evolution. Do not implement until the start
criterion is clear.

## Public-repo polish

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
