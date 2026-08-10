# claude-codex-skill

A skill for [Claude Code](https://claude.com/claude-code) that bridges
Claude and the [Codex CLI](https://github.com/openai/codex), offering
eight operation modes for plan review, code verification, questions, and
task delegation — with controlled sandbox, telemetry, and batching.

## What it is

A single entry point (`SKILL.md` + Python wrapper) for Claude to invoke
Codex in a structured way. Prompts, schemas, timeouts, and reasoning
levels are configured per mode — Claude picks the right mode from the
user's natural-language request and the wrapper handles everything else
(packet assembly, `codex exec` invocation, output JSON normalization, and
telemetry).

## Modes

| Mode                 | Reasoning | Sandbox                | Floor → ceiling | Typical use                                            |
|----------------------|-----------|------------------------|-----------------|--------------------------------------------------------|
| `plan-review`        | max       | read-only              | 900s → 3600s    | Review a plan before implementation                    |
| `verify`             | high      | read-only              | 600s → 2400s    | Review an implementation through `git diff`            |
| `ask`                | medium    | read-only              | 300s → 1200s    | Direct question / second opinion                       |
| `insight`            | max       | read-only              | 1200s → 4800s   | Holistic session retrospective                         |
| `delegate`           | max       | **danger-full-access** | 900s → 3600s    | Codex executes a task (explicit confirmation required) |
| `batch-ask`          | medium    | read-only              | —               | Up to 4 questions in parallel                          |
| `batch-delegate`     | max       | danger-full-access     | —               | Multiple parallel executions with declared write-set   |
| `batch-plan-review`  | max       | read-only              | —               | Plan slices reviewed in parallel, findings deduplicated |

Every mode runs on the strongest model the Codex CLI advertises, on the fast
service tier. The wall-clock budget is not a constant: the floor is what a small
target gets, and the wrapper raises its own ceiling with the size of the prompt
it assembled, up to 4x. A large plan reviewed under a small plan's ceiling dies
mid-review and reports zero findings, which is indistinguishable from a clean
result. Runs are also supervised through the Codex event stream, so a Codex that
stops emitting events for 180s is killed well before either bound.

`plan-review` and `verify` sweep a fixed checklist of categories and report
`coverage` alongside the findings, so an empty category is visibly empty rather
than silently unexamined.

### Failure is never silent

Every entry point mirrors its `status` in the process exit code — `0` for `ok`,
`2` for `error`, `3` for `needs_input` — while still writing the full JSON to
stdout. A caller that checks only the exit code cannot mistake a timeout for a
spotless review. The same rule holds one level up: `codex_bg.py start` refuses
an unknown mode before spawning anything, probes the spawned process for 3s and
reports `died_on_startup` with the captured stderr instead of handing out a
`run_id` for a process that is already gone; and a `codex_dialogue.py` turn
whose wrapper failed comes back with envelope `status: error`, so two timed-out
turns are never read as convergence.

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

See [`SETUP.md`](SETUP.md) for details, and
[`CONTRIBUTING.md`](CONTRIBUTING.md) for development setup and the test
suite.

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
│   ├── codex_bg.py                  # detached background runs
│   ├── codex_dialogue.py            # iterative multi-turn plan review
│   ├── analyze_plan_complexity.py   # complexity score + raw metrics + routing flags
│   ├── split_plan_by_phase.py       # per-phase slices + coherence slice
│   ├── build_review_packet.py       # builds packet for plan-review
│   ├── collect_claude_context.py    # collects global/repo/target CLAUDE.md
│   ├── dump_transcript_for_codex.py # filtered transcript dump
│   ├── normalize_codex_result.py    # normalises raw Codex output
│   └── codex_output_schema.json     # JSON Schema (--output-schema)
└── tests/
    ├── test_codex_skill.py          # mocked tests (fast)
    ├── test_codex_bg.py             # background runner tests
    ├── test_codex_live.py           # tests against the real Codex (cost tokens)
    └── fake_codex.py                # parametrisable fake Codex
```

## Large plans

A plan big enough to hold several phases does not fit one review. Before every
`plan-review`, `analyze_plan_complexity.py` scores the plan and returns
`suggest_iterative` / `suggest_split` plus the raw `metrics` behind them. From
four phases and 16 KB up, the skill splits the plan with
`split_plan_by_phase.py` into one slice per phase plus a **coherence slice**,
and reviews all of them in parallel through `batch-plan-review`.

Each phase slice repeats the plan's shared context and the outline of the other
phases, so no reviewer reads blind. The coherence slice carries the whole plan
with an instruction to look only at structure — ordering, cross-phase
dependencies, contradictions, boundary gaps — which is precisely what slicing
would otherwise throw away. Findings come back deduplicated by title and
location, each naming the slices that raised it. A plan with no phase headings
returns `not_splittable` and falls back to the monolithic review, rather than
reviewing one slice and calling it coverage.

The slices are routed by how long they can afford to take. Phase slices are
interactive and sized to fit `split.interactive_budget_seconds` (300s); the
coherence slice reads the whole plan, is always the slowest, and goes to
`bg-start` so it never holds the round hostage. You get the phase findings
inside the budget and collect the structural one by `run_id`.

Parallelism is deliberately capped at 3. Every item of a batch draws on the
same Codex quota simultaneously, so a wide round costs N reviews at once: a
7-way round over a 47 KB plan was observed degrading badly and exhausting an
account's quota in a single shot. When that happens the wrapper reports
`error_class: quota_exhausted` with the reset date instead of a generic
non-zero exit, and the batch stops dispatching rather than driving the
remaining slices into the same wall.

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

The skill is in real use. Open work is listed in
[`ROADMAP.md`](ROADMAP.md):

- Robust `codex exec --json` stream parser.
- Review caching by packet fingerprint.
- `repro` mode for replaying a `run_id`'s prompt + output.

## License

[MIT](LICENSE).
