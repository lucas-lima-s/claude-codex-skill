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
        "degraded":          bool,                         (optional)
        "questions":         [ {id, question, context} ],  (status=needs_input)
        "files_created":     [str],                        (delegate only)
        "files_edited":      [str],                        (delegate only)
        "files_deleted":     [str],                        (delegate only)
        "commands_run":      [str],                        (delegate only)
        "tests_run":         [str]                         (delegate only)
    }

Environment overrides:
  * ``CODEX_WRAPPER_TIMEOUT_SECONDS``   — global override (per-mode default otherwise).
  * ``CODEX_WRAPPER_CODEX_OVERRIDE``    — alternative ``codex`` script (testing).
  * ``CODEX_WRAPPER_DISABLE_HEARTBEAT`` — ``1`` silences the stderr heartbeat.
  * ``CODEX_WRAPPER_USE_JSON_STREAM``   — ``1`` adds ``--json`` (experimental).
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

DEFAULT_TIMEOUT_SECONDS = float(_config_get("wrapper.default_timeout_seconds", 120.0))

MODE_TIMEOUTS: dict[str, float] = {
    k: float(v)
    for k, v in _config_get(
        "wrapper.mode_timeouts",
        {
            "ask": 120.0,
            "verify": 180.0,
            "plan-review": 300.0,
            "delegate": 300.0,
            "insight": 420.0,
        },
    ).items()
}

MODE_REASONING: dict[str, str] = dict(
    _config_get(
        "wrapper.mode_reasoning",
        {
            "plan-review": "xhigh",
            "delegate": "xhigh",
            "verify": "medium",
            "ask": "medium",
            "insight": "xhigh",
        },
    )
)

VALID_REASONING_EFFORTS = tuple(
    _config_get(
        "wrapper.valid_reasoning_efforts",
        ("low", "medium", "high", "xhigh"),
    )
)

REVIEW_MODES = ("plan-review", "verify", "ask", "insight")
ALL_MODES = ("plan-review", "verify", "delegate", "ask", "insight")

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

    language_directive = t("prompt.language_directive")
    if language_directive and language_directive != "prompt.language_directive":
        header += "\n" + language_directive + "\n"

    if mode == "plan-review":
        body = (
            "Task: Review Claude's plan below. Look for flawed assumptions, missed edge cases, "
            "risky operations, and contradictions with the CLAUDE.md context.\n\n"
            "--- Claude's plan ---\n" + user_payload
        )
    elif mode == "verify":
        body = (
            "Task: Review Claude's implementation turn (already applied to the filesystem) for regressions, "
            "missing tests, anti-patterns, and contradictions with the CLAUDE.md context. The payload below "
            "includes the last assistant message, git status/diffs, and changed files reconstructed from the "
            "transcript.\n\n"
            "--- Implementation turn payload (JSON) ---\n" + user_payload
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


def _build_codex_command(
    mode: str,
    cwd: Path,
    output_file: Path,
    effort_override: str | None = None,
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
    effort = effort_override or MODE_REASONING.get(mode)
    if effort:
        cmd += ["-c", f"model_reasoning_effort={effort}"]
    if mode in REVIEW_MODES and SCHEMA_FILE.exists():
        cmd += ["--output-schema", str(SCHEMA_FILE)]
    if os.environ.get("CODEX_WRAPPER_USE_JSON_STREAM") == "1":
        cmd.append("--json")
    cmd.append("-")
    return cmd


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

    def _loop(self) -> None:
        while not self._stop.is_set():
            interval = HEARTBEAT_INTERVAL + random.uniform(-HEARTBEAT_JITTER, HEARTBEAT_JITTER)
            if self._stop.wait(timeout=interval):
                return
            elapsed = time.monotonic() - self.started_at
            line = (
                f"[codex-heartbeat] mode={self.mode} phase={self.phase} "
                f"elapsed={elapsed:.0f}s packet_bytes={self._packet_bytes()}"
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


def _run_codex_once(
    mode: str,
    prompt: str,
    cwd: Path,
    timeout: float,
    heartbeat: _Heartbeat,
    effort_override: str | None = None,
) -> tuple[str, int, str | None]:
    """Run Codex once. Returns (raw_output, exit_code, error_summary).

    ``error_summary`` is None on a clean run (regardless of Codex content);
    populated only when the subprocess could not run, timed out, or exited
    with a non-zero status. ``raw_output`` is the captured payload (file +
    stdout fallback) — may be empty.
    """
    with tempfile.NamedTemporaryFile(
        prefix="codex-out-", suffix=".txt", delete=False, mode="w", encoding="utf-8"
    ) as tmp:
        out_path = Path(tmp.name)

    try:
        heartbeat.output_path = out_path
        cmd = _build_codex_command(mode, cwd, out_path, effort_override=effort_override)
        if not cmd:
            return "", -1, t("wrapper.error.codex_unavailable")

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_codex_child_env(),
            )
        except (FileNotFoundError, OSError) as exc:
            return "", -1, t("wrapper.error.process_start", exc=exc.__class__.__name__)

        heartbeat.set_phase("running")
        try:
            stdout, _stderr = proc.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            heartbeat.set_phase("timeout")
            _kill_process_tree(proc.pid)
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return "", -1, t("wrapper.error.timeout", timeout=timeout)
        except OSError as exc:
            return "", -1, t("wrapper.error.io", exc=exc.__class__.__name__)

        raw_output = ""
        try:
            if out_path.exists():
                raw_output = out_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw_output = ""
        if not raw_output:
            raw_output = stdout or ""

        if proc.returncode != 0:
            return (
                raw_output,
                proc.returncode,
                t(
                    "wrapper.error.nonzero",
                    code=proc.returncode,
                ),
            )
        heartbeat.set_phase("parsing")
        return raw_output, proc.returncode, None
    finally:
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass


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

    Returns ``(normalized_result, retry_count, last_exit_code)``.
    """
    started_at = time.monotonic()
    raw, exit_code, err = _run_codex_once(
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

    if _wants_retry(mode, normalized):
        heartbeat.set_phase("retrying")
        retry_prompt = prompt + "\n\n--- Retry instruction ---\n" + RETRY_INSTRUCTION
        raw2, exit_code2, err2 = _run_codex_once(
            mode,
            retry_prompt,
            cwd,
            timeout,
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
