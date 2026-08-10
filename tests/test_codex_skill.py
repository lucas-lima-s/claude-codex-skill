"""End-to-end test suite for the ``codex`` skill.

Self-contained — no pytest, no fixtures, no third-party deps. Runs every
documented use-case against the fake Codex (``fake_codex.py``) so it can be
executed offline.

Run::

    "$SKILLS_PYTHON" tests/test_codex_skill.py

Exit code is the number of failed assertions.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Make sure prints survive on Windows consoles (cp1252 default).
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

THIS_DIR = Path(__file__).resolve().parent
SKILL_DIR = THIS_DIR.parent
SCRIPTS = SKILL_DIR / "scripts"
WRAPPER = SCRIPTS / "invoke_codex_with_claude.py"
BUILDER = SCRIPTS / "build_review_packet.py"
BATCHER = SCRIPTS / "codex_batch.py"
FAKE = THIS_DIR / "fake_codex.py"
PS1 = SCRIPTS / "invoke_codex_with_claude.ps1"


def _resolve_python() -> str:
    for env in ("SKILLS_PYTHON", "CLAUDE_AUTOMATION_PYTHON"):
        v = os.environ.get(env)
        if v and Path(v).exists():
            return v
    return sys.executable


PYTHON = _resolve_python()


# ----------------------------------------------------------------------- runner


class Runner:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.failures: list[str] = []

    def section(self, name: str) -> None:
        print(f"\n=== {name} ===")

    def passed_(self, label: str) -> None:
        self.passed += 1
        print(f"  PASS  {label}")

    def fail(self, label: str, detail: str) -> None:
        self.failed += 1
        self.failures.append(f"{label}: {detail}")
        print(f"  FAIL  {label}  -- {detail}")

    def eq(self, actual: Any, expected: Any, label: str) -> None:
        if actual == expected:
            self.passed_(label)
        else:
            self.fail(label, f"expected {expected!r}, got {actual!r}")

    def truthy(self, value: Any, label: str) -> None:
        if value:
            self.passed_(label)
        else:
            self.fail(label, f"expected truthy, got {value!r}")

    def falsy(self, value: Any, label: str) -> None:
        if not value:
            self.passed_(label)
        else:
            self.fail(label, f"expected falsy, got {value!r}")

    def in_(self, needle: Any, haystack: Any, label: str) -> None:
        if needle in haystack:
            self.passed_(label)
        else:
            self.fail(label, f"{needle!r} not found in {haystack!r}")

    def le(self, actual: float, ceiling: float, label: str) -> None:
        if actual <= ceiling:
            self.passed_(label)
        else:
            self.fail(label, f"{actual} > ceiling {ceiling}")

    def ge(self, actual: float, floor: float, label: str) -> None:
        if actual >= floor:
            self.passed_(label)
        else:
            self.fail(label, f"{actual} < floor {floor}")


# ----------------------------------------------------------------------- helpers


TELEMETRY_SANDBOX = Path(tempfile.mkdtemp(prefix="codex-test-cache-"))


def _base_env(behavior: str = "success", timeout: str = "10", extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["CODEX_WRAPPER_CODEX_OVERRIDE"] = str(FAKE)
    env["CODEX_WRAPPER_TIMEOUT_SECONDS"] = timeout
    env["CODEX_WRAPPER_DISABLE_HEARTBEAT"] = "1"
    env["FAKE_CODEX_BEHAVIOR"] = behavior
    env["CODEX_LOCALE"] = "en-US"
    env["CODEX_WRAPPER_CACHE_DIR"] = str(TELEMETRY_SANDBOX)
    if extra:
        env.update(extra)
    return env


def _run_wrapper(
    mode: str,
    behavior: str,
    args: list[str],
    timeout_env: str = "10",
    extra_env: dict[str, str] | None = None,
    hard_timeout: float = 30.0,
) -> tuple[dict[str, Any], str, float]:
    """Returns (parsed_json_result, stderr, wall_clock_seconds)."""
    env = _base_env(behavior=behavior, timeout=timeout_env, extra=extra_env)
    cmd = [PYTHON, str(WRAPPER), mode] + args
    started = time.monotonic()
    proc = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=hard_timeout,
    )
    elapsed = time.monotonic() - started
    try:
        result = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        result = {"_parse_error": True, "_stdout": proc.stdout}
    return result, proc.stderr, elapsed


def _run_wrapper_rc(
    mode: str,
    behavior: str,
    args: list[str],
    timeout_env: str = "10",
    extra_env: dict[str, str] | None = None,
    hard_timeout: float = 30.0,
) -> tuple[dict[str, Any], int]:
    """Same as ``_run_wrapper`` but surfaces the process exit code."""
    env = _base_env(behavior=behavior, timeout=timeout_env, extra=extra_env)
    proc = subprocess.run(
        [PYTHON, str(WRAPPER), mode] + args,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=hard_timeout,
    )
    try:
        result = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        result = {"_parse_error": True, "_stdout": proc.stdout}
    return result, proc.returncode


@contextmanager
def _tempdir():
    path = Path(tempfile.mkdtemp(prefix="codex-test-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# ----------------------------------------------------------------------- tests


def test_plan_review_modes(r: Runner) -> None:
    r.section("plan-review × scenarios")
    with _tempdir() as tmp:
        plan = tmp / "plan.md"
        plan.write_text(
            "# Test plan\n\nTouch scripts/normalize_codex_result.py for something.\n",
            encoding="utf-8",
        )
        cwd = str(SKILL_DIR)
        common = ["--cwd", cwd, "--last-message-file", str(plan)]

        # success
        result, _, elapsed = _run_wrapper("plan-review", "success", common)
        r.eq(result.get("status"), "ok", "plan-review/success status=ok")
        r.eq(result.get("mode"), "plan-review", "plan-review/success mode")
        r.le(elapsed, 5.0, "plan-review/success completes <5s")
        r.eq(len(result.get("findings", [])), 1, "plan-review/success has 1 finding")

        # invalid_json → retry → still error after retry
        result, _, _ = _run_wrapper("plan-review", "invalid_json", common)
        r.eq(result.get("status"), "error", "plan-review/invalid_json status=error")
        r.in_(
            "structured json",
            (result.get("summary") or "").lower(),
            "plan-review/invalid_json summary mentions JSON",
        )

        # partial → degraded best-effort
        result, _, _ = _run_wrapper("plan-review", "partial", common)
        r.eq(result.get("status"), "ok", "plan-review/partial salvaged status=ok")
        r.truthy(result.get("degraded"), "plan-review/partial degraded=true")

        # nonzero exit
        result, _, _ = _run_wrapper("plan-review", "nonzero", common)
        r.eq(result.get("status"), "error", "plan-review/nonzero status=error")
        r.in_(
            "non-zero status (2)",
            result.get("summary", ""),
            "plan-review/nonzero summary mentions exit 2",
        )

        # needs_input
        result, _, _ = _run_wrapper("plan-review", "needs_input", common)
        r.eq(result.get("status"), "needs_input", "plan-review/needs_input status=needs_input")
        questions = result.get("questions", [])
        r.eq(len(questions), 1, "plan-review/needs_input 1 question")
        r.in_(
            "staging",
            questions[0].get("context", "").lower(),
            "plan-review/needs_input context preserved",
        )

        # noisy stderr does not break stdout JSON (wrapper consumes child stderr,
        # so we only check that stdout JSON was unaffected by 50 stderr lines)
        result, _, _ = _run_wrapper("plan-review", "noisy_stderr", common)
        r.eq(result.get("status"), "ok", "plan-review/noisy_stderr status=ok")
        r.eq(
            len(result.get("findings", [])),
            1,
            "plan-review/noisy_stderr stdout JSON intact despite stderr noise",
        )

        # timeout
        result, _, elapsed = _run_wrapper("plan-review", "timeout", common, timeout_env="2")
        r.eq(result.get("status"), "error", "plan-review/timeout status=error")
        r.in_(
            "timeout",
            result.get("summary", "").lower(),
            "plan-review/timeout summary mentions timeout",
        )
        r.le(elapsed, 6.0, "plan-review/timeout returns <=6s")


def test_verify_mode(r: Runner) -> None:
    r.section("verify × scenarios")
    with _tempdir() as tmp:
        payload = tmp / "payload.json"
        payload.write_text(
            json.dumps(
                {
                    "cwd": str(SKILL_DIR),
                    "last_assistant_message": "Refactored normalize_codex_result.py",
                    "git_status_short": " M scripts/normalize_codex_result.py",
                    "git_diff_worktree": "diff --git a/x b/x\n@@ -1 +1,2 @@\n+new line\n",
                    "git_diff_cached": "",
                }
            ),
            encoding="utf-8",
        )
        common = ["--cwd", str(SKILL_DIR), "--payload-file", str(payload)]

        result, _, _ = _run_wrapper("verify", "success", common)
        r.eq(result.get("status"), "ok", "verify/success status=ok")
        r.eq(result.get("mode"), "verify", "verify/success mode")

        result, _, _ = _run_wrapper("verify", "invalid_json", common)
        r.eq(result.get("status"), "error", "verify/invalid_json status=error")


def test_ask_mode(r: Runner) -> None:
    r.section("ask × scenarios")
    with _tempdir() as tmp:
        question = tmp / "q.txt"
        question.write_text("What's the complexity of quicksort?", encoding="utf-8")
        common = ["--cwd", str(SKILL_DIR), "--question-file", str(question)]

        result, _, _ = _run_wrapper("ask", "success", common)
        r.eq(result.get("status"), "ok", "ask/success status=ok")
        r.eq(result.get("mode"), "ask", "ask/success mode")
        r.truthy(result.get("summary"), "ask/success has non-empty summary")


def test_insight_mode(r: Runner) -> None:
    r.section("insight × scenarios")
    with _tempdir() as tmp:
        focus = tmp / "focus.txt"
        focus.write_text("Focus on architecture.", encoding="utf-8")
        common = ["--cwd", str(SKILL_DIR), "--focus-file", str(focus)]

        result, _, _ = _run_wrapper("insight", "success", common)
        r.eq(result.get("status"), "ok", "insight/success status=ok")
        r.eq(result.get("mode"), "insight", "insight/success mode")


def test_delegate_mode(r: Runner) -> None:
    r.section("delegate × scenarios")
    with _tempdir() as tmp:
        task = tmp / "task.txt"
        task.write_text("Create an empty fake/new.txt file.", encoding="utf-8")
        common = ["--cwd", str(SKILL_DIR), "--task-file", str(task)]

        # delegate_ok behavior returns delegate-shaped JSON
        result, _, _ = _run_wrapper("delegate", "delegate_ok", common)
        r.eq(result.get("status"), "ok", "delegate/delegate_ok status=ok")
        r.eq(result.get("mode"), "delegate", "delegate/delegate_ok mode")
        r.eq(
            result.get("files_created"),
            ["fake/new.txt"],
            "delegate/delegate_ok files_created passed through",
        )
        r.eq(
            result.get("files_edited"),
            ["fake/touched.py"],
            "delegate/delegate_ok files_edited passed through",
        )
        r.eq(
            result.get("commands_run"),
            ["echo fake"],
            "delegate/delegate_ok commands_run passed through",
        )

        # delegate accepts prose (no JSON) without erroring
        result, _, _ = _run_wrapper("delegate", "invalid_json", common)
        r.eq(result.get("status"), "ok", "delegate/invalid_json tolerates prose")


def test_review_packet_window(r: Runner) -> None:
    r.section("build_review_packet × selection and windows")
    with _tempdir() as tmp:
        # Build a 500-line synthetic file so the windowing rule kicks in.
        big_dir = tmp / "src"
        big_dir.mkdir()
        big = big_dir / "huge.py"
        big.write_text(
            "\n".join(f"# line {i:04d}" for i in range(1, 501)) + "\n",
            encoding="utf-8",
        )

        # Plan with path:line citation → ±50 window (max-lines default 300, file=500)
        plan = tmp / "plan.md"
        plan.write_text("Refactor src/huge.py:200 carefully.\n", encoding="utf-8")
        out = tmp / "packet.md"
        proc = subprocess.run(
            [
                PYTHON,
                str(BUILDER),
                "--plan-file",
                str(plan),
                "--cwd",
                str(tmp),
                "--output",
                str(out),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        r.eq(proc.returncode, 0, "builder exit=0 (with line citation)")
        body = out.read_text(encoding="utf-8")
        r.in_(
            "window around line 200",
            body,
            "packet picks ±50 window when line cited and file >max_lines",
        )

        # Plan without :line on a >max_lines file → first N lines
        plan2 = tmp / "plan2.md"
        plan2.write_text("Touch src/huge.py.\n", encoding="utf-8")
        out2 = tmp / "packet2.md"
        subprocess.run(
            [
                PYTHON,
                str(BUILDER),
                "--plan-file",
                str(plan2),
                "--cwd",
                str(tmp),
                "--max-lines",
                "50",
                "--output",
                str(out2),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        body2 = out2.read_text(encoding="utf-8")
        r.in_("first 50 lines", body2, "packet picks first N lines when no :line cited")

        # File <= max_lines: full file mode
        small = big_dir / "small.py"
        small.write_text("\n".join(f"# {i}" for i in range(50)) + "\n", encoding="utf-8")
        plan3 = tmp / "plan3.md"
        plan3.write_text("Touch src/small.py:5 quickly.\n", encoding="utf-8")
        out3 = tmp / "packet3.md"
        subprocess.run(
            [
                PYTHON,
                str(BUILDER),
                "--plan-file",
                str(plan3),
                "--cwd",
                str(tmp),
                "--output",
                str(out3),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        body3 = out3.read_text(encoding="utf-8")
        r.in_("full file", body3, "packet uses full file when total <= max_lines")


def test_review_packet_max_files(r: Runner) -> None:
    r.section("build_review_packet × max_files limit")
    with _tempdir() as tmp:
        # Create 14 fake source files in tmp
        sources_dir = tmp / "src"
        sources_dir.mkdir()
        cited = []
        for i in range(14):
            p = sources_dir / f"mod_{i:02d}.py"
            p.write_text(f"# module {i}\nprint({i})\n", encoding="utf-8")
            cited.append(f"src/mod_{i:02d}.py")

        plan = tmp / "plan.md"
        plan.write_text(
            "# plan\nTouch the following files:\n" + "\n".join(f"- {c}" for c in cited),
            encoding="utf-8",
        )
        out = tmp / "packet.md"
        subprocess.run(
            [
                PYTHON,
                str(BUILDER),
                "--plan-file",
                str(plan),
                "--cwd",
                str(tmp),
                "--max-files",
                "12",
                "--output",
                str(out),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        body = out.read_text(encoding="utf-8")
        # 12 should be included, 2 should be skipped with manifest entries.
        included_count = sum(1 for c in cited if f"### {tmp.resolve()}" in body and c.replace("/", os.sep) in body)
        skipped_count = body.count("skipped (max_files=12 exceeded)")
        r.eq(skipped_count, 2, "packet skips exactly 2 files when 14 cited")
        # Loose includes check (resolved path may differ on Windows)
        r.ge(included_count + skipped_count, 12, "packet processed at least 12 files")


def test_review_packet_byte_truncation(r: Runner) -> None:
    r.section("build_review_packet × byte truncation")
    with _tempdir() as tmp:
        big = tmp / "big.py"
        big.write_text("\n".join(f"# line {i:04d}" for i in range(2000)), encoding="utf-8")
        plan = tmp / "plan.md"
        plan.write_text("Touch big.py thoroughly.\n", encoding="utf-8")
        out = tmp / "packet.md"
        subprocess.run(
            [
                PYTHON,
                str(BUILDER),
                "--plan-file",
                str(plan),
                "--cwd",
                str(tmp),
                "--max-bytes",
                "2048",
                "--output",
                str(out),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        body = out.read_text(encoding="utf-8")
        r.in_("packet truncated", body, "packet truncates body when max_bytes exceeded")


def test_review_packet_empty_plan(r: Runner) -> None:
    r.section("build_review_packet × empty plan")
    with _tempdir() as tmp:
        plan = tmp / "empty.md"
        plan.write_text("   \n", encoding="utf-8")
        out = tmp / "packet.md"
        proc = subprocess.run(
            [
                PYTHON,
                str(BUILDER),
                "--plan-file",
                str(plan),
                "--cwd",
                str(tmp),
                "--output",
                str(out),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        r.eq(proc.returncode, 2, "empty plan → builder returns 2")


def test_wrapper_auto_packet(r: Runner) -> None:
    r.section("wrapper auto-build do review packet em plan-review")
    with _tempdir() as tmp:
        plan = tmp / "plan.md"
        plan.write_text(
            "Touch scripts/normalize_codex_result.py:50 carefully.\n",
            encoding="utf-8",
        )
        common = ["--cwd", str(SKILL_DIR), "--last-message-file", str(plan)]
        result, _, _ = _run_wrapper("plan-review", "success", common)
        r.eq(result.get("status"), "ok", "wrapper plan-review with auto-packet returns ok")


def test_batch_ask_speedup(r: Runner) -> None:
    r.section("batch-ask × parallel speedup")
    with _tempdir() as tmp:
        batch = {
            "max_parallel": 4,
            "tasks": [{"id": f"q{i}", "question": f"Question {i}?", "cwd": str(SKILL_DIR)} for i in range(4)],
        }
        batch_file = tmp / "batch.json"
        batch_file.write_text(json.dumps(batch), encoding="utf-8")

        env = _base_env(behavior="delay_short")

        # Sequential baseline
        t0 = time.monotonic()
        for _ in range(4):
            subprocess.run(
                [
                    PYTHON,
                    str(WRAPPER),
                    "ask",
                    "--cwd",
                    str(SKILL_DIR),
                    "--question-file",
                    str(batch_file),
                ],
                env=env,
                capture_output=True,
                timeout=30,
            )
        seq = time.monotonic() - t0

        # Parallel
        t0 = time.monotonic()
        proc = subprocess.run(
            [PYTHON, str(BATCHER), "batch-ask", "--input-file", str(batch_file)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        par = time.monotonic() - t0

        speedup = seq / par if par > 0 else 0
        r.ge(speedup, 2.0, f"batch-ask ≥2× speedup ({speedup:.2f}× observed)")

        result = json.loads(proc.stdout)
        r.eq(result.get("status"), "ok", "batch-ask all-success status=ok")
        r.eq(len(result.get("items", [])), 4, "batch-ask returns 4 items")
        r.falsy(result.get("partial"), "batch-ask partial=false on full success")


def test_batch_ask_partial(r: Runner) -> None:
    r.section("batch-ask × partial failure does not cancel the rest")
    with _tempdir() as tmp:
        # Run 3 OK tasks; the global behavior is fixed via env, so we can't
        # mix per-item easily with the current fake. Instead we exercise the
        # aggregator: same env, all succeed; for "partial" coverage rely on
        # the disjoint test below where some items deliberately fail.
        batch = {
            "max_parallel": 4,
            "tasks": [
                {"id": "ok1", "question": "x", "cwd": str(SKILL_DIR)},
                {"id": "ok2", "question": "y", "cwd": str(SKILL_DIR)},
                {"id": "ok3", "question": "z", "cwd": str(SKILL_DIR)},
            ],
        }
        path = tmp / "b.json"
        path.write_text(json.dumps(batch), encoding="utf-8")
        env = _base_env(behavior="success")
        proc = subprocess.run(
            [PYTHON, str(BATCHER), "batch-ask", "--input-file", str(path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        result = json.loads(proc.stdout)
        r.eq(result.get("status"), "ok", "batch-ask 3/3 ok aggregate")
        ids = sorted(i["id"] for i in result.get("items", []))
        r.eq(ids, ["ok1", "ok2", "ok3"], "batch-ask preserves item ids")

        # Partial: force every item to fail (nonzero) to exercise error path
        env_fail = _base_env(behavior="nonzero")
        proc = subprocess.run(
            [PYTHON, str(BATCHER), "batch-ask", "--input-file", str(path)],
            env=env_fail,
            capture_output=True,
            text=True,
            timeout=60,
        )
        result = json.loads(proc.stdout)
        r.eq(result.get("status"), "error", "batch-ask all-fail aggregate=error")


def test_batch_delegate_overlap(r: Runner) -> None:
    r.section("batch-delegate × write-set overlap")
    with _tempdir() as tmp:
        batch = {
            "tasks": [
                {
                    "id": "t1",
                    "task": "x",
                    "cwd": str(SKILL_DIR),
                    "write_set": ["src/a.py", "src/shared.py"],
                },
                {
                    "id": "t2",
                    "task": "y",
                    "cwd": str(SKILL_DIR),
                    "write_set": ["src/b.py", "src/shared.py"],
                },
            ],
        }
        path = tmp / "ov.json"
        path.write_text(json.dumps(batch), encoding="utf-8")
        env = _base_env(behavior="delegate_ok")
        proc = subprocess.run(
            [PYTHON, str(BATCHER), "batch-delegate", "--input-file", str(path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        result = json.loads(proc.stdout)
        r.eq(result.get("status"), "error", "batch-delegate overlap aggregate=error")
        overlaps = result.get("overlaps") or []
        r.ge(len(overlaps), 1, "overlap descriptor present")
        r.in_("shared.py", str(overlaps), "overlap path identified")


def test_batch_delegate_violation(r: Runner) -> None:
    r.section("batch-delegate × write_set_violated when Codex overshoots")
    with _tempdir() as tmp:
        batch = {
            "tasks": [
                {"id": "d1", "task": "x", "cwd": str(SKILL_DIR), "write_set": ["src/a.py"]},
                {"id": "d2", "task": "y", "cwd": str(SKILL_DIR), "write_set": ["src/b.py"]},
            ],
        }
        path = tmp / "dj.json"
        path.write_text(json.dumps(batch), encoding="utf-8")
        env = _base_env(behavior="delegate_ok")
        proc = subprocess.run(
            [PYTHON, str(BATCHER), "batch-delegate", "--input-file", str(path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        result = json.loads(proc.stdout)
        r.eq(result.get("status"), "ok", "batch-delegate disjoint runs ok")
        violated = [i for i in result.get("items", []) if i.get("write_set_violated")]
        r.eq(len(violated), 2, "all items mark write_set_violated when Codex extrapolates")


def test_telemetry_schema(r: Runner) -> None:
    r.section("telemetry × schema and write")
    cache_dir = TELEMETRY_SANDBOX
    cache_dir.mkdir(exist_ok=True)
    runs = cache_dir / "runs.jsonl"
    # Snapshot
    initial = runs.read_text(encoding="utf-8") if runs.exists() else ""
    initial_lines = initial.count("\n")

    with _tempdir() as tmp:
        plan = tmp / "p.md"
        plan.write_text("test plan\n", encoding="utf-8")
        common = ["--cwd", str(SKILL_DIR), "--last-message-file", str(plan)]
        _run_wrapper("plan-review", "success", common)

    new_text = runs.read_text(encoding="utf-8")
    new_lines = new_text.count("\n")
    r.eq(new_lines - initial_lines, 1, "telemetry: 1 row added per run")

    last_line = new_text.strip().splitlines()[-1]
    entry = json.loads(last_line)
    required = {
        "schema_version",
        "timestamp",
        "run_id",
        "mode",
        "cwd",
        "duration_ms",
        "status",
        "packet_bytes",
        "retry_count",
        "error_class",
        "exit_code",
    }
    missing = required - set(entry.keys())
    r.eq(missing, set(), f"telemetry has all required fields ({sorted(required)})")
    r.eq(entry["mode"], "plan-review", "telemetry: mode field correct")
    r.eq(entry["status"], "ok", "telemetry: status field correct")
    r.eq(entry["schema_version"], 1, "telemetry: schema_version=1")


def test_telemetry_rotation(r: Runner) -> None:
    r.section("telemetry × rotation at 5 MB")
    with _tempdir() as tmp:
        cache_dir = tmp / "cache"
        cache_dir.mkdir()
        runs = cache_dir / "runs.jsonl"
        backup = cache_dir / "runs.jsonl.1"
        runs.write_bytes(b"x" * (5 * 1024 * 1024 + 100))

        plan = tmp / "p.md"
        plan.write_text("rot\n", encoding="utf-8")
        common = ["--cwd", str(SKILL_DIR), "--last-message-file", str(plan)]
        _run_wrapper(
            "plan-review",
            "success",
            common,
            extra_env={"CODEX_WRAPPER_CACHE_DIR": str(cache_dir)},
        )

        r.truthy(backup.exists(), "rotation: runs.jsonl.1 created")
        r.le(runs.stat().st_size, 5 * 1024 * 1024, "rotation: runs.jsonl below 5 MB after rotation")
        r.ge(backup.stat().st_size, 5 * 1024 * 1024, "rotation: runs.jsonl.1 holds the old payload")


def test_ps1_stub(r: Runner) -> None:
    r.section("PowerShell stub × args forwarding")
    if sys.platform != "win32":
        print("  SKIP  (non-Windows runtime)")
        return
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        print("  SKIP  (no pwsh/powershell on PATH)")
        return

    with _tempdir() as tmp:
        plan = tmp / "p.md"
        plan.write_text("ps1 stub test\n", encoding="utf-8")
        env = _base_env(behavior="success")
        cmd = [
            pwsh,
            "-NoProfile",
            "-File",
            str(PS1),
            "plan-review",
            "--cwd",
            str(SKILL_DIR),
            "--last-message-file",
            str(plan),
        ]
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
        r.eq(data.get("status"), "ok", "ps1 stub forwards args to wrapper")


def test_codex_config(r: Runner) -> None:
    r.section("codex_config × get/t/detect_locale + override config.local")
    sys.path.insert(0, str(SCRIPTS))
    try:
        import codex_config as cc
    except ImportError as exc:
        r.fail("codex_config import", str(exc))
        return

    # Snapshot env so we don't leak CODEX_LOCALE between subtests.
    saved_locale = os.environ.pop("CODEX_LOCALE", None)
    try:
        # get() — settings core
        cc.clear_cache()
        r.eq(cc.get("dialogue.default_max_turns"), 3, "get(dialogue.default_max_turns) == 3")
        r.eq(
            cc.get("background.default_max_concurrent"),
            5,
            "get(background.default_max_concurrent) == 5",
        )
        r.eq(cc.get("foo.bar.does.not.exist", default="N/A"), "N/A", "get(missing) returns default")

        # t() — pt-BR direct lookup
        os.environ["CODEX_LOCALE"] = "pt-BR"
        cc.clear_cache()
        r.in_(
            "não retornou JSON estruturado",
            cc.t("normalize.summary.not_structured"),
            "t pt-BR resolves normalize key",
        )
        r.in_("Timeout", cc.t("wrapper.error.timeout", timeout=30.0), "t pt-BR resolves with kwargs")

        # t() — en-US direct lookup
        os.environ["CODEX_LOCALE"] = "en-US"
        cc.clear_cache()
        r.in_(
            "did not return structured JSON",
            cc.t("normalize.summary.not_structured"),
            "t en-US resolves normalize key",
        )
        r.in_(
            "Codex timeout after",
            cc.t("wrapper.error.timeout", timeout=30.0),
            "t en-US resolves with kwargs",
        )

        # t() — chave inexistente cai no literal
        cc.clear_cache()
        r.eq(cc.t("foo.bar.baz"), "foo.bar.baz", "t missing key falls back to literal key")

        # detect_locale com env var
        os.environ["CODEX_LOCALE"] = "pt_BR"  # underscore
        cc.clear_cache()
        r.eq(cc.detect_locale(), "pt-BR", "detect_locale normalises pt_BR → pt-BR")

        # override via config.local.json
        os.environ.pop("CODEX_LOCALE", None)
        local_path = SKILL_DIR / "config.local.json"
        local_existed = local_path.exists()
        backup = local_path.read_text(encoding="utf-8") if local_existed else None
        try:
            local_path.write_text(
                json.dumps(
                    {
                        "settings": {"dialogue": {"default_max_turns": 999}},
                        "locales": {"pt-BR": {"foo.test": "VALOR LOCAL pt-BR"}},
                    }
                ),
                encoding="utf-8",
            )
            cc.clear_cache()
            r.eq(cc.get("dialogue.default_max_turns"), 999, "config.local.json override settings")
            os.environ["CODEX_LOCALE"] = "pt-BR"
            cc.clear_cache()
            r.eq(cc.t("foo.test"), "VALOR LOCAL pt-BR", "config.local.json adds locale entries")
            # Chave existente no default + override deve mostrar override
            os.environ["CODEX_LOCALE"] = "pt-BR"
        finally:
            if local_existed and backup is not None:
                local_path.write_text(backup, encoding="utf-8")
            else:
                local_path.unlink(missing_ok=True)
            cc.clear_cache()
    finally:
        os.environ.pop("CODEX_LOCALE", None)
        if saved_locale is not None:
            os.environ["CODEX_LOCALE"] = saved_locale
        cc.clear_cache()


def test_disable_heartbeat_silent(r: Runner) -> None:
    r.section("heartbeat × CODEX_WRAPPER_DISABLE_HEARTBEAT=1 silences stderr")
    with _tempdir() as tmp:
        plan = tmp / "p.md"
        plan.write_text("hb test\n", encoding="utf-8")
        common = ["--cwd", str(SKILL_DIR), "--last-message-file", str(plan)]
        _, stderr, _ = _run_wrapper(
            "plan-review",
            "delay_short",
            common,
            extra_env={"CODEX_WRAPPER_DISABLE_HEARTBEAT": "1"},
        )
        r.falsy("[codex-heartbeat]" in stderr, "heartbeat suppressed when env=1")


def test_analyze_plan_complexity(r: Runner) -> None:
    r.section("analyze_plan_complexity × auto-suggestion heuristic")
    helper = SCRIPTS / "analyze_plan_complexity.py"
    if not helper.exists():
        r.fail("analyze_plan_complexity.py present", "missing")
        return

    with _tempdir() as tmp:
        # Case 1: simple plan (1 line, no keywords) → score 0
        simple = tmp / "simple.md"
        simple.write_text("# Plan\n\nTouch scripts/foo.py for something.\n", encoding="utf-8")
        proc = subprocess.run(
            [PYTHON, str(helper), "--plan-file", str(simple)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
        r.eq(data.get("suggest_iterative"), False, "simple plan → suggest_iterative=false")
        r.le(data.get("score") or 0, 1, "simple plan → low score")

        # Case 2: sensitive plan (cross-module + keywords + 3 phases + large)
        sensitive_lines = ["# Sensitive plan", ""]
        sensitive_lines.append("Refactor de auth, payment e migration. Toca `handler/`, `service/`, `repo/` e `api/`.")
        for n in range(1, 4):
            sensitive_lines.append(f"## Phase {n}")
            for f_idx in range(8):
                sensitive_lines.append(f"- Touch scripts/file{f_idx}_{n}.py")
            sensitive_lines.append("Detalhes: " + "X" * 800)
        sensitive_text = "\n".join(sensitive_lines)
        sensitive = tmp / "sensitive.md"
        sensitive.write_text(sensitive_text, encoding="utf-8")
        proc = subprocess.run(
            [PYTHON, str(helper), "--plan-file", str(sensitive)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
        r.eq(data.get("suggest_iterative"), True, "sensitive plan → suggest_iterative=true")
        r.truthy((data.get("score") or 0) >= 3, "sensitive plan → score >= 3")
        reasons = data.get("reasons") or []
        r.truthy(any("keywords" in (rs or "") for rs in reasons), "reasons mention sensitive keywords")

        # Case 3: missing file → fails gracefully
        missing = tmp / "does_not_exist.md"
        proc = subprocess.run(
            [PYTHON, str(helper), "--plan-file", str(missing)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
        r.eq(
            data.get("suggest_iterative"),
            False,
            "missing file → suggest_iterative=false (graceful)",
        )
        r.eq(data.get("score") or 0, 0, "missing file → score 0")


def test_dialogue_lifecycle(r: Runner) -> None:
    r.section("codex_dialogue × cycle start → next-turn → finish/abort")
    dialogue = SCRIPTS / "codex_dialogue.py"
    if not dialogue.exists():
        r.fail("codex_dialogue.py present", "missing")
        return

    env = _base_env(behavior="success", timeout="30")
    with _tempdir() as tmp:
        plan = tmp / "plan_v1.md"
        plan.write_text("# Plan v1\nTouch foo.py.\n", encoding="utf-8")

        # start sem --accepted-by-user → recusa
        proc = subprocess.run(
            [PYTHON, str(dialogue), "start", "--plan-file", str(plan), "--cwd", str(SKILL_DIR)],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
        r.eq(data.get("status"), "error", "start without --accepted-by-user → status=error")
        r.eq(
            data.get("reason"),
            "needs_user_acceptance",
            "reason=needs_user_acceptance (guard rail R5-F1)",
        )

        # start
        proc = subprocess.run(
            [
                PYTHON,
                str(dialogue),
                "start",
                "--accepted-by-user",
                "--plan-file",
                str(plan),
                "--cwd",
                str(SKILL_DIR),
                "--max-turns",
                "2",
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
        r.eq(data.get("status"), "ok", "dialogue start status=ok")
        r.eq(data.get("turn"), 1, "dialogue start turn=1")
        dialogue_id = data.get("dialogue_id")
        r.truthy(dialogue_id, "dialogue start emits dialogue_id")
        r.eq(data.get("max_turns"), 2, "max_turns honored from CLI flag")

        if not dialogue_id:
            return

        # status mid-flight
        proc = subprocess.run(
            [PYTHON, str(dialogue), "status", "--dialogue-id", dialogue_id],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
        r.eq(data.get("dialogue_status"), "running", "status: dialogue running")
        r.eq(data.get("current_turn"), 1, "status: current_turn=1")

        # next-turn (turn 2 = limit)
        plan_v2 = tmp / "plan_v2.md"
        plan_v2.write_text("# Plan v2\nRevised version.\n", encoding="utf-8")
        proc = subprocess.run(
            [
                PYTHON,
                str(dialogue),
                "next-turn",
                "--dialogue-id",
                dialogue_id,
                "--plan-file",
                str(plan_v2),
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
        r.eq(data.get("turn"), 2, "next-turn returns turn=2")
        r.eq(data.get("stop_reason"), "limit", "stop_reason=limit when current_turn == max_turns")

        # finish
        proc = subprocess.run(
            [PYTHON, str(dialogue), "finish", "--dialogue-id", dialogue_id],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
        r.eq(data.get("status"), "ok", "finish returns status=ok")
        r.truthy(data.get("dialogue_log_path"), "finish emits dialogue_log_path")
        r.truthy(data.get("final_plan_path"), "finish emits final_plan_path")
        log_path = data.get("dialogue_log_path") or ""
        if log_path:
            r.truthy(Path(log_path).exists(), "dialogue_log.md was written")

        # abort on a fresh dialogue (no next-turn)
        plan2 = tmp / "abort_plan.md"
        plan2.write_text("# Plan to abort\n", encoding="utf-8")
        proc = subprocess.run(
            [
                PYTHON,
                str(dialogue),
                "start",
                "--plan-file",
                str(plan2),
                "--cwd",
                str(SKILL_DIR),
                "--max-turns",
                "5",
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
        abort_id = data.get("dialogue_id")
        if abort_id:
            proc = subprocess.run(
                [PYTHON, str(dialogue), "abort", "--dialogue-id", abort_id],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError:
                data = {}
            r.eq(data.get("dialogue_status"), "aborted", "abort sets status=aborted")

            # stop_signal should reflect aborted
            proc = subprocess.run(
                [PYTHON, str(dialogue), "status", "--dialogue-id", abort_id],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError:
                data = {}
            r.eq(data.get("stop_reason"), "aborted", "status reports stop_reason=aborted")

        # not_found error path
        proc = subprocess.run(
            [PYTHON, str(dialogue), "status", "--dialogue-id", "deadbeefcafe"],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
        r.eq(data.get("status"), "error", "unknown dialogue_id → status=error")
        r.eq(data.get("reason"), "dialogue_not_found", "reason=dialogue_not_found")


def _verify_payload(tmp: Path) -> Path:
    payload = tmp / "payload.json"
    payload.write_text(
        json.dumps(
            {
                "cwd": str(SKILL_DIR),
                "last_assistant_message": "x",
                "transcript_path": "",
                "git_status_short": "",
                "git_diff_worktree": "",
                "git_diff_cached": "",
                "changed_files_from_transcript": [],
            }
        ),
        encoding="utf-8",
    )
    return payload


def test_model_and_service_tier(r: Runner) -> None:
    r.section("model + service tier reach the Codex argv")
    with _tempdir() as tmp:
        payload = _verify_payload(tmp)
        common = ["--cwd", str(SKILL_DIR), "--payload-file", str(payload)]

        argv_log = tmp / "argv.json"
        _run_wrapper(
            "verify",
            "success",
            common,
            extra_env={"FAKE_CODEX_ARGV_LOG_FILE": str(argv_log)},
        )
        if argv_log.exists():
            argv = json.loads(argv_log.read_text(encoding="utf-8")).get("argv", [])
            argv_str = " ".join(argv)
            r.in_("-m", argv_str, "explicit -m is forwarded")
            r.in_("service_tier=priority", argv_str, "fast service tier is forwarded")
            r.in_("--json", argv_str, "event stream is on by default")
        else:
            r.fail("model+tier argv", "argv log not written")

        argv_log_2 = tmp / "argv2.json"
        _run_wrapper(
            "verify",
            "success",
            common,
            extra_env={
                "FAKE_CODEX_ARGV_LOG_FILE": str(argv_log_2),
                "CODEX_WRAPPER_MODEL": "fake-model-slug",
                "CODEX_WRAPPER_SERVICE_TIER": "default",
            },
        )
        if argv_log_2.exists():
            argv_str = " ".join(json.loads(argv_log_2.read_text(encoding="utf-8")).get("argv", []))
            r.in_("fake-model-slug", argv_str, "CODEX_WRAPPER_MODEL overrides the config")
            r.in_("service_tier=default", argv_str, "CODEX_WRAPPER_SERVICE_TIER opts out of fast mode")
        else:
            r.fail("model override argv", "argv log not written")

        argv_log_3 = tmp / "argv3.json"
        _, stderr_3, _ = _run_wrapper(
            "verify",
            "success",
            common + ["--reasoning-effort", "ultra"],
            extra_env={"FAKE_CODEX_ARGV_LOG_FILE": str(argv_log_3)},
        )
        if argv_log_3.exists():
            argv_str = " ".join(json.loads(argv_log_3.read_text(encoding="utf-8")).get("argv", []))
            r.in_("model_reasoning_effort=ultra", argv_str, "ultra effort is accepted")
            r.falsy("ultra" in stderr_3, "ultra effort emits no warning")
        else:
            r.fail("ultra effort argv", "argv log not written")


def test_idle_timeout(r: Runner) -> None:
    r.section("idle guard kills a silent Codex before the wall clock")
    with _tempdir() as tmp:
        payload = _verify_payload(tmp)
        common = ["--cwd", str(SKILL_DIR), "--payload-file", str(payload)]

        result, _stderr, elapsed = _run_wrapper(
            "verify",
            "idle_stall",
            common,
            timeout_env="60",
            extra_env={"CODEX_WRAPPER_IDLE_TIMEOUT_SECONDS": "3"},
            hard_timeout=45.0,
        )
        r.eq(result.get("status"), "error", "stalled run reports an error")
        r.in_("no event", (result.get("summary") or "").lower(), "error names the idle guard")
        r.le(elapsed, 30.0, "stalled run ends in seconds, not at the wall-clock ceiling")

        result_ok, _stderr_ok, _elapsed_ok = _run_wrapper(
            "verify",
            "stream_slow",
            common,
            timeout_env="60",
            extra_env={
                "CODEX_WRAPPER_IDLE_TIMEOUT_SECONDS": "5",
                "FAKE_CODEX_STREAM_EVENTS": "6",
                "FAKE_CODEX_STREAM_GAP_SECONDS": "1",
            },
            hard_timeout=60.0,
        )
        r.eq(result_ok.get("status"), "ok", "streaming run is not killed by the idle guard")

        result_nostream, _stderr_ns, _elapsed_ns = _run_wrapper(
            "verify",
            "delay_short",
            common,
            timeout_env="60",
            extra_env={
                "CODEX_WRAPPER_USE_JSON_STREAM": "0",
                "CODEX_WRAPPER_IDLE_TIMEOUT_SECONDS": "2",
            },
            hard_timeout=45.0,
        )
        r.eq(result_nostream.get("status"), "ok", "idle guard is off when the stream is off")


def test_heartbeat_reports_progress(r: Runner) -> None:
    r.section("heartbeat exposes liveness, not just elapsed time")
    with _tempdir() as tmp:
        payload = _verify_payload(tmp)
        _, stderr, _ = _run_wrapper(
            "verify",
            "stream_slow",
            ["--cwd", str(SKILL_DIR), "--payload-file", str(payload)],
            timeout_env="60",
            extra_env={
                "CODEX_WRAPPER_DISABLE_HEARTBEAT": "0",
                "CODEX_WRAPPER_HEARTBEAT_INTERVAL_SECONDS": "1",
                "FAKE_CODEX_STREAM_EVENTS": "6",
                "FAKE_CODEX_STREAM_GAP_SECONDS": "1",
            },
            hard_timeout=60.0,
        )
        heartbeat_lines = [line for line in stderr.splitlines() if "[codex-heartbeat]" in line]
        if heartbeat_lines:
            joined = " ".join(heartbeat_lines)
            r.in_("idle=", joined, "heartbeat reports seconds since the last event")
            r.in_("events=", joined, "heartbeat reports the event count")
            r.in_("last=", joined, "heartbeat reports the last event type")
        else:
            r.fail("heartbeat progress", "no heartbeat line captured")


def test_stream_only_fallback(r: Runner) -> None:
    r.section("empty -o falls back to the LAST agent message")
    with _tempdir() as tmp:
        payload = _verify_payload(tmp)
        result, _stderr, _elapsed = _run_wrapper(
            "verify",
            "stream_only",
            ["--cwd", str(SKILL_DIR), "--payload-file", str(payload)],
            timeout_env="60",
            hard_timeout=45.0,
        )
        r.eq(result.get("status"), "ok", "stream-only run parses")
        r.eq(
            result.get("summary"),
            "Fake Codex review succeeded.",
            "the final agent message wins over the intermediate one",
        )


def test_service_tier_retry_guard(r: Runner) -> None:
    r.section("service tier retry never repeats work")
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    import invoke_codex_with_claude as wrapper

    r.falsy(
        wrapper._looks_like_service_tier_error("panic: service_tier was requested"),
        "a message merely naming the tier is not a refusal",
    )
    r.truthy(
        wrapper._looks_like_service_tier_error("400: service_tier 'priority' is not supported for this account"),
        "an explicit refusal is detected",
    )

    refused = wrapper._RunOutcome(
        raw_output="",
        exit_code=1,
        error_summary="boom",
        stderr_tail="service_tier is not supported",
        termination="nonzero",
        event_count=3,
        last_event_type="command_execution",
        did_work=True,
    )
    r.falsy(
        wrapper._can_retry_without_tier(refused),
        "a run that already executed a command is never repeated",
    )
    r.truthy(
        wrapper._can_retry_without_tier(refused._replace(did_work=False, last_event_type="turn.started")),
        "a run refused before doing anything can be retried",
    )


def test_idle_timeout_per_mode(r: Runner) -> None:
    r.section("idle limit is per mode, not one global number")
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    import invoke_codex_with_claude as wrapper

    delegate_limit = wrapper._resolve_idle_timeout("delegate")
    ask_limit = wrapper._resolve_idle_timeout("ask")
    r.ge(delegate_limit, ask_limit, "delegate tolerates longer silence than ask")
    r.ge(delegate_limit, 300.0, "delegate survives a long build or test run")

    os.environ["CODEX_WRAPPER_IDLE_TIMEOUT_SECONDS"] = "7"
    try:
        r.eq(wrapper._resolve_idle_timeout("delegate"), 7.0, "env override still wins for every mode")
    finally:
        del os.environ["CODEX_WRAPPER_IDLE_TIMEOUT_SECONDS"]


def test_subprocess_timeout_respects_env(r: Runner) -> None:
    r.section("sub-runner budget follows the wrapper's own override")
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    import codex_config

    os.environ["CODEX_WRAPPER_TIMEOUT_SECONDS"] = "1200"
    try:
        raised = codex_config.subprocess_timeout_for("plan-review")
        lowered = codex_config.subprocess_timeout_for("ask")
    finally:
        del os.environ["CODEX_WRAPPER_TIMEOUT_SECONDS"]
    r.ge(raised, 1200.0, "a raised wrapper timeout raises the caller's budget too")
    r.eq(raised, lowered, "the override wins over the per-mode config for every mode")


def test_coverage_reconciliation(r: Runner) -> None:
    r.section("coverage is reconciled against the checklist")
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    import invoke_codex_with_claude as wrapper

    result = {
        "findings": [
            {"category": "testing", "title": "t", "detail": "d"},
            {"category": "security", "title": "t", "detail": "d"},
        ],
        "coverage": [
            {"category": "testing", "findings_count": 1},
            {"category": "testing", "findings_count": 9},
            {"category": "invented-category", "findings_count": 4},
        ],
    }
    wrapper._reconcile_coverage("plan-review", result)
    categories = [entry["category"] for entry in result["coverage"]]
    expected = [name for name, _ in wrapper.PLAN_REVIEW_CATEGORIES]
    r.eq(categories, expected, "coverage lists the full checklist, in checklist order")
    r.falsy(any(c == "invented-category" for c in categories), "categories outside the checklist are dropped")
    by_name = {entry["category"]: entry["findings_count"] for entry in result["coverage"]}
    r.eq(by_name["testing"], 1, "the first declaration wins over the duplicate")
    r.eq(by_name["security"], 1, "a category omitted by Codex is filled from the findings")
    r.eq(by_name["rollback"], 0, "untouched categories are present with zero")

    ask_result = {"findings": [], "coverage": [{"category": "x", "findings_count": 1}]}
    wrapper._reconcile_coverage("ask", ask_result)
    r.falsy("coverage" in ask_result, "modes without a checklist carry no coverage")


def test_posix_process_group(r: Runner) -> None:
    r.section("killing Codex kills what Codex started")
    if sys.platform == "win32":
        print("  SKIP  (POSIX-only: Windows uses taskkill /T)")
        return
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    import invoke_codex_with_claude as wrapper

    r.truthy(wrapper._process_group_kwargs().get("start_new_session"), "the child leads its own session")

    spawner = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", spawner], **wrapper._process_group_kwargs())
    grandchild_pgid = os.getpgid(proc.pid)
    wrapper._kill_process_tree(proc.pid)
    proc.wait(timeout=10)
    time.sleep(0.5)
    try:
        os.killpg(grandchild_pgid, 0)
        alive = True
    except (ProcessLookupError, PermissionError):
        alive = False
    r.falsy(alive, "the whole process group is gone, grandchildren included")


def test_telemetry_diagnostics(r: Runner) -> None:
    r.section("telemetry records what actually ran")
    runs = TELEMETRY_SANDBOX / "runs.jsonl"
    with _tempdir() as tmp:
        plan = tmp / "p.md"
        plan.write_text("diag\n", encoding="utf-8")
        _run_wrapper("plan-review", "success", ["--cwd", str(SKILL_DIR), "--last-message-file", str(plan)])
    entry = json.loads(runs.read_text(encoding="utf-8").strip().splitlines()[-1])
    for field in ("model", "model_source", "reasoning_effort", "service_tier", "termination", "event_count"):
        r.truthy(field in entry, f"telemetry carries {field}")
    r.eq(entry["reasoning_effort"], "max", "the effort actually used is recorded")
    r.eq(entry["termination"], "ok", "a clean run is recorded as ok")

    with _tempdir() as tmp:
        plan = tmp / "p.md"
        plan.write_text("diag\n", encoding="utf-8")
        _run_wrapper(
            "plan-review",
            "idle_stall",
            ["--cwd", str(SKILL_DIR), "--last-message-file", str(plan)],
            timeout_env="60",
            extra_env={"CODEX_WRAPPER_IDLE_TIMEOUT_SECONDS": "3"},
            hard_timeout=45.0,
        )
    stalled = json.loads(runs.read_text(encoding="utf-8").strip().splitlines()[-1])
    r.eq(stalled["termination"], "idle", "an idle kill is distinguishable from a wall-clock timeout")
    r.eq(stalled["error_class"], "idle", "error_class names the termination reason")


def test_output_schema_is_strict(r: Runner) -> None:
    r.section("output schema satisfies strict structured output")
    schema = json.loads((SKILL_DIR / "scripts" / "codex_output_schema.json").read_text(encoding="utf-8"))

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            properties = node.get("properties") or {}
            required = node.get("required") or []
            missing = sorted(set(properties) - set(required))
            r.falsy(missing, f"{path or 'root'}: every property is required (missing: {missing})")
            r.truthy(node.get("additionalProperties") is False, f"{path or 'root'}: additionalProperties is false")
            for name, child in properties.items():
                walk(child, f"{path}.{name}" if path else name)
        if node.get("type") == "array":
            walk(node.get("items"), f"{path}[]")

    walk(schema, "")


def test_coverage_passthrough(r: Runner) -> None:
    r.section("coverage survives normalization")
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    from normalize_codex_result import normalize

    raw = json.dumps(
        {
            "severity": "medium",
            "confidence": "high",
            "summary": "s",
            "findings": [],
            "block_recommended": False,
            "coverage": [
                {"category": "assumptions", "findings_count": 2},
                {"category": "testing", "findings_count": 0},
                {"category": "", "findings_count": 1},
                "not-an-object",
            ],
        }
    )
    result = normalize(raw, mode="plan-review")
    coverage = result.get("coverage") or []
    r.eq(len(coverage), 2, "malformed coverage entries are dropped")
    r.eq(coverage[0].get("category"), "assumptions", "category is preserved")
    r.eq(coverage[1].get("findings_count"), 0, "zero-finding categories are kept")

    without = normalize(
        json.dumps(
            {
                "severity": "low",
                "confidence": "low",
                "summary": "s",
                "findings": [],
                "block_recommended": False,
            }
        ),
        mode="plan-review",
    )
    r.falsy("coverage" in without, "coverage is absent when Codex omits it")


def test_reasoning_effort_override(r: Runner) -> None:
    r.section("--reasoning-effort overrides per-mode default")
    with _tempdir() as tmp:
        # Case 1: --reasoning-effort high in verify mode.
        # We confirm that the argv received by Codex contains model_reasoning_effort=high.
        payload = tmp / "payload.json"
        payload.write_text(
            json.dumps(
                {
                    "cwd": str(SKILL_DIR),
                    "last_assistant_message": "x",
                    "transcript_path": "",
                    "git_status_short": "",
                    "git_diff_worktree": "",
                    "git_diff_cached": "",
                    "changed_files_from_transcript": [],
                }
            ),
            encoding="utf-8",
        )
        argv_log = tmp / "argv.json"
        common = [
            "--cwd",
            str(SKILL_DIR),
            "--payload-file",
            str(payload),
            "--reasoning-effort",
            "high",
        ]
        _run_wrapper(
            "verify",
            "success",
            common,
            extra_env={"FAKE_CODEX_ARGV_LOG_FILE": str(argv_log)},
        )
        if argv_log.exists():
            argv = json.loads(argv_log.read_text(encoding="utf-8")).get("argv", [])
            argv_str = " ".join(argv)
            r.in_(
                "model_reasoning_effort=high",
                argv_str,
                "verify+--reasoning-effort high → high passed to Codex",
            )
            r.falsy(
                "model_reasoning_effort=medium" in argv_str,
                "verify+--reasoning-effort high → medium NOT in argv",
            )
        else:
            r.fail("verify+--reasoning-effort high", "argv log not written")

        # Case 2: no --reasoning-effort in verify mode → keeps the per-mode default.
        argv_log_2 = tmp / "argv2.json"
        common_default = [
            "--cwd",
            str(SKILL_DIR),
            "--payload-file",
            str(payload),
        ]
        _run_wrapper(
            "verify",
            "success",
            common_default,
            extra_env={"FAKE_CODEX_ARGV_LOG_FILE": str(argv_log_2)},
        )
        if argv_log_2.exists():
            argv_str = " ".join(json.loads(argv_log_2.read_text(encoding="utf-8")).get("argv", []))
            r.in_(
                "model_reasoning_effort=high",
                argv_str,
                "verify default → per-mode default preserved (no regression)",
            )
        else:
            r.fail("verify default", "argv log not written")

        # Case 3: invalid value emits a warning and falls back to the per-mode default.
        argv_log_3 = tmp / "argv3.json"
        common_invalid = [
            "--cwd",
            str(SKILL_DIR),
            "--payload-file",
            str(payload),
            "--reasoning-effort",
            "ultraplus",
        ]
        _, stderr_3, _ = _run_wrapper(
            "verify",
            "success",
            common_invalid,
            extra_env={"FAKE_CODEX_ARGV_LOG_FILE": str(argv_log_3)},
        )
        if argv_log_3.exists():
            argv_str = " ".join(json.loads(argv_log_3.read_text(encoding="utf-8")).get("argv", []))
            r.in_(
                "model_reasoning_effort=high",
                argv_str,
                "invalid --reasoning-effort → falls back to per-mode default",
            )
            r.in_("ultraplus", stderr_3, "invalid --reasoning-effort → warning to stderr")
        else:
            r.fail("verify+invalid effort", "argv log not written")


PHASED_PLAN = (
    "# Big plan\n\nShared context every phase needs.\n\n"
    "## Phase 1 — groundwork\nTouch a.py.\n\n"
    "## Phase 2 — schema\nTouch b.sql, run a migration.\n\n"
    "## Phase 3 — wiring\nTouch c.ts.\n\n"
    "## Phase 4 — cleanup\nTouch d.md.\n"
)


def test_split_plan_by_phase(r: Runner) -> None:
    r.section("split_plan_by_phase × slices + coherence")
    script = SCRIPTS / "split_plan_by_phase.py"
    if not script.exists():
        r.fail("split_plan_by_phase.py present", "missing")
        return

    def _split(plan_path: Path, out_dir: Path) -> dict[str, Any]:
        proc = subprocess.run(
            [PYTHON, str(script), "--plan-file", str(plan_path), "--output-dir", str(out_dir)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        try:
            return json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return {"_stdout": proc.stdout, "_stderr": proc.stderr}

    with _tempdir() as tmp:
        plan = tmp / "plan.md"
        plan.write_text(PHASED_PLAN, encoding="utf-8")
        data = _split(plan, tmp / "slices")

        r.eq(data.get("status"), "ok", "phased plan splits")
        r.eq(data.get("phases"), 4, "one slice per phase heading")
        slices = data.get("slices") or []
        r.eq(len(slices), 5, "N phases + 1 coherence slice")
        r.eq(slices[-1].get("id"), "coherence", "the coherence slice comes last")

        first = Path(slices[0]["path"]).read_text(encoding="utf-8")
        r.in_("Shared context every phase needs.", first, "each slice repeats the plan head")
        r.in_("Phase 4 — cleanup", first, "each slice carries the outline of the other phases")
        r.in_("Touch a.py", first, "the slice carries its own phase body")
        r.truthy("Touch d.md" not in first, "a phase slice does not carry another phase's body")

        coherence = Path(slices[-1]["path"]).read_text(encoding="utf-8")
        r.in_("Touch d.md", coherence, "the coherence slice carries the whole plan")

        flat = tmp / "flat.md"
        flat.write_text("# Plan\n\nNo phases here, just prose.\n", encoding="utf-8")
        data = _split(flat, tmp / "flat_slices")
        r.eq(data.get("status"), "not_splittable", "a plan without phase headings is not split")
        r.eq(data.get("reason"), "no_phase_headings", "and says why")


def test_batch_plan_review(r: Runner) -> None:
    r.section("batch-plan-review × parallel slices, real plan-review mode")
    batch_script = SCRIPTS / "codex_batch.py"
    split_script = SCRIPTS / "split_plan_by_phase.py"
    if not batch_script.exists() or not split_script.exists():
        r.fail("batch + split scripts present", "missing")
        return

    with _tempdir() as tmp:
        plan = tmp / "plan.md"
        plan.write_text(PHASED_PLAN, encoding="utf-8")
        proc = subprocess.run(
            [PYTHON, str(split_script), "--plan-file", str(plan), "--output-dir", str(tmp / "slices")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        slices = (json.loads(proc.stdout or "{}") or {}).get("slices") or []
        r.eq(len(slices), 5, "5 slices to review")

        payload = {
            "max_parallel": 5,
            "tasks": [{"id": s["id"], "plan_file": s["path"], "cwd": str(SKILL_DIR)} for s in slices],
        }
        input_file = tmp / "batch.json"
        input_file.write_text(json.dumps(payload), encoding="utf-8")

        started = time.monotonic()
        proc = subprocess.run(
            [PYTHON, str(batch_script), "batch-plan-review", "--input-file", str(input_file)],
            env=_base_env(behavior="delay_short"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        elapsed = time.monotonic() - started
        result = json.loads(proc.stdout or "{}")

        r.eq(result.get("status"), "ok", "every slice reviewed")
        items = result.get("items") or []
        r.eq(len(items), 5, "one item per slice")
        r.truthy(
            all((i.get("result") or {}).get("mode") == "plan-review" for i in items),
            "every slice ran the real plan-review mode, not ask",
        )
        r.le(elapsed, 5 * 4.0, "slices run in parallel, not one after another")

        aggregate = result.get("aggregate") or {}
        findings = aggregate.get("findings") or []
        r.eq(len(findings), 1, "the same finding across 5 slices collapses to one")
        r.eq(len(findings[0].get("slices") or []), 5, "the merged finding records every source slice")
        r.eq(aggregate.get("duplicates_merged"), 4, "4 duplicates merged away")

        per_slice_categories = {c["category"] for c in ((items[0].get("result") or {}).get("coverage") or [])}
        union_categories = {c["category"] for c in (aggregate.get("coverage") or [])}
        r.eq(
            union_categories,
            per_slice_categories,
            "coverage is the union of the categories swept, with no category counted twice",
        )

        # The fake's behavior is process-wide, so a mixed batch is out of reach
        # here (see test_batch_ask_partial). An all-failing batch still proves
        # the aggregate never invents findings out of failed slices.
        proc = subprocess.run(
            [PYTHON, str(batch_script), "batch-plan-review", "--input-file", str(input_file)],
            env=_base_env(behavior="nonzero"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        result = json.loads(proc.stdout or "{}")
        r.eq(result.get("status"), "error", "every slice failing → aggregate status=error")
        r.eq(proc.returncode, 2, "a failed batch also exits non-zero")
        r.eq(len(result.get("items") or []), 5, "a failed slice still reports an item")
        r.eq(len((result.get("aggregate") or {}).get("findings") or []), 0, "no findings invented from failures")


def test_quota_exhaustion_detection(r: Runner) -> None:
    r.section("quota exhaustion is classified, not buried in a generic failure")
    sys.path.insert(0, str(SCRIPTS))
    import invoke_codex_with_claude as wrapper

    real = (
        "ERROR: You've hit your usage limit. Upgrade to Pro "
        "(https://chatgpt.com/explore/pro), visit "
        "https://chatgpt.com/codex/settings/usage to purchase more credits "
        "or try again at Aug 15th, 2026 5:45 PM."
    )
    r.eq(
        wrapper.detect_quota_exhaustion(real),
        "Aug 15th, 2026 5:45 PM",
        "the reset date is lifted out of the Codex stderr",
    )
    r.eq(
        wrapper.detect_quota_exhaustion("You have exceeded your quota."),
        "",
        "quota without a date returns empty, not None",
    )
    r.truthy(
        wrapper.detect_quota_exhaustion("Codex exited with non-zero status (1)") is None,
        "an ordinary failure is not misread as a quota problem",
    )
    r.truthy(
        wrapper.detect_quota_exhaustion("", "") is None,
        "empty diagnostics are not a quota problem",
    )
    r.eq(
        wrapper.detect_quota_exhaustion("", real),
        "Aug 15th, 2026 5:45 PM",
        "raw stdout is searched too, not only stderr",
    )

    with _tempdir() as tmp:
        plan = tmp / "plan.md"
        plan.write_text("# Plan\n\nTouch scripts/set_locale.py.\n", encoding="utf-8")
        result, rc = _run_wrapper_rc(
            "plan-review",
            "quota_exhausted",
            ["--cwd", str(SKILL_DIR), "--last-message-file", str(plan)],
        )
        r.eq(result.get("error_class"), "quota_exhausted", "the wrapper classifies it end to end")
        r.eq(rc, 2, "quota exhaustion still exits non-zero")
        r.in_("Aug 15th", result.get("summary") or "", "the reset date reaches the user's summary")


def test_split_routing(r: Runner) -> None:
    r.section("split × interactive phases, background coherence")
    script = SCRIPTS / "split_plan_by_phase.py"
    with _tempdir() as tmp:
        plan = tmp / "plan.md"
        plan.write_text(PHASED_PLAN, encoding="utf-8")
        proc = subprocess.run(
            [
                PYTHON,
                str(script),
                "--plan-file",
                str(plan),
                "--output-dir",
                str(tmp / "slices"),
                "--cwd",
                str(SKILL_DIR),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        data = json.loads(proc.stdout or "{}")
        slices = data.get("slices") or []
        phases = [s for s in slices if s["kind"] == "phase"]
        coherence = [s for s in slices if s["kind"] == "coherence"]

        r.truthy(all(s["route"] == "interactive" for s in phases), "every phase slice is interactive")
        r.eq(coherence[0]["route"], "background", "the coherence slice is routed to background")
        r.truthy(
            all(s["reasoning_effort"] == phases[0]["reasoning_effort"] for s in phases),
            "phase slices share one effort",
        )
        r.truthy(
            coherence[0]["reasoning_effort"] != phases[0]["reasoning_effort"],
            "coherence keeps a different (higher) effort than the phases",
        )

        batch = json.loads(Path(data["batch_input"]).read_text(encoding="utf-8"))
        ids = [t["id"] for t in batch["tasks"]]
        r.eq(len(ids), len(phases), "batch_input carries only the interactive slices")
        r.truthy("coherence" not in ids, "the coherence slice stays off the critical path")
        r.truthy(all(t.get("reasoning_effort") for t in batch["tasks"]), "each task carries its effort")
        r.le(batch["max_parallel"], 4, "parallelism is capped low enough to avoid self-contention")
        r.eq(batch["tasks"][0]["cwd"], str(SKILL_DIR), "--cwd reaches the batch input")


def test_batch_effort_passthrough(r: Runner) -> None:
    r.section("batch passes reasoning_effort through to the wrapper")
    with _tempdir() as tmp:
        plan = tmp / "slice.md"
        plan.write_text("# Slice\n\nTouch scripts/set_locale.py.\n", encoding="utf-8")
        argv_log = tmp / "argv.json"
        batch = {
            "max_parallel": 1,
            "tasks": [
                {
                    "id": "s1",
                    "plan_file": str(plan),
                    "cwd": str(SKILL_DIR),
                    "reasoning_effort": "medium",
                }
            ],
        }
        input_file = tmp / "batch.json"
        input_file.write_text(json.dumps(batch), encoding="utf-8")
        subprocess.run(
            [PYTHON, str(BATCHER), "batch-plan-review", "--input-file", str(input_file)],
            env=_base_env(behavior="success", extra={"FAKE_CODEX_ARGV_LOG_FILE": str(argv_log)}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if argv_log.exists():
            argv = " ".join(json.loads(argv_log.read_text(encoding="utf-8")).get("argv", []))
            r.in_("model_reasoning_effort=medium", argv, "the per-task effort reaches the Codex CLI")
        else:
            r.fail("batch effort passthrough", "argv log not written")


def test_batch_stops_on_quota(r: Runner) -> None:
    r.section("batch stops spending once quota is gone")
    with _tempdir() as tmp:
        plan = tmp / "slice.md"
        plan.write_text("# Slice\n\nTouch scripts/set_locale.py.\n", encoding="utf-8")
        batch = {
            "max_parallel": 1,
            "tasks": [{"id": f"s{i}", "plan_file": str(plan), "cwd": str(SKILL_DIR)} for i in range(1, 6)],
        }
        input_file = tmp / "batch.json"
        input_file.write_text(json.dumps(batch), encoding="utf-8")
        proc = subprocess.run(
            [PYTHON, str(BATCHER), "batch-plan-review", "--input-file", str(input_file)],
            env=_base_env(behavior="quota_exhausted"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        result = json.loads(proc.stdout or "{}")
        items = result.get("items") or []
        r.truthy(result.get("quota_exhausted"), "the batch reports the quota wall explicitly")
        r.eq(len(items), 5, "every task still reports an item")
        skipped = [i for i in items if "quota" in (i.get("summary") or "").lower()]
        r.ge(len(skipped), 1, "later items are skipped instead of burning the same wall again")
        r.eq(proc.returncode, 2, "a quota-stopped batch exits non-zero")


def test_aggregate_findings_unit(r: Runner) -> None:
    r.section("batch aggregate × dedupe, severity and partial input")
    sys.path.insert(0, str(SCRIPTS))
    import codex_batch

    items = [
        {
            "id": "phase-1",
            "status": "ok",
            "result": {
                "severity": "low",
                "block_recommended": False,
                "coverage": [{"category": "design", "findings_count": 1}],
                "findings": [
                    {"severity": "low", "title": "Same defect", "location": "a.py:1"},
                    {"severity": "low", "title": "Only here", "location": "b.py:2"},
                ],
            },
        },
        {
            "id": "phase-2",
            "status": "ok",
            "result": {
                "severity": "high",
                "block_recommended": True,
                "coverage": [
                    {"category": "design", "findings_count": 1},
                    {"category": "security", "findings_count": 1},
                ],
                # Same defect, different casing and spacing, higher severity.
                "findings": [{"severity": "high", "title": "same  DEFECT", "location": "A.py:1"}],
            },
        },
        {"id": "phase-3", "status": "error", "result": {}},
    ]

    agg = codex_batch._aggregate_findings(items)
    titles = [f.get("title") for f in agg["findings"]]
    r.eq(len(agg["findings"]), 2, "the duplicate collapses, the unique one survives")
    r.eq(titles[0], "Same defect", "highest severity sorts first")
    r.eq(agg["findings"][0]["severity"], "high", "the merged finding keeps the highest severity seen")
    r.eq(sorted(agg["findings"][0]["slices"]), ["phase-1", "phase-2"], "both source slices recorded")
    r.eq(agg["severity"], "high", "aggregate severity is the highest across slices")
    r.truthy(agg["block_recommended"], "block_recommended is any(), not all()")
    r.eq(len(agg["coverage"]), 2, "coverage unions the categories without duplicating design")
    r.eq(agg["duplicates_merged"], 1, "one duplicate merged")


def test_timeout_scales_with_prompt(r: Runner) -> None:
    r.section("wall-clock ceiling scales with the assembled prompt")
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    import codex_config
    import invoke_codex_with_claude as wrapper

    base = wrapper._base_timeout("plan-review")
    ceiling = wrapper.max_timeout_for("plan-review")

    r.eq(wrapper._resolve_timeout("plan-review", 0), base, "no prompt → per-mode base")
    r.eq(
        wrapper._resolve_timeout("plan-review", wrapper.TIMEOUT_SCALE_FREE_BYTES),
        base,
        "prompt inside the free window → still the base",
    )

    one_unit = wrapper.TIMEOUT_SCALE_FREE_BYTES + wrapper.TIMEOUT_SCALE_BYTES_PER_UNIT
    r.eq(
        wrapper._resolve_timeout("plan-review", one_unit),
        base * 2.0,
        "one scale unit past the free window doubles the ceiling",
    )

    huge = wrapper._resolve_timeout("plan-review", 100 * 1024 * 1024)
    r.eq(huge, ceiling, "an enormous prompt saturates at the ceiling, never beyond")
    r.truthy(ceiling > base, "the ceiling is above the base, otherwise scaling is a no-op")

    os.environ["CODEX_WRAPPER_TIMEOUT_SECONDS"] = "77"
    try:
        r.eq(
            wrapper._resolve_timeout("plan-review", 100 * 1024 * 1024),
            77.0,
            "the env override wins over the scaling",
        )
    finally:
        del os.environ["CODEX_WRAPPER_TIMEOUT_SECONDS"]

    # The invariant that keeps a supervisor from killing a wrapper that is
    # still inside its own budget.
    codex_config.clear_cache()
    for mode in wrapper.ALL_MODES:
        budget = codex_config.subprocess_timeout_for(mode)
        r.ge(budget, wrapper.max_timeout_for(mode), f"caller budget covers the {mode} ceiling")


def test_analyze_exposes_raw_metrics(r: Runner) -> None:
    r.section("analyze_plan_complexity × raw metrics")
    script = SCRIPTS / "analyze_plan_complexity.py"
    with _tempdir() as tmp:
        plan = tmp / "plan.md"
        plan.write_text(
            "# Plan\n\n"
            "## Phase 1\nTouch a.py and b.ts, run a migration.\n\n"
            "## Phase 2\nTouch c.tsx, d.sql, e.go, f.rs, g.java.\n\n"
            "## Phase 3\nWire the api handler to the service repo.\n",
            encoding="utf-8",
        )
        proc = subprocess.run(
            [PYTHON, str(script), "--plan-file", str(plan)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        data = json.loads(proc.stdout or "{}")
        metrics = data.get("metrics") or {}
        r.eq(metrics.get("phases"), 3, "phases counted")
        r.eq(metrics.get("size_bytes"), plan.stat().st_size, "size_bytes matches the file")
        r.ge(metrics.get("distinct_files") or 0, 7, "distinct files counted")
        r.in_("migration", metrics.get("sensitive_hits") or [], "sensitive keywords listed raw")
        r.in_("api", metrics.get("cross_module_hits") or [], "cross-module dirs listed raw")

        missing = subprocess.run(
            [PYTHON, str(script), "--plan-file", str(tmp / "nope.md")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        fallback = json.loads(missing.stdout or "{}")
        r.truthy("metrics" in fallback, "metrics is present even on the soft-failure path")


def test_wrapper_exit_codes(r: Runner) -> None:
    r.section("wrapper × exit code mirrors status")
    with _tempdir() as tmp:
        plan = tmp / "plan.md"
        plan.write_text("# Plan\n\nTouch scripts/set_locale.py.\n", encoding="utf-8")
        common = ["--cwd", str(SKILL_DIR), "--last-message-file", str(plan)]

        result, rc = _run_wrapper_rc("plan-review", "success", common)
        r.eq(rc, 0, "status=ok → exit 0")
        r.eq(result.get("status"), "ok", "status=ok still on stdout")

        result, rc = _run_wrapper_rc("plan-review", "timeout", common, timeout_env="2", hard_timeout=25.0)
        r.eq(rc, 2, "timeout → exit 2")
        r.eq(result.get("status"), "error", "timeout still emits parseable JSON")
        r.eq(result.get("error_class"), "timeout", "timeout keeps its error_class")

        result, rc = _run_wrapper_rc("plan-review", "needs_input", common)
        r.eq(rc, 3, "needs_input → exit 3")
        r.eq(result.get("status"), "needs_input", "needs_input still on stdout")

        result, rc = _run_wrapper_rc("plan-review", "invalid_json", common)
        r.eq(rc, 2, "unparseable Codex reply → exit 2")


def test_dialogue_error_envelope(r: Runner) -> None:
    r.section("codex_dialogue × failed turn never reports ok")
    dialogue = SCRIPTS / "codex_dialogue.py"
    if not dialogue.exists():
        r.fail("codex_dialogue.py present", "missing")
        return

    env = _base_env(behavior="timeout", timeout="2")
    with _tempdir() as tmp:
        plan = tmp / "plan.md"
        plan.write_text("# Plan\nTouch foo.py.\n", encoding="utf-8")

        codes: list[int] = []

        def _turn(cmd: list[str]) -> dict[str, Any]:
            proc = subprocess.run(
                [PYTHON, str(dialogue)] + cmd,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
            )
            codes.append(proc.returncode)
            try:
                return json.loads(proc.stdout)
            except json.JSONDecodeError:
                return {}

        first = _turn(
            [
                "start",
                "--accepted-by-user",
                "--plan-file",
                str(plan),
                "--cwd",
                str(SKILL_DIR),
                "--max-turns",
                "3",
            ]
        )
        r.eq(first.get("status"), "error", "turn 1 timed out → envelope status=error")
        r.eq(first.get("wrapper_status"), "error", "envelope exposes wrapper_status")
        dialogue_id = first.get("dialogue_id")
        r.truthy(dialogue_id, "dialogue_id still returned on a failed turn")
        if not dialogue_id:
            return

        second = _turn(["next-turn", "--dialogue-id", dialogue_id, "--plan-file", str(plan)])
        r.eq(second.get("status"), "error", "turn 2 timed out → envelope status=error")
        r.truthy(
            second.get("stop_reason") != "converged",
            "two failed turns are never read as convergence",
        )
        r.eq(codes[-1], 2, "a failed turn also exits non-zero")


# ----------------------------------------------------------------------- main


def main() -> int:
    if not WRAPPER.exists():
        print(f"FATAL: wrapper not found at {WRAPPER}")
        return 1
    if not FAKE.exists():
        print(f"FATAL: fake_codex.py not found at {FAKE}")
        return 1

    r = Runner()
    started = time.monotonic()

    test_plan_review_modes(r)
    test_verify_mode(r)
    test_ask_mode(r)
    test_insight_mode(r)
    test_delegate_mode(r)
    test_review_packet_window(r)
    test_review_packet_max_files(r)
    test_review_packet_byte_truncation(r)
    test_review_packet_empty_plan(r)
    test_wrapper_auto_packet(r)
    test_batch_ask_speedup(r)
    test_batch_ask_partial(r)
    test_batch_delegate_overlap(r)
    test_batch_delegate_violation(r)
    test_telemetry_schema(r)
    test_telemetry_rotation(r)
    test_ps1_stub(r)
    test_disable_heartbeat_silent(r)
    test_heartbeat_reports_progress(r)
    test_model_and_service_tier(r)
    test_idle_timeout(r)
    test_output_schema_is_strict(r)
    test_coverage_passthrough(r)
    test_coverage_reconciliation(r)
    test_stream_only_fallback(r)
    test_service_tier_retry_guard(r)
    test_idle_timeout_per_mode(r)
    test_subprocess_timeout_respects_env(r)
    test_posix_process_group(r)
    test_telemetry_diagnostics(r)
    test_reasoning_effort_override(r)
    test_codex_config(r)
    test_analyze_plan_complexity(r)
    test_dialogue_lifecycle(r)
    test_wrapper_exit_codes(r)
    test_dialogue_error_envelope(r)
    test_timeout_scales_with_prompt(r)
    test_analyze_exposes_raw_metrics(r)
    test_split_plan_by_phase(r)
    test_batch_plan_review(r)
    test_aggregate_findings_unit(r)
    test_quota_exhaustion_detection(r)
    test_split_routing(r)
    test_batch_effort_passthrough(r)
    test_batch_stops_on_quota(r)

    duration = time.monotonic() - started
    print()
    print(f"=== summary: {r.passed} passed, {r.failed} failed in {duration:.1f}s ===")
    if r.failures:
        print()
        print("Failures:")
        for f in r.failures:
            print(f"  - {f}")
    return r.failed


if __name__ == "__main__":
    raise SystemExit(main())
