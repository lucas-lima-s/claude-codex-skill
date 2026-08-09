# Setup — `codex` skill

Claude Code → Codex CLI bridge. The single entry point for automation.

Supported on Windows, Linux, and macOS via Python 3.10+.

## Requirements

- Codex CLI **0.146.0 or newer**, on `PATH` (`codex --version` must
  respond). The wrapper's defaults depend on `codex exec --json` for the
  liveness stream, `-c service_tier=`, `-m`, and the `max` / `ultra`
  reasoning levels; older CLIs reject them and every run fails. Point
  `wrapper.model` at an explicit slug and set
  `CODEX_WRAPPER_USE_JSON_STREAM=0` if you must run an older one, and
  expect the idle guard to be inactive.
- Python available via `$SKILLS_PYTHON` (preferred),
  `$CLAUDE_AUTOMATION_PYTHON`, or simply `python` / `python3` on `PATH`.
- (Optional) Credentials for the Codex subprocess in
  `~/.claude/credentials.env` (global) or `.env` at the skill root
  (per-skill override), `KEY=value` format. Only the keys listed in
  `settings.credentials.propagate` (in `config.local.json`) are
  injected into the Codex subprocess env from those files; values never
  appear in logs. **Important**: variables already present in the
  parent process environment (user's shell, Claude Code) are inherited
  as in any POSIX subprocess, regardless of `propagate`. For full
  isolation, invoke with `env -i` or its equivalent.

## First run

Run the interactive wizard:

    python scripts/setup.py

It asks for the interface locale (`en-US` or `pt-BR`), validates the
environment (Codex CLI, Python), and lists the credentials that will
be propagated to the Codex subprocess (without exposing values). The
preference is persisted in `config.local.json` (gitignored). All wizard
output goes to **stderr** — stdout stays reserved for the wrapper's
canonical JSON, so the auto-trigger on first interactive use does not
break parsers.

To change the locale later, re-run the same command, or use the
non-interactive helper:

    python scripts/set_locale.py --locale en-US

**BREAKING CHANGE compared to the previous version**: until this
release, `COMPOSIO_API_KEY` was propagated automatically. Propagation
is now declarative and the default is empty (no credential is sent to
the subprocess from the `source` files). If you depended on that
behaviour, add the following to your `config.local.json`:

    {"settings": {"credentials": {"propagate": ["COMPOSIO_API_KEY"]}}}

To use a per-skill `.env` as the primary source:

    {"settings": {"credentials": {
      "source": ["./.env", "~/.claude/credentials.env"],
      "propagate": ["GITHUB_TOKEN"]
    }}}

## Recognised environment variables

| Variable | Purpose |
|---|---|
| `SKILLS_PYTHON` | Preferred Python interpreter. |
| `CLAUDE_AUTOMATION_PYTHON` | Python fallback. |
| `CODEX_WRAPPER_TIMEOUT_SECONDS` | Global wall-clock override (seconds). Overrides the per-mode ceiling. |
| `CODEX_WRAPPER_IDLE_TIMEOUT_SECONDS` | Kill Codex after this many seconds without a single event on the stream. Overrides the per-mode `wrapper.mode_idle_timeouts` (180s for `ask` up to 600s for `delegate`, which legitimately runs long silent commands). `0` disables the idle guard and leaves only the wall clock. |
| `CODEX_WRAPPER_CACHE_DIR` | Where `runs.jsonl` telemetry is written. Point it at a scratch directory to keep a test run from polluting or rotating away the real record. |
| `CODEX_WRAPPER_MODEL` | Model slug passed as `codex exec -m`. Overrides `wrapper.model`, whose default (`auto`) resolves the strongest model the CLI advertises. |
| `CODEX_WRAPPER_SERVICE_TIER` | Codex service tier. `priority` is the fast tier (default); `default` opts out and trades speed for quota. |
| `CODEX_WRAPPER_CODEX_OVERRIDE` | Points to an alternative Python script invoked instead of the real `codex` (testing only — see `tests/fake_codex.py`). |
| `CODEX_WRAPPER_DISABLE_HEARTBEAT` | When `1`, disables the progress heartbeat on `stderr`. |
| `CODEX_WRAPPER_HEARTBEAT_INTERVAL_SECONDS` | Seconds between heartbeat lines (default 15). |
| `CODEX_WRAPPER_USE_JSON_STREAM` | `codex exec --json` is on by default because the idle guard is fed by it. Set to `0` to drop the flag, which also disables the idle guard. |
| `CODEX_WRAPPER_TELEMETRY_DISABLED` | When `1`, skips writing to `cache/runs.jsonl`. |
| `CODEX_BG_MAX_CONCURRENT` | Concurrent background runs limit (default 5). Override with `--max-concurrent N` on `codex_bg.py start`. |
| `CODEX_DIALOGUE_MAX_TURNS` | Default turn count for `codex_dialogue.py start` (default 3, range 1-20). Override with `--max-turns N`. |
| `CODEX_LOCALE` | User-facing message language (`pt-BR` or `en-US`). Default: detected from the system (`locale.getdefaultlocale()`), falling back to `pt-BR`. |

## Layout

```
~/.claude/skills/codex/
  SKILL.md                       — skill entry point (modes, examples, rules)
  SETUP.md                       — this file
  scripts/
    invoke_codex_with_claude.py  — canonical wrapper (Python)
    invoke_codex_with_claude.ps1 — compatibility shim (PowerShell)
    collect_claude_context.py    — collects global/repo/target CLAUDE.md
    dump_transcript_for_codex.py — filtered session transcript dump
    build_review_packet.py       — builds packet for `plan-review`
    normalize_codex_result.py    — normalises raw Codex output
    codex_batch.py               — synchronous batch-ask / batch-delegate runner
    codex_bg.py                  — async runner (start/status/output/cancel/list)
    codex_dialogue.py            — iterative multi-turn dialogue (start/next-turn/finish/abort/status)
    analyze_plan_complexity.py   — heuristic to auto-suggest plan-review-iter
    codex_config.py              — centralised config + i18n (get/t/detect_locale)
    setup.py                     — interactive setup wizard
    set_locale.py                — non-interactive locale setter
    codex_output_schema.json     — JSON Schema used by `--output-schema`
  config.default.json            — versioned settings + locales
  config.local.json              — user override (gitignored, optional)
  cache/
    runs.jsonl                   — telemetry (rotates to .1 at 5 MB)
  tests/
    fake_codex.py                — parametrisable fake Codex for tests
```

## Configuration and i18n

The skill loads settings + locales from `config.default.json` (versioned)
and deep-merges `config.local.json` (gitignored, optional) on top.
Helpers in `scripts/codex_config.py`:

- `get("dialogue.default_max_turns", 3)` — dotted-path lookup into
  `settings.*`.
- `t("wrapper.error.timeout", timeout=30)` — translation with fallback
  pt-BR → literal key, supporting `{kwargs}`.
- `detect_locale()` — `CODEX_LOCALE` env > `settings.locale` in
  `config.local.json` > `locale.getdefaultlocale()` > `pt-BR`. Codes
  with underscore (`pt_BR`) are normalised to hyphen (`pt-BR`).

**To customise**: copy fields from `config.default.json` into a new
`config.local.json`. Only the keys present in the override are
overridden (deep-merge).

Locales supported out of the box: `pt-BR` (default) and `en-US`. To add
a new locale, add a `locales.<code>` section in `config.local.json` (or
open a PR to land it in the default).

## Working with line endings & locale

### Line endings

A `.gitattributes` at the root pins the EOL convention so contributors on
Linux, macOS, and Windows all get a clean `git status` after a fresh
clone:

- Default: `text=auto eol=lf` — every text file is normalised to LF in
  the index and on checkout.
- `*.ps1` and `*.bat` are pinned to CRLF — PowerShell and CMD expect
  it.
- `*.png` and `*.jpg` are flagged binary so git never tries to apply
  EOL conversion.

If `git status` shows EOL diffs immediately after a clone, your local
`core.autocrlf` is overriding the repo's `.gitattributes`. Either set
`core.autocrlf=false` or run `git add --renormalize .` and commit the
result on a feature branch.

### `config.local.json` is gitignored by design

Per-user customisations (locale, credential propagate list, dialogue
turn count, etc.) live in `config.local.json`. The file is in
`.gitignore` so private tweaks never leak into a PR. The supported way
to change the locale is the helper script:

```sh
"$SKILLS_PYTHON" scripts/set_locale.py --locale en-US
```

Hand-editing `config.local.json` is also fine — both paths persist the
same key.

### `CODEX_LOCALE` runtime override

Set `CODEX_LOCALE` to override the locale for a single shell or single
invocation, without touching `config.local.json`:

```sh
CODEX_LOCALE=en-US "$SKILLS_PYTHON" scripts/invoke_codex_with_claude.py ask --question-file q.txt
```

Codes with underscore (`pt_BR`) are normalised to hyphen (`pt-BR`).

### Test suites are pinned to `en-US`

The mocked suite (`tests/test_codex_skill.py`) and the live suite
(`tests/test_codex_live.py`) both export `CODEX_LOCALE=en-US` from
inside their helpers (`_base_env` and `_live_env`). This means:

- The asserts compare against the en-US strings from
  `config.default.json`, no matter what `config.local.json` says.
- Running on a CI machine with no `config.local.json` works identically
  to running on a developer machine that defaulted to pt-BR.
- If you intentionally want to inspect the pt-BR prompts via the test
  suite, run `set_locale.py --locale pt-BR` and then run the wrapper
  manually — the test runner itself will keep using en-US, so its
  asserts will still apply.

## Modes

See `SKILL.md` for details. Summary:

- `plan-review` — Codex reviews a plan (read-only).
- `verify` — Codex reviews a diff (read-only).
- `ask` — Codex answers a direct question.
- `insight` — Holistic session retrospective.
- `delegate` — Codex executes a task (`--sandbox danger-full-access`,
  explicit confirmation required).
- `batch-ask` — Runs multiple questions in parallel (read-only).
- `batch-delegate` — Multiple parallel executions with declared
  write-set.

## Roadmap

See `ROADMAP.md` at the skill root. Open work:

- Public-repo polish (CI, CHANGELOG, CONTRIBUTING, GitHub templates,
  `.gitattributes`).
- Robust `codex exec --json` stream parser, review caching by
  fingerprint, `repro` mode.

## Notes for other users

The skill is portable: paths come from `$SKILLS_PYTHON`, `$USERPROFILE`,
and `$env:TEMP`. Nothing is hardcoded to the author's machine. If you
do not have `$SKILLS_PYTHON`, export the variable (or use `python` on
`PATH`).

## Official `codex@openai-codex` plugin

Disabled in `~/.claude/settings.json` to avoid two competing entry
points. Manual invocation via `/codex:*` is still possible when the
user re-enables the plugin, but it should not be used as an automation
path by any skill or hook.
