"""Single-entrypoint wrapper for Claude Code -> Codex automation.

Modes (all return canonical JSON on stdout):
  * ``plan-review``    — Codex reviews a plan (read-only).
  * ``verify``         — Codex reviews a diff (read-only).
  * ``ask``            — Codex answers a question (read-only).
  * ``insight``        — Codex retrospects on the session (read-only).
  * ``delegate``       — Codex executes a task (``danger-full-access`` sandbox).

The wrapper never raises: timeouts, missing binary, non-zero exit, malformed
output and internal exceptions all surface as a structured ``status=error``
result on stdout.

Canonical output (JSON, single line on stdout, UTF-8)::

    {
        "status":            "ok" | "error" | "needs_input",
        "severity":          "low" | "medium" | "high",
        "confidence":        "low" | "medium" | "high",
        "summary":           str,
        "findings":          [ ... ],
        "block_recommended": bool,
        "fingerprint":       str,
        "raw_codex_output":  str,
        "mode":              one of the modes above,
        "duration_seconds":  float,
        "coverage":          [ {category, findings_count} ],  (review modes)
        "degraded":          bool,                         (optional)
        "questions":         [ {id, question, context} ],  (status=needs_input)
        "files_created":     [str],                        (delegate only)
        "files_edited":      [str],                        (delegate only)
        "files_deleted":     [str],                        (delegate only)
        "commands_run":      [str],                        (delegate only)
        "tests_run":         [str]                         (delegate only)
    }

Environment overrides:
  * ``CODEX_WRAPPER_TIMEOUT_SECONDS``   — global wall-clock override (per-mode default otherwise).
  * ``CODEX_WRAPPER_IDLE_TIMEOUT_SECONDS`` — kill Codex after this many seconds
    without a single stream event; ``0`` disables the idle guard.
  * ``CODEX_WRAPPER_MODEL``             — model slug; overrides config (``auto`` resolves
    the strongest model advertised by the Codex models cache).
  * ``CODEX_WRAPPER_SERVICE_TIER``      — ``priority`` is the fast tier; ``default`` opts out.
  * ``CODEX_WRAPPER_CODEX_OVERRIDE``    — alternative ``codex`` script (testing).
  * ``CODEX_WRAPPER_DISABLE_HEARTBEAT`` — ``1`` silences the stderr heartbeat.
  * ``CODEX_WRAPPER_USE_JSON_STREAM``   — ``0`` drops ``--json`` (and the idle guard with it).
  * ``CODEX_WRAPPER_TELEMETRY_DISABLED``— ``1`` skips writing ``cache/runs.jsonl``.
  * ``CLAUDE_AUTOMATION_PYTHON`` / ``SKILLS_PYTHON`` — Python interpreter.
  * Credentials propagated to the Codex subprocess are declared in
    ``settings.credentials.propagate`` (config-driven). Keys listed
    there are loaded from ``settings.credentials.source`` (an ordered
    list defaulting to ``./.env`` then ``~/.claude/credentials.env``)
    and injected as env vars. Variables already present in the parent
    process environment are inherited as in any POSIX subprocess,
    independently of ``propagate``.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
SKILL_DIR = BIN_DIR.parent
SCHEMA_FILE = BIN_DIR / "codex_output_schema.json"
COLLECT_SCRIPT = BIN_DIR / "collect_claude_context.py"

# Make sibling helpers importable before the module-level constants run, so
# the config-driven defaults below can call ``_config_get`` directly.
sys.path.insert(0, str(BIN_DIR))
from codex_config import (  # noqa: E402
    ensure_setup_complete,
    t,
)
from codex_config import (
    get as _config_get,
)
from codex_config import (
    resolve_python as _resolve_python,
)

CACHE_DIR = SKILL_DIR / "cache"
TELEMETRY_FILE = CACHE_DIR / "runs.jsonl"
TELEMETRY_BACKUP = CACHE_DIR / "runs.jsonl.1"
TELEMETRY_MAX_BYTES = int(_config_get("wrapper.telemetry_max_bytes", 5 * 1024 * 1024))
TELEMETRY_SCHEMA_VERSION = int(_config_get("wrapper.telemetry_schema_version", 1))

DEFAULT_TIMEOUT_SECONDS = float(_config_get("wrapper.default_timeout_seconds", 300.0))

MODE_TIMEOUTS: dict[str, float] = {
    k: float(v)
    for k, v in _config_get(
        "wrapper.mode_timeouts",
        {
            "ask": 300.0,
            "verify": 600.0,
            "plan-review": 900.0,
            "delegate": 900.0,
            "insight": 1200.0,
        },
    ).items()
}

IDLE_TIMEOUT_SECONDS = float(_config_get("wrapper.idle_timeout_seconds", 180.0))
TOTAL_DEADLINE_MULTIPLIER = float(_config_get("wrapper.total_deadline_multiplier", 1.5))
MIN_RETRY_BUDGET_SECONDS = 60.0

MODE_REASONING: dict[str, str] = dict(
    _config_get(
        "wrapper.mode_reasoning",
        {
            "plan-review": "max",
            "delegate": "max",
            "verify": "high",
            "ask": "medium",
            "insight": "max",
        },
    )
)

VALID_REASONING_EFFORTS = tuple(
    _config_get(
        "wrapper.valid_reasoning_efforts",
        ("low", "medium", "high", "xhigh", "max", "ultra"),
    )
)

MODE_MODEL: dict[str, str] = dict(_config_get("wrapper.mode_model", {}))
CONFIGURED_MODEL = str(_config_get("wrapper.model", "auto"))
MODEL_FALLBACK = str(_config_get("wrapper.model_fallback", "gpt-5.6-sol"))
MODELS_CACHE_PATH = str(_config_get("wrapper.models_cache_path", "~/.codex/models_cache.json"))
SERVICE_TIER = str(_config_get("wrapper.service_tier", "priority"))

REVIEW_MODES = ("plan-review", "verify", "ask", "insight")
ALL_MODES = ("plan-review", "verify", "delegate", "ask", "insight")
COVERAGE_MODES = ("plan-review", "verify")

HEARTBEAT_INTERVAL = float(_config_get("wrapper.heartbeat_interval_seconds", 15.0))
HEARTBEAT_JITTER = float(_config_get("wrapper.heartbeat_jitter_seconds", 2.0))

RETRY_INSTRUCTION = (
    "Your previous reply was not parseable as JSON. Reply ONLY with a single "
    "JSON object that matches the schema. No prose, no markdown fences."
)

from normalize_codex_result import normalize  # noqa: E402

# --------------------------------------------------------------------------- IO


def _emit_json(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    try:
        sys.stdout.buffer.write(text.encode("utf-8"))
    except AttributeError:
        sys.stdout.write(text)


def _read_text_safely(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return ""


def _read_json_safely(path: Path | None) -> dict[str, Any]:
    text = _read_text_safely(path)
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _codex_child_env() -> dict[str, str]:
    env = os.environ.copy()
    propagate = _config_get("credentials.propagate", []) or []
    if not propagate:
        return env

    raw_sources = _config_get("credentials.source", "~/.claude/credentials.env")
    if isinstance(raw_sources, str):
        raw_sources = [raw_sources]
    elif not isinstance(raw_sources, list):
        return env

    file_kv: dict[str, str] = {}
    for raw in raw_sources:
        if not isinstance(raw, str):
            continue
        path_str = os.path.expanduser(raw)
        path = Path(path_str)
        if not path.is_absolute():
            path = SKILL_DIR / path_str
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            if key and key not in file_kv:  # first source wins
                file_kv[key] = value
    for name in propagate:
        if not isinstance(name, str):
            continue
        if env.get(name):  # already set in parent env, don't overwrite
            continue
        if name in file_kv:
            env[name] = file_kv[name]
    return env


# ------------------------------------------------------------------- CONTEXT


def _collect_context(cwd: Path, target_path: Path | None) -> str:
    cmd = [_resolve_python(), str(COLLECT_SCRIPT), "--cwd", str(cwd), "--format", "text"]
    if target_path is not None:
        cmd += ["--target-path", str(target_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


# ------------------------------------------------------------------- PROMPTS

EXHAUSTIVE_DIRECTIVE = (
    "Be exhaustive. Report EVERY issue you find. There is no maximum number of findings — "
    "do not cap the list, do not return only the most important ones, do not rank-limit, and "
    "do not merge several distinct issues into a single finding. Conversely, do not split one "
    "issue into several findings to inflate the count, and do not invent problems to fill a "
    "category. Sweep every checklist category below one by one, and record each one in "
    "`coverage` with how many findings it produced (0 is a valid and expected count).\n\n"
)

PLAN_REVIEW_CATEGORIES = (
    ("assumptions", "flawed or unstated assumptions the plan depends on"),
    ("edge-cases", "missed edge cases, failure modes and concurrency hazards"),
    ("risky-operations", "risky or irreversible operations: data loss, migrations, deploys, deletions"),
    ("project-conventions", "contradictions with the project's own conventions in the CLAUDE.md context"),
    ("ordering", "ordering and dependencies between steps; work that cannot run in the stated sequence"),
    ("testing", "testing gaps against the plan's own stated acceptance criteria"),
    ("rollback", "rollback, recovery and what happens if the change must be undone"),
    ("security", "security, authorization, tenancy and data exposure"),
    ("performance", "performance and behaviour at scale"),
    ("observability", "observability and operability: logs, metrics, diagnosing it in production"),
    ("scope", "scope beyond what was asked, or requested scope silently dropped"),
)

VERIFY_CATEGORIES = (
    ("correctness", "logic errors and regressions introduced by the diff"),
    ("edge-cases", "unhandled edge cases, error paths and concurrency hazards"),
    ("project-conventions", "contradictions with the project's own conventions in the CLAUDE.md context"),
    ("testing", "missing or vacuous tests for the behaviour that changed"),
    ("security", "security, authorization, tenancy and data exposure"),
    ("performance", "performance and behaviour at scale"),
    ("error-handling", "swallowed errors, silent failures and missing validation"),
    ("observability", "observability and operability of the new code"),
    ("dead-code", "leftovers: dead code, stray debug output, unused imports, TODOs"),
    ("scope", "changes beyond what the turn was supposed to do, or stated work not actually done"),
)


def _checklist_block(categories: tuple[tuple[str, str], ...]) -> str:
    lines = "\n".join(f"- `{name}`: {description}" for name, description in categories)
    return f"Checklist categories (sweep each one):\n{lines}\n"


def _build_prompt(
    mode: str,
    context_text: str,
    user_payload: str,
    transcript_text: str = "",
    transcript_jsonl_path: str = "",
    extra_instruction: str = "",
) -> str:
    header = (
        "You are Codex reviewing work produced by Claude Code. "
        "You MUST reply with a JSON object that conforms to the schema "
        "specified via --output-schema, and nothing else. No prose around it.\n\n"
        "Conventions:\n"
        "- `severity` reflects the worst finding; if no findings, use 'low'.\n"
        "- `confidence` is your own confidence in the review (not the code).\n"
        "- `block_recommended` must be false unless severity=high AND confidence=high.\n"
        "- `findings` must be an array; empty when nothing actionable.\n"
        "- `summary` is one paragraph, plain text, no markdown headings.\n"
        "- If you genuinely cannot proceed without more info, set "
        '`status="needs_input"` and provide concrete `questions` '
        "(each as {id, question, context}).\n"
    )
    if mode in COVERAGE_MODES:
        header += (
            "- `coverage` is one entry per checklist category, each as "
            "{category, findings_count}, including the categories that produced none.\n"
        )
    else:
        header += "- `coverage` does not apply to this mode; return it as an empty array.\n"

    language_directive = t("prompt.language_directive")
    if language_directive and language_directive != "prompt.language_directive":
        header += "\n" + language_directive + "\n"

    if mode == "plan-review":
        body = (
            "Task: Review Claude's plan below.\n\n"
            + EXHAUSTIVE_DIRECTIVE
            + _checklist_block(PLAN_REVIEW_CATEGORIES)
            + "\n--- Claude's plan ---\n"
            + user_payload
        )
    elif mode == "verify":
        body = (
            "Task: Review Claude's implementation turn (already applied to the filesystem). The payload below "
            "includes the last assistant message, git status/diffs, and changed files reconstructed from the "
            "transcript.\n\n"
            + EXHAUSTIVE_DIRECTIVE
            + _checklist_block(VERIFY_CATEGORIES)
            + "\n--- Implementation turn payload (JSON) ---\n"
            + user_payload
        )
    elif mode == "ask":
        body = (
            "Task: Answer the user's question / opinion request below. You may use the CLAUDE.md context as "
            "project background. Place your entire answer in the `summary` field (plain text, one or more "
            "sentences). Keep `findings` empty unless you spot a concrete issue worth flagging.\n\n"
            "--- Question ---\n" + user_payload
        )
    elif mode == "insight":
        body = (
            "Task: Holistic retrospective of the current Claude Code session. Do NOT hunt for bugs or regressions. "
            "Focus on strategic insight: (a) what was accomplished, (b) what was attempted but left incomplete, "
            "(c) gaps and blind spots, (d) prioritized next steps, (e) approach improvements.\n"
            "Use `summary` for the overall retrospective (1-3 paragraphs). Use `findings` as individual "
            "suggestions/observations — each finding is NOT a bug, it is an insight (severity reflects "
            "priority: high=must-do, medium=should-consider, low=nice-to-have).\n\n"
            "--- Optional focus from the user (may be empty) ---\n" + user_payload
        )
    else:  # delegate
        body = (
            "Task: Perform the following work, using the CLAUDE.md context as canonical project instructions. "
            "Write code if needed. You are running with --sandbox danger-full-access; you MAY create, edit, "
            "and delete files anywhere on disk. Keep changes scoped to what the task asks.\n\n"
            "Respond with a JSON object containing at minimum: status, summary, findings (may be empty), "
            "files_created (array of paths), files_edited, files_deleted, commands_run, tests_run.\n\n"
            "--- Task ---\n" + user_payload
        )

    pieces = [header]
    if context_text.strip():
        pieces.append("--- CLAUDE.md context ---\n" + context_text)
    if transcript_text.strip():
        pieces.append("--- Recent Claude Code conversation ---\n" + transcript_text)
    if transcript_jsonl_path.strip():
        pieces.append(
            "--- Full session log (best-effort reference) ---\n"
            f"  {transcript_jsonl_path}\n"
            "Sub-process reads of this file may be blocked by the Windows sandbox; rely on the inline "
            "transcript above as the primary source."
        )
    pieces.append(body)
    if extra_instruction:
        pieces.append("--- Additional instruction ---\n" + extra_instruction)
    return "\n\n".join(pieces)


# -------------------------------------------------------------- CODEX COMMAND


def _codex_base_cmd() -> list[str]:
    override = os.environ.get("CODEX_WRAPPER_CODEX_OVERRIDE")
    if override:
        if not Path(override).exists():
            return []
        return [_resolve_python(), override, "exec"]
    resolved = shutil.which("codex")
    if resolved is None:
        return []
    return [resolved, "exec"]


def _strongest_model_from_cache() -> str | None:
    """Pick the strongest model Codex currently advertises.

    Reads the CLI's own models cache and returns the lowest ``priority``
    entry that is user-selectable, callable through the API and not flagged
    for migration. The cache format is internal to Codex and has already
    changed between releases, so every failure path is silent — the caller
    falls back to the configured slug.
    """
    try:
        raw = Path(os.path.expanduser(MODELS_CACHE_PATH)).read_text(encoding="utf-8")
        models = json.loads(raw).get("models")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(models, list):
        return None

    best_slug: str | None = None
    best_priority: float = float("inf")
    for entry in models:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        if entry.get("visibility") != "list" or entry.get("supported_in_api") is not True:
            continue
        if entry.get("upgrade"):
            continue
        raw_priority = entry.get("priority")
        if not isinstance(raw_priority, (int, float)) or isinstance(raw_priority, bool):
            continue
        priority = float(raw_priority)
        if priority < best_priority:
            best_priority = priority
            best_slug = slug
    return best_slug


def _resolve_model(mode: str) -> str:
    env_model = os.environ.get("CODEX_WRAPPER_MODEL", "").strip()
    if env_model:
        return env_model
    mode_model = str(MODE_MODEL.get(mode, "")).strip()
    if mode_model:
        return mode_model
    if CONFIGURED_MODEL and CONFIGURED_MODEL != "auto":
        return CONFIGURED_MODEL
    return _strongest_model_from_cache() or MODEL_FALLBACK


def _resolve_service_tier() -> str:
    env_tier = os.environ.get("CODEX_WRAPPER_SERVICE_TIER", "").strip()
    return env_tier or SERVICE_TIER


def _json_stream_enabled() -> bool:
    return os.environ.get("CODEX_WRAPPER_USE_JSON_STREAM", "1") != "0"


def _build_codex_command(
    mode: str,
    cwd: Path,
    output_file: Path,
    effort_override: str | None = None,
    with_service_tier: bool = True,
) -> list[str]:
    base = _codex_base_cmd()
    if not base:
        return []
    if mode == "delegate":
        sandbox = "danger-full-access"
    else:
        sandbox = "read-only"
    cmd = list(base)
    cmd += [
        "--sandbox",
        sandbox,
        "--skip-git-repo-check",
        "--ephemeral",
        "--color",
        "never",
        "-C",
        str(cwd),
        "-o",
        str(output_file),
    ]
    model = _resolve_model(mode)
    if model:
        cmd += ["-m", model]
    effort = effort_override or MODE_REASONING.get(mode)
    if effort:
        cmd += ["-c", f"model_reasoning_effort={effort}"]
    tier = _resolve_service_tier()
    if with_service_tier and tier:
        cmd += ["-c", f"service_tier={tier}"]
    if mode in REVIEW_MODES and SCHEMA_FILE.exists():
        cmd += ["--output-schema", str(SCHEMA_FILE)]
    if _json_stream_enabled():
        cmd.append("--json")
    cmd.append("-")
    return cmd


# ---------------------------------------------------------------- PROGRESS


class _Progress:
    """Liveness state fed by the Codex ``--json`` event stream.

    A Codex run can spend minutes reasoning without writing a single byte to
    the output file, so file size alone cannot tell "still thinking" from
    "hung". Every JSONL event the CLI emits refreshes ``last_event_at``,
    which turns the wall-clock timeout into a fallback and makes silence
    itself the thing we measure.

    Only surface tags are kept (event type, counter). The reasoning text
    that rides along in the stream is never stored nor printed.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_event_at = time.monotonic()
        self.event_count = 0
        self.last_event_type = "none"
        self.last_agent_message = ""

    def record(self, event_type: str, agent_message: str | None = None) -> None:
        with self._lock:
            self._last_event_at = time.monotonic()
            self.event_count += 1
            self.last_event_type = event_type or "unknown"
            if agent_message:
                self.last_agent_message = agent_message

    def touch(self) -> None:
        with self._lock:
            self._last_event_at = time.monotonic()

    def idle_seconds(self) -> float:
        with self._lock:
            return max(0.0, time.monotonic() - self._last_event_at)


# --------------------------------------------------------------- HEARTBEAT


class _Heartbeat:
    """Background thread that writes progress lines to stderr.

    The heartbeat exists so Claude can see that a long Codex run is still
    making progress (the real Claude Code session captures stderr from Bash
    tool calls). We never expose the model's chain-of-thought — only safe
    surface signals: phase, elapsed time, packet size, last event tag.
    """

    def __init__(self, mode: str, started_at: float, output_path: Path) -> None:
        self.mode = mode
        self.started_at = started_at
        self.output_path = output_path
        self.phase = "starting"
        self.progress: _Progress | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._enabled = os.environ.get("CODEX_WRAPPER_DISABLE_HEARTBEAT") != "1"

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def start(self) -> None:
        if not self._enabled:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _packet_bytes(self) -> int:
        try:
            return self.output_path.stat().st_size
        except OSError:
            return 0

    def _resolve_interval(self) -> float:
        raw = os.environ.get("CODEX_WRAPPER_HEARTBEAT_INTERVAL_SECONDS")
        if raw:
            try:
                return max(0.1, float(raw))
            except ValueError:
                pass
        return HEARTBEAT_INTERVAL

    def _loop(self) -> None:
        base_interval = self._resolve_interval()
        jitter = min(HEARTBEAT_JITTER, base_interval / 2)
        while not self._stop.is_set():
            interval = base_interval + random.uniform(-jitter, jitter)
            if self._stop.wait(timeout=interval):
                return
            elapsed = time.monotonic() - self.started_at
            line = (
                f"[codex-heartbeat] mode={self.mode} phase={self.phase} "
                f"elapsed={elapsed:.0f}s packet_bytes={self._packet_bytes()}"
            )
            progress = self.progress
            if progress is not None:
                line += (
                    f" idle={progress.idle_seconds():.0f}s "
                    f"events={progress.event_count} last={progress.last_event_type}"
                )
            try:
                print(line, file=sys.stderr, flush=True)
            except OSError:
                return


# --------------------------------------------------------------- ERROR RESULTS


def _empty_result(mode: str) -> dict[str, Any]:
    return {
        "status": "ok",
        "severity": "low",
        "confidence": "low",
        "summary": "",
        "findings": [],
        "block_recommended": False,
        "fingerprint": "",
        "raw_codex_output": "",
        "mode": mode,
        "duration_seconds": 0.0,
    }


def _generic_error(mode: str, started_at: float, summary: str) -> dict[str, Any]:
    result = _empty_result(mode)
    result["status"] = "error"
    result["confidence"] = "high"
    result["summary"] = summary
    result["duration_seconds"] = max(0.0, time.monotonic() - started_at)
    return result


def _kill_process_tree(pid: int) -> None:
    """Kill the whole subprocess tree on Windows (taskkill /T) so node
    grandchildren launched by codex.cmd do not linger."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


# --------------------------------------------------------------- INVOCATION

MAX_CAPTURED_STDOUT_BYTES = 2 * 1024 * 1024
MAX_CAPTURED_STDERR_LINES = 50
SERVICE_TIER_ERROR_MARKERS = ("service_tier", "service tier")


def _resolve_idle_timeout() -> float:
    raw = os.environ.get("CODEX_WRAPPER_IDLE_TIMEOUT_SECONDS")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return IDLE_TIMEOUT_SECONDS


def _classify_stream_event(line: str) -> tuple[str | None, str | None]:
    """Map one ``--json`` line to (event tag, agent message).

    Returns ``(None, None)`` for anything that is not a Codex event object,
    so plain output still counts as liveness without being mistaken for a
    structured event.
    """
    try:
        data = json.loads(line)
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    event_type = data.get("type")
    if not isinstance(event_type, str) or not event_type:
        return None, None

    label = event_type
    message: str | None = None
    item = data.get("item")
    if isinstance(item, dict):
        item_type = item.get("type")
        if isinstance(item_type, str) and item_type:
            label = item_type
        if item_type == "agent_message":
            text_value = item.get("text")
            if isinstance(text_value, str) and text_value.strip():
                message = text_value
    return label, message


def _pump_stdout(stream: Any, progress: _Progress, chunks: list[str]) -> None:
    captured = 0
    try:
        for line in stream:
            event_type, message = _classify_stream_event(line.strip())
            progress.record(event_type or "output", message)
            if captured < MAX_CAPTURED_STDOUT_BYTES:
                chunks.append(line)
                captured += len(line)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _pump_stderr(stream: Any, lines: deque[str]) -> None:
    try:
        for line in stream:
            lines.append(line)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _feed_stdin(stream: Any, prompt: str) -> None:
    try:
        stream.write(prompt)
        stream.flush()
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _terminate(proc: subprocess.Popen) -> None:
    _kill_process_tree(proc.pid)
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _supervise(
    proc: subprocess.Popen,
    progress: _Progress,
    heartbeat: _Heartbeat,
    timeout: float,
    idle_limit: float,
) -> str | None:
    """Wait for Codex, watching both the wall clock and the event stream.

    Returns None on a natural exit, or the reason the process was killed:
    ``timeout`` (wall clock) or ``idle`` (no stream event for too long).
    """
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        now = time.monotonic()
        if now >= deadline:
            heartbeat.set_phase("timeout")
            _terminate(proc)
            return "timeout"
        if idle_limit > 0 and progress.idle_seconds() > idle_limit:
            heartbeat.set_phase("idle-timeout")
            _terminate(proc)
            return "idle"
        time.sleep(0.25)
    return None


def _run_codex_once(
    mode: str,
    prompt: str,
    cwd: Path,
    timeout: float,
    heartbeat: _Heartbeat,
    effort_override: str | None = None,
    with_service_tier: bool = True,
) -> tuple[str, int, str | None, str]:
    """Run Codex once. Returns (raw_output, exit_code, error_summary, stderr_tail).

    ``error_summary`` is None on a clean run (regardless of Codex content);
    populated only when the subprocess could not run, timed out, went silent
    past the idle limit, or exited with a non-zero status. ``raw_output`` is
    the captured payload (output file, then the stream fallback) — may be
    empty.

    The run is supervised rather than simply awaited: stdout is consumed as
    it arrives so a stalled Codex is killed by silence long before the
    wall-clock ceiling, and so the heartbeat can report what it is doing.
    """
    with tempfile.NamedTemporaryFile(
        prefix="codex-out-", suffix=".txt", delete=False, mode="w", encoding="utf-8"
    ) as tmp:
        out_path = Path(tmp.name)

    try:
        heartbeat.output_path = out_path
        cmd = _build_codex_command(
            mode,
            cwd,
            out_path,
            effort_override=effort_override,
            with_service_tier=with_service_tier,
        )
        if not cmd:
            return "", -1, t("wrapper.error.codex_unavailable"), ""

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=_codex_child_env(),
            )
        except (FileNotFoundError, OSError) as exc:
            return "", -1, t("wrapper.error.process_start", exc=exc.__class__.__name__), ""

        progress = _Progress()
        heartbeat.progress = progress
        heartbeat.set_phase("running")

        stdout_chunks: list[str] = []
        stderr_lines: deque[str] = deque(maxlen=MAX_CAPTURED_STDERR_LINES)
        workers = [
            threading.Thread(target=_feed_stdin, args=(proc.stdin, prompt), daemon=True),
            threading.Thread(target=_pump_stdout, args=(proc.stdout, progress, stdout_chunks), daemon=True),
            threading.Thread(target=_pump_stderr, args=(proc.stderr, stderr_lines), daemon=True),
        ]
        for worker in workers:
            worker.start()

        idle_limit = _resolve_idle_timeout() if _json_stream_enabled() else 0.0
        started_at = time.monotonic()
        kill_reason = _supervise(proc, progress, heartbeat, timeout, idle_limit)
        for worker in workers:
            worker.join(timeout=5)
        stderr_tail = "".join(stderr_lines)

        if kill_reason == "timeout":
            return "", -1, t("wrapper.error.timeout", timeout=timeout), stderr_tail
        if kill_reason == "idle":
            return (
                "",
                -1,
                t(
                    "wrapper.error.idle_timeout",
                    idle=progress.idle_seconds(),
                    limit=idle_limit,
                    elapsed=time.monotonic() - started_at,
                ),
                stderr_tail,
            )

        raw_output = ""
        try:
            if out_path.exists():
                raw_output = out_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw_output = ""
        if not raw_output:
            raw_output = progress.last_agent_message or "".join(stdout_chunks)

        if proc.returncode != 0:
            return (
                raw_output,
                proc.returncode,
                t(
                    "wrapper.error.nonzero",
                    code=proc.returncode,
                ),
                stderr_tail,
            )
        heartbeat.set_phase("parsing")
        return raw_output, proc.returncode, None, stderr_tail
    finally:
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass


def _looks_like_service_tier_error(stderr_tail: str) -> bool:
    lowered = stderr_tail.lower()
    return any(marker in lowered for marker in SERVICE_TIER_ERROR_MARKERS)


def _run_codex(
    mode: str,
    prompt: str,
    cwd: Path,
    timeout: float,
    heartbeat: _Heartbeat,
    effort_override: str | None = None,
) -> tuple[str, int, str | None]:
    """Run Codex, retrying once without the fast service tier if it is refused.

    An account that loses access to the priority tier would otherwise fail
    every single review; the extra attempt only ever happens on failure.
    """
    raw, exit_code, err, stderr_tail = _run_codex_once(
        mode,
        prompt,
        cwd,
        timeout,
        heartbeat,
        effort_override=effort_override,
    )
    tier_refused = err is not None and exit_code != 0 and _looks_like_service_tier_error(stderr_tail)
    if tier_refused and _resolve_service_tier():
        heartbeat.set_phase("retrying-without-service-tier")
        raw, exit_code, err, _ = _run_codex_once(
            mode,
            prompt,
            cwd,
            timeout,
            heartbeat,
            effort_override=effort_override,
            with_service_tier=False,
        )
    return raw, exit_code, err


def _wants_retry(mode: str, normalized: dict[str, Any]) -> bool:
    """True when we should ask Codex to retry with a stricter instruction.

    Only review modes — delegate is allowed to return prose. We retry when
    the normalizer reported an error because no parseable JSON object was
    found in the raw output.
    """
    if mode not in REVIEW_MODES:
        return False
    if normalized.get("status") != "error":
        return False
    return normalized.get("error_class") == "not_structured_json"


def _best_effort_partial(mode: str, raw: str) -> dict[str, Any] | None:
    """Try to salvage a half-written JSON object. Returns None if nothing
    usable can be extracted."""
    if not raw or not raw.strip():
        return None
    snippet = raw.strip()
    open_idx = snippet.find("{")
    if open_idx < 0:
        return None
    snippet = snippet[open_idx:]
    open_count = 0
    close_count = 0
    in_string = False
    escape = False
    for ch in snippet:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            open_count += 1
        elif ch == "}":
            close_count += 1
    if open_count <= close_count:
        return None
    repaired = snippet
    if in_string:
        repaired += '"'
    repaired += "}" * (open_count - close_count)
    try:
        parsed = json.loads(repaired)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    summary = parsed.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    salvaged = normalize(json.dumps(parsed, ensure_ascii=False), mode=mode)
    salvaged["degraded"] = True
    return salvaged


def _enrich_delegate_fields(normalized: dict[str, Any], raw_data: dict[str, Any]) -> None:
    """Pass through the optional delegate-specific fields when present."""
    for key in ("files_created", "files_edited", "files_deleted", "commands_run", "tests_run"):
        value = raw_data.get(key)
        if isinstance(value, list):
            normalized[key] = [str(item) for item in value if isinstance(item, (str, int, float))]


def _enrich_needs_input(normalized: dict[str, Any], raw_data: dict[str, Any]) -> None:
    questions = raw_data.get("questions")
    if not isinstance(questions, list):
        return
    cleaned: list[dict[str, str]] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        question_text = q.get("question")
        if not isinstance(question_text, str) or not question_text.strip():
            continue
        cleaned.append(
            {
                "id": str(q.get("id") or f"q{len(cleaned) + 1}"),
                "question": question_text,
                "context": str(q.get("context") or ""),
            }
        )
    if cleaned:
        normalized["status"] = "needs_input"
        normalized["questions"] = cleaned


def _try_extract_raw_dict(raw: str) -> dict[str, Any]:
    """Look for a parseable JSON object inside raw output. Returns {} on miss."""
    if not raw or not raw.strip():
        return {}
    candidates: list[str] = []
    stripped = raw.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    fenced_start = raw.find("```json")
    if fenced_start >= 0:
        end = raw.find("```", fenced_start + 7)
        if end > fenced_start:
            candidates.append(raw[fenced_start + 7 : end].strip())
    open_idx = raw.find("{")
    close_idx = raw.rfind("}")
    if 0 <= open_idx < close_idx:
        candidates.append(raw[open_idx : close_idx + 1])
    for c in candidates:
        try:
            data = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def _invoke_codex(
    mode: str,
    prompt: str,
    cwd: Path,
    timeout: float,
    heartbeat: _Heartbeat,
    effort_override: str | None = None,
) -> tuple[dict[str, Any], int, int]:
    """Run Codex with one optional retry on JSON parse failure.

    Both attempts share a single deadline (``timeout`` times the configured
    multiplier), so a retry can never silently double the wall-clock cost of
    a run.

    Returns ``(normalized_result, retry_count, last_exit_code)``.
    """
    started_at = time.monotonic()
    hard_deadline = started_at + timeout * max(1.0, TOTAL_DEADLINE_MULTIPLIER)
    raw, exit_code, err = _run_codex(
        mode,
        prompt,
        cwd,
        timeout,
        heartbeat,
        effort_override=effort_override,
    )
    retry_count = 0

    if err is not None:
        return _generic_error(mode, started_at, err), retry_count, exit_code

    normalized = normalize(raw, mode=mode)
    raw_data = _try_extract_raw_dict(raw)
    if raw_data:
        _enrich_delegate_fields(normalized, raw_data)
        _enrich_needs_input(normalized, raw_data)

    remaining = hard_deadline - time.monotonic()
    retry_budget = min(MIN_RETRY_BUDGET_SECONDS, timeout)
    if _wants_retry(mode, normalized) and remaining >= retry_budget:
        heartbeat.set_phase("retrying")
        retry_prompt = prompt + "\n\n--- Retry instruction ---\n" + RETRY_INSTRUCTION
        raw2, exit_code2, err2 = _run_codex(
            mode,
            retry_prompt,
            cwd,
            min(timeout, remaining),
            heartbeat,
            effort_override=effort_override,
        )
        retry_count = 1
        if err2 is None:
            normalized2 = normalize(raw2, mode=mode)
            raw_data2 = _try_extract_raw_dict(raw2)
            if raw_data2:
                _enrich_delegate_fields(normalized2, raw_data2)
                _enrich_needs_input(normalized2, raw_data2)
            if normalized2.get("status") != "error":
                normalized = normalized2
                exit_code = exit_code2
            else:
                salvaged = _best_effort_partial(mode, raw2) or _best_effort_partial(mode, raw)
                if salvaged is not None:
                    normalized = salvaged
                    exit_code = exit_code2
                else:
                    normalized = normalized2
                    exit_code = exit_code2

    return normalized, retry_count, exit_code


# --------------------------------------------------------------- TIMEOUT


def _resolve_timeout(mode: str) -> float:
    raw = os.environ.get("CODEX_WRAPPER_TIMEOUT_SECONDS")
    if raw:
        try:
            value = float(raw)
            return max(1.0, value)
        except ValueError:
            pass
    return MODE_TIMEOUTS.get(mode, DEFAULT_TIMEOUT_SECONDS)


# --------------------------------------------------------------- TELEMETRY


def _write_telemetry(entry: dict[str, Any]) -> None:
    if os.environ.get("CODEX_WRAPPER_TELEMETRY_DISABLED") == "1":
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if TELEMETRY_FILE.exists() and TELEMETRY_FILE.stat().st_size >= TELEMETRY_MAX_BYTES:
            try:
                if TELEMETRY_BACKUP.exists():
                    TELEMETRY_BACKUP.unlink()
                TELEMETRY_FILE.rename(TELEMETRY_BACKUP)
            except OSError:
                pass
        with TELEMETRY_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------- CLI


def _parse_cli(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=t("wrapper.cli.description"))
    parser.add_argument("mode", choices=list(ALL_MODES), help=t("wrapper.cli.help.mode"))
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--target-path", default=None)
    parser.add_argument("--last-message-file", default=None)
    parser.add_argument("--payload-file", default=None)
    parser.add_argument("--task-file", default=None)
    parser.add_argument("--question-file", default=None)
    parser.add_argument("--focus-file", default=None)
    parser.add_argument("--review-packet-file", default=None, help=t("wrapper.cli.help.review_packet_file"))
    parser.add_argument("--transcript-file", default=None)
    parser.add_argument("--transcript-jsonl-path", default=None)
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        help=t("wrapper.cli.help.reasoning_effort"),
    )
    return parser.parse_args(argv)


def _resolve_effort_override(raw_value: str | None) -> str | None:
    """Validate the user-provided effort. Returns None if not set or invalid.

    Invalid values emit a warning to stderr and fall back to the per-mode
    default (preserving the wrapper's "never raise" contract).
    """
    if raw_value is None:
        return None
    candidate = str(raw_value).strip().lower()
    if candidate in VALID_REASONING_EFFORTS:
        return candidate
    print(
        t(
            "wrapper.warning.invalid_effort",
            raw_value=repr(raw_value),
            valid=", ".join(VALID_REASONING_EFFORTS),
        ),
        file=sys.stderr,
        flush=True,
    )
    return None


def _maybe_build_review_packet(args: argparse.Namespace) -> str | None:
    """When plan-review is invoked with only --last-message-file, materialize a
    review packet on the fly so Codex always gets the structured bundle."""
    if args.mode != "plan-review":
        return None
    if args.review_packet_file:
        return None
    if not args.last_message_file:
        return None
    plan_path = Path(args.last_message_file)
    if not plan_path.is_file():
        return None
    try:
        from build_review_packet import _build_packet  # type: ignore
    except Exception:
        return None
    plan_text = _read_text_safely(plan_path)
    if not plan_text.strip():
        return None
    cwd = Path(args.cwd).resolve()
    target_path = Path(args.target_path).resolve() if args.target_path else None
    transcript_text = ""
    if args.transcript_file:
        transcript_text = _read_text_safely(Path(args.transcript_file))
    try:
        body, _manifest = _build_packet(
            plan_text=plan_text,
            cwd=cwd,
            target_path=target_path,
            transcript_text=transcript_text,
            max_files=12,
            max_lines=300,
            max_bytes=120 * 1024,
        )
    except Exception:
        return None
    return body


def _assemble_user_payload(args: argparse.Namespace) -> str:
    if args.mode == "plan-review":
        if args.review_packet_file:
            text = _read_text_safely(Path(args.review_packet_file))
            if text:
                return text
        auto_packet = _maybe_build_review_packet(args)
        if auto_packet:
            return auto_packet
        if args.last_message_file:
            return _read_text_safely(Path(args.last_message_file))
        return ""
    if args.mode == "verify":
        payload = _read_json_safely(Path(args.payload_file) if args.payload_file else None)
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if args.mode == "ask":
        return _read_text_safely(Path(args.question_file) if args.question_file else None)
    if args.mode == "insight":
        return _read_text_safely(Path(args.focus_file) if args.focus_file else None)
    return _read_text_safely(Path(args.task_file) if args.task_file else None)


def main(argv: list[str] | None = None) -> int:
    args = _parse_cli(argv)
    # All wrapper modes (ask, verify, plan-review, delegate, insight) are
    # active — none are passive. Setup wizard is auto-triggered (only in
    # TTY) before any Codex invocation when locale is unset.
    ensure_setup_complete()
    started_at = time.monotonic()
    started_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_id = uuid.uuid4().hex[:12]
    retry_count = 0
    exit_code = 0
    error_class = ""
    cwd_str = args.cwd

    heartbeat = _Heartbeat(args.mode, started_at, Path(tempfile.gettempdir()))
    heartbeat.start()

    try:
        cwd = Path(args.cwd)
        cwd_str = str(cwd)
        target_path = Path(args.target_path) if args.target_path else None
        user_payload = _assemble_user_payload(args)
        heartbeat.set_phase("collecting-context")
        context_text = _collect_context(cwd, target_path)
        transcript_text = _read_text_safely(Path(args.transcript_file) if args.transcript_file else None)
        transcript_jsonl_path = args.transcript_jsonl_path or ""
        prompt = _build_prompt(args.mode, context_text, user_payload, transcript_text, transcript_jsonl_path)
        timeout = _resolve_timeout(args.mode)
        effort_override = _resolve_effort_override(getattr(args, "reasoning_effort", None))
        heartbeat.set_phase("invoking-codex")
        result, retry_count, exit_code = _invoke_codex(
            args.mode,
            prompt,
            cwd,
            timeout,
            heartbeat,
            effort_override=effort_override,
        )
    except Exception as exc:  # wrapper must never raise
        error_class = exc.__class__.__name__
        result = _generic_error(args.mode, started_at, t("wrapper.error.internal", exc=error_class))
    finally:
        heartbeat.stop()

    duration = max(0.0, time.monotonic() - started_at)
    result["duration_seconds"] = duration

    _write_telemetry(
        {
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "timestamp": started_iso,
            "run_id": run_id,
            "mode": args.mode,
            "cwd": cwd_str,
            "duration_ms": int(duration * 1000),
            "status": result.get("status", "error"),
            "packet_bytes": len(result.get("raw_codex_output") or ""),
            "retry_count": retry_count,
            "error_class": error_class,
            "exit_code": exit_code,
        }
    )

    _emit_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
