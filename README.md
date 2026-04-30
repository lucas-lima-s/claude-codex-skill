# claude-codex-skill

A skill for [Claude Code](https://claude.com/claude-code) that bridges
Claude and the [Codex CLI](https://github.com/openai/codex), offering
seven operation modes for plan review, code verification, questions, and
task delegation — with controlled sandbox, telemetry, and batching.

## What it is

A single entry point (`SKILL.md` + Python wrapper) for Claude to invoke
Codex in a structured way. Prompts, schemas, timeouts, and reasoning
levels are configured per mode — Claude picks the right mode from the
user's natural-language request and the wrapper handles everything else
(packet assembly, `codex exec` invocation, output JSON normalization, and
telemetry).

## Modes

| Mode             | Reasoning | Sandbox                | Timeout | Typical use                                                   |
|------------------|-----------|------------------------|---------|---------------------------------------------------------------|
| `plan-review`    | xhigh     | read-only              | 300s    | Review a plan before implementation                           |
| `verify`         | medium    | read-only              | 180s    | Review an implementation through `git diff`                   |
| `ask`            | medium    | read-only              | 120s    | Direct question / second opinion                              |
| `insight`        | xhigh     | read-only              | 420s    | Holistic session retrospective                                |
| `delegate`       | xhigh     | **danger-full-access** | 300s    | Codex executes a task (explicit confirmation required)        |
| `batch-ask`      | medium    | read-only              | —       | Up to 4 questions in parallel                                 |
| `batch-delegate` | xhigh     | danger-full-access     | —       | Multiple parallel executions with declared write-set          |

## Installation

```bash
git clone https://github.com/lucas-lima-s/claude-codex-skill.git ~/.claude/skills/codex
```

The skill is portable: paths come from environment variables
(`$SKILLS_PYTHON`, `$USERPROFILE`, `$env:TEMP`). No paths are hardcoded.

### Prerequisites

- [Codex CLI](https://github.com/openai/codex) installed and on `PATH`
  (`codex --version` must respond).
- Python 3.10+ available via `$SKILLS_PYTHON`, `$CLAUDE_AUTOMATION_PYTHON`,
  or just `python` / `python3` on `PATH`.
- Claude Code (the skill is designed to be invoked by Claude, but the
  scripts under `scripts/` can be used standalone).

See [`SETUP.md`](SETUP.md) for details.

## Usage

Once installed, Claude automatically recognises natural-language phrases
such as:

- "review this plan with codex" → `plan-review`
- "ask codex what it thinks" → `ask`
- "verify my implementation with codex" → `verify`
- "delegate to codex" → `delegate` (requires explicit confirmation)
- "do a session insight" → `insight`

(Trigger phrases are matched in pt-BR by default — see `SKILL.md`.)

## Structure

```
.
├── SKILL.md                       # skill entry point (modes, examples, rules)
├── SETUP.md                       # requirements, env vars, installation
├── ROADMAP.md                     # open items (not implemented yet)
├── README.md                      # this file
├── LICENSE                        # MIT
├── scripts/
│   ├── invoke_codex_with_claude.py  # canonical wrapper
│   ├── invoke_codex_with_claude.ps1 # PowerShell shim
│   ├── codex_batch.py               # parallel batching (batch-* modes)
│   ├── build_review_packet.py       # builds packet for plan-review
│   ├── collect_claude_context.py    # collects global/repo/target CLAUDE.md
│   ├── dump_transcript_for_codex.py # filtered transcript dump
│   ├── normalize_codex_result.py    # normalises raw Codex output
│   └── codex_output_schema.json     # JSON Schema (--output-schema)
└── tests/
    ├── test_codex_skill.py          # mocked tests (fast)
    ├── test_codex_live.py           # tests against the real Codex (cost tokens)
    └── fake_codex.py                # parametrisable fake Codex
```

## Security

- **`delegate` runs with `--sandbox danger-full-access`** — Codex can
  create, edit, or delete files anywhere on disk. The skill requires
  explicit user confirmation before every call, listing the literal
  task, `cwd`, branch, paths outside the workspace, and risk keywords
  detected (`delete`, `rm -rf`, `force`, `reset --hard`, etc.).
- All other modes use `--sandbox read-only`.
- Credentials propagated to the subprocess are declarative in
  `settings.credentials.propagate` (config-driven, empty by default).
  Sources live in `settings.credentials.source` (ordered list, default
  `["./.env", "~/.claude/credentials.env"]`). Only the listed keys are
  injected into the subprocess env from those files — values never
  appear in logs. Variables already present in the parent process env
  are inherited as in any POSIX subprocess.
- Local telemetry (`cache/runs.jsonl`) is in `.gitignore` by default.

## Status and roadmap

The skill is in real use. Open items (not yet implemented) are listed in
[`ROADMAP.md`](ROADMAP.md):

- Named background agents (non-blocking execution).
- Multi-turn Claude ↔ Codex dialogue (up to 5 turns).
- Review caching by fingerprint, repro mode, `--json` stream parser.

## License

[MIT](LICENSE).
