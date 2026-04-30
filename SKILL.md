---
name: codex
description: Delegate one of five operations to Codex — review a plan (plan-review), verify an implementation via git diff (verify), answer a question/opinion (ask), produce a holistic session retrospective (insight), or execute a task (delegate). Use this skill ALWAYS when the user says any of these phrases (English: "review this plan with codex", "have codex review", "ask codex what it thinks", "second opinion from codex", "verify my implementation with codex", "have codex look at what I did", "delegate to codex", "send it to codex", "do a session insight", "retrospective with codex"; or pt-BR equivalents like "revise esse plano com o codex", "pergunte ao codex o que ele acha", "verifica minha implementação com o codex", "manda o codex fazer", "delega ao codex", "faz um insight da sessão"). This skill is the ONLY entry point to invoke Codex automatically — do not use the /codex:* plugin. For `delegate` mode, ALWAYS confirm with the user before running because Codex runs with `--sandbox danger-full-access` and can edit/delete files inside or outside the workspace.
argument-hint: plan-review|plan-review-iter|verify|ask|insight|delegate|batch-ask|batch-delegate|bg-start|bg-status|bg-output|bg-cancel|bg-list [args]
allowed-tools:
  - Read
  - Write
  - Bash(git status*)
  - Bash(git diff*)
  - Bash(git rev-parse*)
  - Bash(*scripts/invoke_codex_with_claude.py*)
  - Bash(*scripts/invoke_codex_with_claude.ps1*)
  - Bash(*scripts/dump_transcript_for_codex.py*)
  - Bash(*scripts/build_review_packet.py*)
  - Bash(*scripts/codex_batch.py*)
  - Bash(*scripts/codex_bg.py*)
  - Bash(*scripts/analyze_plan_complexity.py*)
  - Bash(*scripts/codex_dialogue.py*)
  - Bash(*scripts/set_locale.py*)
---

# Codex — single entry point

Invokes the wrapper at
`~/.claude/skills/codex/scripts/invoke_codex_with_claude.py` in one of
the modes below. Never call the `codex` CLI directly, never use the
official `/codex:*` plugin from this skill — that one is an explicit
manual user bypass, not an automation path.

**Reasoning per mode** (controlled by the wrapper, do not override
manually):

| Mode | Reasoning | Sandbox | Default timeout |
|---|---|---|---|
| `plan-review` | `xhigh` | `read-only` | 300s |
| `verify` | `medium` | `read-only` | 180s |
| `ask` | `medium` | `read-only` | 120s |
| `insight` | `xhigh` | `read-only` | 420s |
| `delegate` | `xhigh` | `danger-full-access` | 300s |

**Natural-phrase → mode mapping** (Claude matches either language
naturally; English is listed first as the documentation default,
Portuguese phrases follow as user-spoken equivalents):

| Trigger phrase (English / pt-BR) | Mode |
|---|---|
| "review this plan with codex" / "have codex review" / "ask codex to review" / "revise esse plano com o codex" / "revisa pelo codex" / "pede pro codex revisar" | `plan-review` |
| "iteratively review with codex" / "open a discussion with codex" / "go back and forth with codex until convergence" / "multi-turn round with codex" / "revisa iterativamente com o codex" / "abre uma discussão com o codex sobre esse plano" / "vai e volta com o codex até convergir" / "rodada multi-jogada com o codex" | `plan-review-iter` |
| "ask codex what it thinks" / "ask codex" / "second opinion from codex" / "pergunte ao codex o que ele acha" / "pergunta pro codex" / "segunda opinião do codex" | `ask` |
| "verify my implementation with codex" / "have codex look at what I did" / "verifica minha implementação com o codex" / "pede pro codex olhar o que eu fiz" | `verify` |
| "implement this plan with codex" / "send it to codex" / "delegate to codex" / "implemente esse plano com o codex" / "manda o codex fazer" / "delega ao codex" | `delegate` |
| "do a session insight" / "analyse what we did" / "retrospective with codex" / "faz um insight da sessão" / "analisa o que a gente fez" / "retrospectiva pelo codex" | `insight` |
| "send this to codex" / "manda isso pro codex" (ambiguous) | ask which mode |

## Session transcript (conversational context)

Per-mode policy:

| Mode | Includes transcript? | Inline turns | Full jsonl path? |
|---|---|---|---|
| `plan-review` | YES (always) | last 10 | no |
| `verify` | YES (always) | last 15 | no |
| `insight` | YES (always) | last 40 | YES |
| `ask` | only if the user requests | last 10 | no |
| `delegate` | only if the user requests | last 10 | no |

**How to generate:**

```bash
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/dump_transcript_for_codex.py" \
  --cwd "<cwd>" \
  --last-turns <N> \
  --output "$env:TEMP/codex_transcript_<timestamp>.txt"
```

`--last-turns 0` = whole session (use only in `insight`).

**When to skip:**
- Brand-new session (first turn) — skip.
- User asked for "no context" / "ignore the conversation" — skip.
- Conversation unrelated to the request — skip.

## Mode 1 — `plan-review` *(review a plan)*

**When:** the user wants to validate a plan before running it.

**Input:** plan file path. If absent:
1. If there is a plan file from the current plan mode (system reminder),
   use it.
2. If the user pasted text, write it to
   `$env:TEMP/codex_plan_<timestamp>.md`.
3. Otherwise, ask.

**Required step before invoking the wrapper:** run
`scripts/analyze_plan_complexity.py --plan-file <path>`. If the output
has `suggest_iterative=true`, **show a single suggestion line** to the
user (format: `"This plan [N] files across [paths], [M] phases — want
to run plan-review-iter (up to 3 turns) instead of the one-shot
review?"`) and ask before proceeding. If the user accepts, switch to
`plan-review-iter`. If they decline (or stay silent), invoke
`plan-review` normally. **Do not suggest twice for the same
plan/turn.**

**Packet:** the wrapper assembles the review packet automatically when
it receives `--last-message-file` (resolves cited files, applies the
±50 window, builds the manifest, truncates at 120 KB). To force a
custom packet, generate it beforehand via
`scripts/build_review_packet.py` and pass `--review-packet-file`.

**Execution:**

```bash
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/invoke_codex_with_claude.py" plan-review \
  --cwd "<cwd>" \
  --target-path "<optional subfolder>" \
  --last-message-file "<plan path>" \
  --transcript-file "<last-10-turns dump>"
```

## Mode 1b — `plan-review-iter` *(iterative multi-turn review)*

**When:** the user explicitly asked for it OR accepted the auto-suggestion
from the complexity step. Worth iterating with Codex for large or
sensitive plans, or those touching multiple files/phases.

**Mechanics:** Claude and Codex iterate up to 3 turns by default
(configurable via `--max-turns N` or `CODEX_DIALOGUE_MAX_TURNS`, range
1-20). On each turn: Codex reviews the plan, Claude presents findings
1-by-1 (translated), opens `AskUserQuestion` to approve/reject each,
revises the plan, and triggers the next turn.

**Stop criteria (automatic):**
- **Convergence**: 2 consecutive turns with `findings=[]` AND
  `severity=low` AND `block_recommended=false`.
- **Limit**: `current_turn >= max_turns`.
- **Divergence**: same `high` finding (match by `title + location`) in
  2 consecutive turns → escalate to the user ("Codex and I disagree on
  point X").
- **Manual abort**: user said "stop here" / "enough" / "end the
  discussion" → Claude calls `codex_dialogue.py abort`.

**Inviolable per-turn transparency rule:** even in the iterative flow,
every finding of the current turn is presented translated, 1-by-1,
with a meta table + numbered list **before** any `AskUserQuestion`.
High severity changes the tone of the final question (recommends
aborting) — but **never** changes the order in which findings are
disclosed.

**Execution:** the `start` subcommand requires `--accepted-by-user` to
prevent Claude from launching a Codex review without confirmation.
Always ask before passing the flag.

```bash
# Turn 1 (only after the user accepts the one-line suggestion!)
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/codex_dialogue.py" start \
  --accepted-by-user \
  --plan-file "<path>" \
  --cwd "<cwd>" \
  [--max-turns N]
# {"status": "ok", "dialogue_id": "abc...", "turn": 1, "findings_payload": {...}}

# Subsequent turns (Claude writes the revised plan to a new file)
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/codex_dialogue.py" next-turn \
  --dialogue-id "abc..." \
  --plan-file "<revised plan path>"

# Finish (generates dialogue_log.md + final_plan.md)
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/codex_dialogue.py" finish \
  --dialogue-id "abc..."

# Abort at any turn
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/codex_dialogue.py" abort \
  --dialogue-id "abc..."
```

**Final presentation** (after `finish`): show the consolidated
`dialogue_log.md` — contains the turn 1 → turn N delta
(added/removed/modified sections), a `turn | severity | findings |
block | summary` table, the last turn's pending findings, and the
final plan. **Do not** verbatim-dump every turn.

## Mode 2 — `verify` *(review an implementation)*

**When:** the user just edited code and wants Codex to look at the diff.

**Payload:** JSON in `$env:TEMP` with:

```json
{
  "cwd": "<cwd>",
  "last_assistant_message": "<short description of what was done, or empty>",
  "transcript_path": "",
  "git_status_short": "<git status --short>",
  "git_diff_worktree": "<git diff --no-ext-diff --relative HEAD -->",
  "git_diff_cached": "<git diff --no-ext-diff --relative --cached>",
  "changed_files_from_transcript": []
}
```

**Execution:**

```bash
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/invoke_codex_with_claude.py" verify \
  --cwd "<cwd>" \
  --payload-file "<path/payload.json>" \
  --transcript-file "<last-15-turns dump>"
```

## Mode 3 — `ask` *(question / opinion)*

**When:** Codex's opinion on text / general technical question.

```bash
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/invoke_codex_with_claude.py" ask \
  --cwd "<cwd>" \
  --target-path "<optional subfolder>" \
  --question-file "<question path>"
```

## Mode 4 — `insight` *(retrospective)*

**When:** strategic analysis — what was done, gaps, next steps. NOT a
bug hunt.

**Required:** `--transcript-file` with the whole session
(`--last-turns 0`).

```bash
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/invoke_codex_with_claude.py" insight \
  --cwd "<cwd>" \
  --focus-file "<focus path (optional)>" \
  --transcript-file "<full dump>" \
  --transcript-jsonl-path "<absolute path to the session jsonl>"
```

## Mode 5 — `delegate` *(Codex executes a task)*

**When:** the user explicitly says "send it to codex" / "delegate to codex".

**Do not use** if the task is ambiguous (ask for `plan-review` first) or
if the user wants Claude to do it.

### Sandbox

`delegate` runs with `--sandbox danger-full-access`. Codex can create,
edit, or delete **any file on the machine, inside or outside `--cwd`**.

### Required confirmation — 5 fields

Before calling the wrapper, show the user **all five**:

1. **Literal task** that will be sent to Codex (full text of the
   `task-file`).
2. **`cwd` and branch** target (include
   `git rev-parse --abbrev-ref HEAD`).
3. **Explicit warning**: "Codex runs with `danger-full-access`. It can
   create, edit, or delete files anywhere on disk, not only in
   `<cwd>`."
4. **Paths outside the workspace** that Claude can infer from the task
   text (any absolute path outside `<cwd>`, any mention of
   `~`/`$HOME`/`$env:TEMP`/`C:\` outside the project).
5. **Risk keywords** detected in the task text: `delete`, `drop`,
   `rm -rf`, `force`, `reset --hard`, `truncate`, `--no-verify`. List
   them one by one. If none are found, say "no risk keyword detected".

After showing those, ask: **"Confirm execution with
`danger-full-access`? (yes/no)"**. Only proceed on an affirmative
answer.

### Execution

```bash
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/invoke_codex_with_claude.py" delegate \
  --cwd "<cwd>" \
  --target-path "<optional subfolder>" \
  --task-file "<path/task.txt>" \
  [--transcript-file "<dump (only if user requested context)>"]
```

### Expected output

The wrapper JSON includes (in addition to canonical fields):

```
files_created   [str]   new files
files_edited    [str]   modified files
files_deleted   [str]   removed files
commands_run    [str]   shell commands executed
tests_run       [str]   tests / checks executed
```

After presenting the result, run `git status --short` and show it in a
code block (the only `code fence` allowed in the presentation — literal
command output).

### `needs_input` protocol

Codex may interrupt with `status=needs_input` and return `questions` (a
list of `{id, question, context}`). When that happens:

1. **Do not escalate immediately.** Try to resolve locally with up to
   **2 passes** of Read/Grep/Glob/Bash. Each pass should target a
   specific question.
2. If the 2 passes resolve every question, **resume the call**: produce
   a new `task-file` that includes the prior context (original task +
   questions + obtained answers) and trigger `delegate` again. Do not
   use Codex's `--resume`; the state is fully reloaded via prompt.
3. If any item requires human judgment (design preference, product
   decision, extra authorization), **escalate to the user** with the
   exact question and the context Claude has already gathered — do not
   trigger the resume on your own.

## Mode 6 — `batch-ask` *(parallel questions)*

Read-only. Runs up to 4 questions in parallel via
`scripts/codex_batch.py`. A single item failing does not cancel the
others. Aggregated response with `partial=true` if there is a partial
error. See `scripts/codex_batch.py --help` for details.

## Mode 7 — `batch-delegate` *(parallel delegate with write-set)*

Each item declares a write-set (path list). The batcher refuses to run
when two write-sets overlap. After each execution, the declared
write-set is compared against the reported
`files_created/edited/deleted`; the run is marked
`write_set_violated=true` when Codex goes outside it.

## Background modes — `bg-start | bg-status | bg-output | bg-cancel | bg-list`

When a wrapper call is expected to take a while (e.g. `delegate` >60s,
`insight` >5min) and blocking the session is undesirable, use
`scripts/codex_bg.py`:

| Subcommand | Purpose |
|---|---|
| `bg-start <mode> [...args]` | Detached wrapper spawn. Returns immediately with `run_id` and `pid`. |
| `bg-status <run_id>` | Current state: `running \| done \| error \| cancelled`. |
| `bg-output <run_id>` | Wrapper canonical JSON (same schema as the synchronous modes) when `done`. |
| `bg-cancel <run_id>` | Kills the subprocess and marks the run as `cancelled`. |
| `bg-list [--limit N]` | Lists active and recent runs. |

**Operational rules:**
- `bg-start` returns control immediately. **ALWAYS** show the `run_id`
  to the user at start time so they can resume with
  `bg-status <run_id>` even in future sessions.
- Default limit of **5 concurrent runs**. Configurable via
  `--max-concurrent N` or `CODEX_BG_MAX_CONCURRENT`. When the limit is
  hit, `bg-start` refuses with
  `status=error, reason=max_concurrent_reached`.
- `bg-cancel` is idempotent.
- Automatic cleanup: terminated runs older than 7 days (mtime) are
  removed.

**Example:**

```bash
# fire
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/codex_bg.py" \
  start delegate \
  --cwd "<cwd>" \
  --task-file "<task.txt>"
# {"status": "ok", "run_id": "abc123def456", "pid": 1234, ...}

# check later
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/codex_bg.py" status abc123def456

# collect when done
"$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/codex_bg.py" output abc123def456
```

`bg-output` returns the wrapper's canonical JSON — present it to the
user with the same formatting as the synchronous modes (meta table +
numbered list of translated findings).

## Locale switch (Claude-side, via `set_locale.py`)

Trigger phrases (English first, pt-BR equivalents follow): "configure
codex language", "switch codex language", "switch codex to English",
"switch codex to pt-BR", "codex in English", "codex in pt-br", "switch
codex locale", "configurar idioma do codex", "trocar idioma do codex",
"muda o codex pra inglês", "codex em inglês", "codex em pt-br",
"trocar locale do codex".

Action:

1. Ask the user via `AskUserQuestion` which locale: `en-US` or `pt-BR`.
2. Invoke via Bash:
   ```
   "$SKILLS_PYTHON" "$USERPROFILE/.claude/skills/codex/scripts/set_locale.py" --locale <choice>
   ```
   (or `python` if `$SKILLS_PYTHON` is not set).
3. Confirm to the user: "Codex locale set to `<choice>`. The next
   /codex calls will use that language (UI messages and Codex's own
   responses)."

Do not try to run `setup.py` — it is interactive (reads `stdin`) and
does not work when Claude invokes it as a subprocess. Use
`set_locale.py` (non-interactive, takes `--locale` as a flag) to
persist the choice in `config.local.json`. The next wrapper invocation
already sees the new locale via `detect_locale()`.

Do not use `Read` + `Write` to edit `config.local.json` directly: the
global `CLAUDE.md` rule forbids `Write` on existing files (line
endings, line-by-line diff). `set_locale.py` does the deep-merge
correctly and preserves keys unrelated to the locale.

## Output (common to individual modes)

```
status             "ok" | "error" | "needs_input"
severity           "low" | "medium" | "high"
confidence         "low" | "medium" | "high"
summary            string
findings           [ {severity, category, title, detail, location} ]
block_recommended  bool
fingerprint        16 hex chars
duration_seconds   float
mode               "plan-review" | ...
degraded           bool (optional, set when partial JSON is salvaged)
questions          [ {id, question, context} ] (status=needs_input)
files_*            (delegate)
```

### Presentation to the user

ALWAYS render in markdown (headers, tables, blockquote, bold). NEVER
wrap the presentation in a code fence. ALWAYS translate `summary`,
`title`, `detail`, and `location` to the user's locale (pt-BR by
default; switch when the locale is en-US) — do not keep the original
English alongside.

**Modes `plan-review`, `verify`, `delegate` (status=ok):**

1. Header: `**Codex {mode}**`
2. Meta table:

   | field | value |
   |---|---|
   | duration | {X}s |
   | severity | {S} |
   | confidence | {C} |
   | findings | {N} |

3. Summary as a blockquote: `> {summary}`
4. Findings as a numbered list:

   ```
   1. **[{severity}] {title}** — `{location}`
      > {detail}
   ```

   If empty: `_No findings._`

**Mode `ask` (status=ok):** lean table without severity, answer in the
blockquote.

**Mode `insight` (status=ok):** labels `must-do` / `should-consider` /
`nice-to-have` — do not use "bug/problem".

**status=needs_input (any mode):**

1. Header: `**Codex {mode}** — open questions`
2. Standard meta table.
3. Summary as a blockquote.
4. Numbered list of `questions` (id, question, context). Then apply the
   `delegate` protocol (2 local passes → escalate if it persists).

**All modes, status=error:** header with `— **ERROR**`, same table,
summary as a blockquote, ask whether to retry.

## Inviolable rules

- **Never** call the `codex` CLI directly.
- **Never** invoke `/codex:*` (the official plugin) from this skill.
- **Never** apply findings as automatic edits — only present them.
- **Before any `plan-review`**, run
  `scripts/analyze_plan_complexity.py`. If the output suggests
  iterative, show the one-line suggestion and ask. Do not skip this
  step. Do not suggest twice for the same plan/turn.
- **Temporary files** (payload.json, task.txt, packet, transcripts)
  **only** in `$env:TEMP`; zero writes inside the target repo.
- For `delegate`, explicit user confirmation with the 5 fields is
  required before invoking the wrapper. Without that confirmation, do
  not invoke.
- Present ALL findings translated to the user's locale before any
  analysis of your own (global "Codex Review Transparency" rule). Do
  not omit, summarise, or group them.
- Default reasoning is controlled by the wrapper per mode. Override
  only via the wrapper's explicit
  `--reasoning-effort {low|medium|high|xhigh}` flag, **and only when
  the user explicitly asked** (e.g. "review at max effort", "run on
  low to be quick"). Do not pass `-c model_reasoning_effort=...`
  directly on the Codex CLI.
- ALWAYS render the presentation as markdown, NEVER inside a code
  fence — except `git status --short` output in `delegate` mode.
