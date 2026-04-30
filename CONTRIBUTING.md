# Contributing

Thanks for considering a contribution. This skill is small and stdlib-only;
the bar to set up a dev environment is low.

## Local setup

1. Clone the repo:

   ```sh
   git clone https://github.com/lucas-lima-s/claude-codex-skill.git
   cd claude-codex-skill
   ```

2. Make sure you have Python 3.10 or newer on `PATH`. The skill itself has
   no third-party runtime dependencies; only the dev tooling does.

3. Set `SKILLS_PYTHON` to the interpreter you want the skill to use, then
   install the dev dependencies into it:

   ```sh
   export SKILLS_PYTHON=$(which python)   # or any 3.10+ interpreter
   "$SKILLS_PYTHON" -m pip install -r requirements-dev.txt
   ```

   On Windows (Git Bash):

   ```sh
   export SKILLS_PYTHON="/c/Python313/python.exe"
   "$SKILLS_PYTHON" -m pip install -r requirements-dev.txt
   ```

## Run the mocked test suite

The mocked suite is the only suite we run automatically. It is self-contained,
spends no Codex tokens, and pins `CODEX_LOCALE=en-US` internally, so it works
on any machine regardless of how the user has configured `config.local.json`.

```sh
"$SKILLS_PYTHON" tests/test_codex_skill.py
```

Exit code is the number of failed assertions (0 means all passed).

### Forcing specific paths via `FAKE_CODEX_BEHAVIOR`

`tests/fake_codex.py` is a configurable Codex stand-in. Pick a behaviour
via the env var when iterating on a single mode:

| Behaviour | What it simulates |
|---|---|
| `success` | Codex returns the expected JSON shape |
| `invalid_json` | Codex returns prose where JSON is required |
| `partial` | Codex returns degraded but salvageable JSON |
| `nonzero` | Codex exits with status 2 |
| `timeout` | Codex hangs past the configured timeout |
| `noisy_stderr` | Codex prints 50 lines of unrelated stderr noise |
| `delay_short` | Codex takes ~2s to respond (good for batch speedup tests) |
| `delay_long` | Codex takes ~5s to respond |
| `needs_input` | Codex returns `status=needs_input` with a clarifying question |
| `delegate_ok` | Codex returns the delegate-shaped JSON (files_created, files_edited, commands_run) |

The wrapper still runs through `tests/fake_codex.py` for every behaviour;
the env var only changes which canned response the fake produces.

## Do not run the live suite by accident

`tests/test_codex_live.py` calls the real `codex` CLI. Every assertion
**spends real Codex tokens**. The CI runner does not invoke it. Only run
it manually when you need to validate that the wrapper still composes
the right `codex exec` command.

```sh
# WARNING: this spends tokens against the configured Codex account
"$SKILLS_PYTHON" tests/test_codex_live.py
```

## Adding a new mode

Modes are wired up in `scripts/invoke_codex_with_claude.py`. The minimum
to land a new one:

1. Extend the operation enum and CLI plumbing in
   `scripts/invoke_codex_with_claude.py`.
2. Add localised prompts under both `en-US` and `pt-BR` in
   `config.default.json`. If you only have an English version, copy it
   into the pt-BR bundle as well — never leave a key resolving to the
   literal in production.
3. Teach `tests/fake_codex.py` to return a believable canned response
   for the new mode.
4. Add a test case in `tests/test_codex_skill.py` that exercises both
   the success path and at least one error path (`invalid_json`,
   `nonzero`, or `timeout`).
5. Update `SKILL.md` with the new trigger phrases (English and pt-BR).

## Commit policy

- **Subject lines in English.** The trigger phrases in `SKILL.md` and the
  pt-BR locale strings in `config.default.json` are the only intentional
  pt-BR in the repo.
- Conventional Commits is welcome but not required. Match the existing
  history (`feat:`, `chore:`, `docs:`, `style:`, `fix:`).
- One logical change per commit. Reformat-only changes go in their own
  `style:` commit so the meat-of-the-change diff stays readable.
- Update `CHANGELOG.md` under `[Unreleased]` for any user-visible change.

## Code style

- **stdlib only** for runtime code. `requirements-dev.txt` is for tooling
  (`ruff`, `black`) and is not shipped to users.
- `from __future__ import annotations` at the top of every Python file.
- `pathlib.Path` over raw string paths.
- Prefer builtin generics (`list[str]`, `dict[str, Any]`) and PEP 604
  unions (`X | None`) over the deprecated `typing` aliases.
- Lint and formatting are enforced in CI:

  ```sh
  "$SKILLS_PYTHON" -m ruff check .
  "$SKILLS_PYTHON" -m black --check .
  ```

  Both are configured in `pyproject.toml` (line length 120, target Python
  3.10).
