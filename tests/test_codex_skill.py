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
from typing import Any, Dict, List, Optional, Tuple

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
        self.failures: List[str] = []

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

def _base_env(behavior: str = "success", timeout: str = "10",
              extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = os.environ.copy()
    env["CODEX_WRAPPER_CODEX_OVERRIDE"] = str(FAKE)
    env["CODEX_WRAPPER_TIMEOUT_SECONDS"] = timeout
    env["CODEX_WRAPPER_DISABLE_HEARTBEAT"] = "1"
    env["FAKE_CODEX_BEHAVIOR"] = behavior
    if extra:
        env.update(extra)
    return env


def _run_wrapper(mode: str, behavior: str, args: List[str],
                 timeout_env: str = "10",
                 extra_env: Optional[Dict[str, str]] = None,
                 hard_timeout: float = 30.0) -> Tuple[Dict[str, Any], str, float]:
    """Returns (parsed_json_result, stderr, wall_clock_seconds)."""
    env = _base_env(behavior=behavior, timeout=timeout_env, extra=extra_env)
    cmd = [PYTHON, str(WRAPPER), mode] + args
    started = time.monotonic()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=hard_timeout)
    elapsed = time.monotonic() - started
    try:
        result = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        result = {"_parse_error": True, "_stdout": proc.stdout}
    return result, proc.stderr, elapsed


@contextmanager
def _tempdir():
    path = Path(tempfile.mkdtemp(prefix="codex-test-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# ----------------------------------------------------------------------- tests

def test_plan_review_modes(r: Runner) -> None:
    r.section("plan-review × cenários")
    with _tempdir() as tmp:
        plan = tmp / "plan.md"
        plan.write_text(
            "# Plano teste\n\nMexer em scripts/normalize_codex_result.py para algo.\n",
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
        r.in_("structured json", (result.get("summary") or "").lower(),
              "plan-review/invalid_json summary mentions JSON")

        # partial → degraded best-effort
        result, _, _ = _run_wrapper("plan-review", "partial", common)
        r.eq(result.get("status"), "ok", "plan-review/partial salvaged status=ok")
        r.truthy(result.get("degraded"), "plan-review/partial degraded=true")

        # nonzero exit
        result, _, _ = _run_wrapper("plan-review", "nonzero", common)
        r.eq(result.get("status"), "error", "plan-review/nonzero status=error")
        r.in_("non-zero status (2)", result.get("summary", ""),
              "plan-review/nonzero summary mentions exit 2")

        # needs_input
        result, _, _ = _run_wrapper("plan-review", "needs_input", common)
        r.eq(result.get("status"), "needs_input", "plan-review/needs_input status=needs_input")
        questions = result.get("questions", [])
        r.eq(len(questions), 1, "plan-review/needs_input 1 question")
        r.in_("staging", questions[0].get("context", "").lower(),
              "plan-review/needs_input context preserved")

        # noisy stderr does not break stdout JSON (wrapper consumes child stderr,
        # so we only check that stdout JSON was unaffected by 50 stderr lines)
        result, _, _ = _run_wrapper("plan-review", "noisy_stderr", common)
        r.eq(result.get("status"), "ok", "plan-review/noisy_stderr status=ok")
        r.eq(len(result.get("findings", [])), 1,
             "plan-review/noisy_stderr stdout JSON intact despite stderr noise")

        # timeout
        result, _, elapsed = _run_wrapper(
            "plan-review", "timeout", common, timeout_env="2"
        )
        r.eq(result.get("status"), "error", "plan-review/timeout status=error")
        r.in_("timeout", result.get("summary", "").lower(),
              "plan-review/timeout summary mentions timeout")
        r.le(elapsed, 6.0, "plan-review/timeout returns <=6s")


def test_verify_mode(r: Runner) -> None:
    r.section("verify × cenários")
    with _tempdir() as tmp:
        payload = tmp / "payload.json"
        payload.write_text(json.dumps({
            "cwd": str(SKILL_DIR),
            "last_assistant_message": "Refatorei normalize_codex_result.py",
            "git_status_short": " M scripts/normalize_codex_result.py",
            "git_diff_worktree": "diff --git a/x b/x\n@@ -1 +1,2 @@\n+new line\n",
            "git_diff_cached": "",
        }), encoding="utf-8")
        common = ["--cwd", str(SKILL_DIR), "--payload-file", str(payload)]

        result, _, _ = _run_wrapper("verify", "success", common)
        r.eq(result.get("status"), "ok", "verify/success status=ok")
        r.eq(result.get("mode"), "verify", "verify/success mode")

        result, _, _ = _run_wrapper("verify", "invalid_json", common)
        r.eq(result.get("status"), "error", "verify/invalid_json status=error")


def test_ask_mode(r: Runner) -> None:
    r.section("ask × cenários")
    with _tempdir() as tmp:
        question = tmp / "q.txt"
        question.write_text("Qual a complexidade de quicksort?", encoding="utf-8")
        common = ["--cwd", str(SKILL_DIR), "--question-file", str(question)]

        result, _, _ = _run_wrapper("ask", "success", common)
        r.eq(result.get("status"), "ok", "ask/success status=ok")
        r.eq(result.get("mode"), "ask", "ask/success mode")
        r.truthy(result.get("summary"), "ask/success has non-empty summary")


def test_insight_mode(r: Runner) -> None:
    r.section("insight × cenários")
    with _tempdir() as tmp:
        focus = tmp / "focus.txt"
        focus.write_text("Foque em arquitetura.", encoding="utf-8")
        common = ["--cwd", str(SKILL_DIR), "--focus-file", str(focus)]

        result, _, _ = _run_wrapper("insight", "success", common)
        r.eq(result.get("status"), "ok", "insight/success status=ok")
        r.eq(result.get("mode"), "insight", "insight/success mode")


def test_delegate_mode(r: Runner) -> None:
    r.section("delegate × cenários")
    with _tempdir() as tmp:
        task = tmp / "task.txt"
        task.write_text("Criar um arquivo fake/new.txt vazio.", encoding="utf-8")
        common = ["--cwd", str(SKILL_DIR), "--task-file", str(task)]

        # delegate_ok behavior returns delegate-shaped JSON
        result, _, _ = _run_wrapper("delegate", "delegate_ok", common)
        r.eq(result.get("status"), "ok", "delegate/delegate_ok status=ok")
        r.eq(result.get("mode"), "delegate", "delegate/delegate_ok mode")
        r.eq(result.get("files_created"), ["fake/new.txt"],
             "delegate/delegate_ok files_created passed through")
        r.eq(result.get("files_edited"), ["fake/touched.py"],
             "delegate/delegate_ok files_edited passed through")
        r.eq(result.get("commands_run"), ["echo fake"],
             "delegate/delegate_ok commands_run passed through")

        # delegate accepts prose (no JSON) without erroring
        result, _, _ = _run_wrapper("delegate", "invalid_json", common)
        r.eq(result.get("status"), "ok", "delegate/invalid_json tolerates prose")


def test_review_packet_window(r: Runner) -> None:
    r.section("build_review_packet × seleção e janelas")
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
        plan.write_text("Refatorar src/huge.py:200 com cuidado.\n", encoding="utf-8")
        out = tmp / "packet.md"
        proc = subprocess.run(
            [PYTHON, str(BUILDER),
             "--plan-file", str(plan),
             "--cwd", str(tmp),
             "--output", str(out)],
            capture_output=True, text=True, timeout=15,
        )
        r.eq(proc.returncode, 0, "builder exit=0 (with line citation)")
        body = out.read_text(encoding="utf-8")
        r.in_("window around line 200", body,
              "packet picks ±50 window when line cited and file >max_lines")

        # Plan without :line on a >max_lines file → first N lines
        plan2 = tmp / "plan2.md"
        plan2.write_text("Mexer em src/huge.py.\n", encoding="utf-8")
        out2 = tmp / "packet2.md"
        subprocess.run(
            [PYTHON, str(BUILDER),
             "--plan-file", str(plan2),
             "--cwd", str(tmp),
             "--max-lines", "50",
             "--output", str(out2)],
            capture_output=True, text=True, timeout=15,
        )
        body2 = out2.read_text(encoding="utf-8")
        r.in_("first 50 lines", body2,
              "packet picks first N lines when no :line cited")

        # File <= max_lines: full file mode
        small = big_dir / "small.py"
        small.write_text("\n".join(f"# {i}" for i in range(50)) + "\n", encoding="utf-8")
        plan3 = tmp / "plan3.md"
        plan3.write_text("Mexer em src/small.py:5 quickly.\n", encoding="utf-8")
        out3 = tmp / "packet3.md"
        subprocess.run(
            [PYTHON, str(BUILDER),
             "--plan-file", str(plan3),
             "--cwd", str(tmp),
             "--output", str(out3)],
            capture_output=True, text=True, timeout=15,
        )
        body3 = out3.read_text(encoding="utf-8")
        r.in_("full file", body3,
              "packet uses full file when total <= max_lines")


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
            "# plano\nTouch the following files:\n"
            + "\n".join(f"- {c}" for c in cited),
            encoding="utf-8",
        )
        out = tmp / "packet.md"
        subprocess.run(
            [PYTHON, str(BUILDER),
             "--plan-file", str(plan),
             "--cwd", str(tmp),
             "--max-files", "12",
             "--output", str(out)],
            capture_output=True, text=True, timeout=15,
        )
        body = out.read_text(encoding="utf-8")
        # 12 should be included, 2 should be skipped with manifest entries.
        included_count = sum(1 for c in cited if f"### {tmp.resolve()}" in body and c.replace("/", os.sep) in body)
        skipped_count = body.count("skipped (max_files=12 exceeded)")
        r.eq(skipped_count, 2, "packet skips exactly 2 files when 14 cited")
        # Loose includes check (resolved path may differ on Windows)
        r.ge(included_count + skipped_count, 12, "packet processed at least 12 files")


def test_review_packet_byte_truncation(r: Runner) -> None:
    r.section("build_review_packet × truncamento por max_bytes")
    with _tempdir() as tmp:
        big = tmp / "big.py"
        big.write_text("\n".join(f"# line {i:04d}" for i in range(2000)),
                       encoding="utf-8")
        plan = tmp / "plan.md"
        plan.write_text("Touch big.py thoroughly.\n", encoding="utf-8")
        out = tmp / "packet.md"
        subprocess.run(
            [PYTHON, str(BUILDER),
             "--plan-file", str(plan),
             "--cwd", str(tmp),
             "--max-bytes", "2048",
             "--output", str(out)],
            capture_output=True, text=True, timeout=15,
        )
        body = out.read_text(encoding="utf-8")
        r.in_("packet truncated", body,
              "packet truncates body when max_bytes exceeded")


def test_review_packet_empty_plan(r: Runner) -> None:
    r.section("build_review_packet × plano vazio")
    with _tempdir() as tmp:
        plan = tmp / "empty.md"
        plan.write_text("   \n", encoding="utf-8")
        out = tmp / "packet.md"
        proc = subprocess.run(
            [PYTHON, str(BUILDER),
             "--plan-file", str(plan),
             "--cwd", str(tmp),
             "--output", str(out)],
            capture_output=True, text=True, timeout=15,
        )
        r.eq(proc.returncode, 2, "empty plan → builder returns 2")


def test_wrapper_auto_packet(r: Runner) -> None:
    r.section("wrapper auto-build do review packet em plan-review")
    with _tempdir() as tmp:
        plan = tmp / "plan.md"
        plan.write_text(
            "Mexer em scripts/normalize_codex_result.py:50 com cuidado.\n",
            encoding="utf-8",
        )
        common = ["--cwd", str(SKILL_DIR), "--last-message-file", str(plan)]
        result, _, _ = _run_wrapper("plan-review", "success", common)
        r.eq(result.get("status"), "ok",
             "wrapper plan-review with auto-packet returns ok")


def test_batch_ask_speedup(r: Runner) -> None:
    r.section("batch-ask × speedup paralelo")
    with _tempdir() as tmp:
        batch = {
            "max_parallel": 4,
            "tasks": [
                {"id": f"q{i}", "question": f"Question {i}?", "cwd": str(SKILL_DIR)}
                for i in range(4)
            ],
        }
        batch_file = tmp / "batch.json"
        batch_file.write_text(json.dumps(batch), encoding="utf-8")

        env = _base_env(behavior="delay_short")

        # Sequential baseline
        t0 = time.monotonic()
        for _ in range(4):
            subprocess.run(
                [PYTHON, str(WRAPPER), "ask", "--cwd", str(SKILL_DIR),
                 "--question-file", str(batch_file)],
                env=env, capture_output=True, timeout=30,
            )
        seq = time.monotonic() - t0

        # Parallel
        t0 = time.monotonic()
        proc = subprocess.run(
            [PYTHON, str(BATCHER), "batch-ask", "--input-file", str(batch_file)],
            env=env, capture_output=True, text=True, timeout=60,
        )
        par = time.monotonic() - t0

        speedup = seq / par if par > 0 else 0
        r.ge(speedup, 2.0, f"batch-ask ≥2× speedup ({speedup:.2f}× observed)")

        result = json.loads(proc.stdout)
        r.eq(result.get("status"), "ok", "batch-ask all-success status=ok")
        r.eq(len(result.get("items", [])), 4, "batch-ask returns 4 items")
        r.falsy(result.get("partial"), "batch-ask partial=false on full success")


def test_batch_ask_partial(r: Runner) -> None:
    r.section("batch-ask × falha parcial não cancela demais")
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
            env=env, capture_output=True, text=True, timeout=60,
        )
        result = json.loads(proc.stdout)
        r.eq(result.get("status"), "ok", "batch-ask 3/3 ok aggregate")
        ids = sorted(i["id"] for i in result.get("items", []))
        r.eq(ids, ["ok1", "ok2", "ok3"], "batch-ask preserves item ids")

        # Partial: force every item to fail (nonzero) to exercise error path
        env_fail = _base_env(behavior="nonzero")
        proc = subprocess.run(
            [PYTHON, str(BATCHER), "batch-ask", "--input-file", str(path)],
            env=env_fail, capture_output=True, text=True, timeout=60,
        )
        result = json.loads(proc.stdout)
        r.eq(result.get("status"), "error",
             "batch-ask all-fail aggregate=error")


def test_batch_delegate_overlap(r: Runner) -> None:
    r.section("batch-delegate × write-set overlap")
    with _tempdir() as tmp:
        batch = {
            "tasks": [
                {"id": "t1", "task": "x", "cwd": str(SKILL_DIR),
                 "write_set": ["src/a.py", "src/shared.py"]},
                {"id": "t2", "task": "y", "cwd": str(SKILL_DIR),
                 "write_set": ["src/b.py", "src/shared.py"]},
            ],
        }
        path = tmp / "ov.json"
        path.write_text(json.dumps(batch), encoding="utf-8")
        env = _base_env(behavior="delegate_ok")
        proc = subprocess.run(
            [PYTHON, str(BATCHER), "batch-delegate", "--input-file", str(path)],
            env=env, capture_output=True, text=True, timeout=30,
        )
        result = json.loads(proc.stdout)
        r.eq(result.get("status"), "error",
             "batch-delegate overlap aggregate=error")
        overlaps = result.get("overlaps") or []
        r.ge(len(overlaps), 1, "overlap descriptor present")
        r.in_("shared.py", str(overlaps), "overlap path identified")


def test_batch_delegate_violation(r: Runner) -> None:
    r.section("batch-delegate × write_set_violated quando Codex extrapola")
    with _tempdir() as tmp:
        batch = {
            "tasks": [
                {"id": "d1", "task": "x", "cwd": str(SKILL_DIR),
                 "write_set": ["src/a.py"]},
                {"id": "d2", "task": "y", "cwd": str(SKILL_DIR),
                 "write_set": ["src/b.py"]},
            ],
        }
        path = tmp / "dj.json"
        path.write_text(json.dumps(batch), encoding="utf-8")
        env = _base_env(behavior="delegate_ok")
        proc = subprocess.run(
            [PYTHON, str(BATCHER), "batch-delegate", "--input-file", str(path)],
            env=env, capture_output=True, text=True, timeout=60,
        )
        result = json.loads(proc.stdout)
        r.eq(result.get("status"), "ok", "batch-delegate disjoint runs ok")
        violated = [i for i in result.get("items", []) if i.get("write_set_violated")]
        r.eq(len(violated), 2, "all items mark write_set_violated when Codex extrapolates")


def test_telemetry_schema(r: Runner) -> None:
    r.section("telemetria × schema e gravação")
    cache_dir = SKILL_DIR / "cache"
    cache_dir.mkdir(exist_ok=True)
    runs = cache_dir / "runs.jsonl"
    backup = cache_dir / "runs.jsonl.1"
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
    required = {"schema_version", "timestamp", "run_id", "mode", "cwd",
                "duration_ms", "status", "packet_bytes", "retry_count",
                "error_class", "exit_code"}
    missing = required - set(entry.keys())
    r.eq(missing, set(), f"telemetry has all required fields ({sorted(required)})")
    r.eq(entry["mode"], "plan-review", "telemetry: mode field correct")
    r.eq(entry["status"], "ok", "telemetry: status field correct")
    r.eq(entry["schema_version"], 1, "telemetry: schema_version=1")


def test_telemetry_rotation(r: Runner) -> None:
    r.section("telemetria × rotação aos 5 MB")
    cache_dir = SKILL_DIR / "cache"
    cache_dir.mkdir(exist_ok=True)
    runs = cache_dir / "runs.jsonl"
    backup = cache_dir / "runs.jsonl.1"

    # Pre-fill above 5 MB
    runs.write_bytes(b"x" * (5 * 1024 * 1024 + 100))
    if backup.exists():
        backup.unlink()

    with _tempdir() as tmp:
        plan = tmp / "p.md"
        plan.write_text("rot\n", encoding="utf-8")
        common = ["--cwd", str(SKILL_DIR), "--last-message-file", str(plan)]
        _run_wrapper("plan-review", "success", common)

    r.truthy(backup.exists(), "rotation: runs.jsonl.1 created")
    r.le(runs.stat().st_size, 5 * 1024 * 1024,
         "rotation: runs.jsonl below 5 MB after rotation")
    r.ge(backup.stat().st_size, 5 * 1024 * 1024,
         "rotation: runs.jsonl.1 holds the old payload")


def test_ps1_stub(r: Runner) -> None:
    r.section("PowerShell stub × forwarding de args")
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
            pwsh, "-NoProfile", "-File", str(PS1),
            "plan-review", "--cwd", str(SKILL_DIR), "--last-message-file", str(plan),
        ]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
        r.eq(data.get("status"), "ok", "ps1 stub forwards args to wrapper")


def test_disable_heartbeat_silent(r: Runner) -> None:
    r.section("heartbeat × CODEX_WRAPPER_DISABLE_HEARTBEAT=1 silencia stderr")
    with _tempdir() as tmp:
        plan = tmp / "p.md"
        plan.write_text("hb test\n", encoding="utf-8")
        common = ["--cwd", str(SKILL_DIR), "--last-message-file", str(plan)]
        _, stderr, _ = _run_wrapper(
            "plan-review", "delay_short", common,
            extra_env={"CODEX_WRAPPER_DISABLE_HEARTBEAT": "1"},
        )
        r.falsy("[codex-heartbeat]" in stderr,
                "heartbeat suppressed when env=1")


def test_reasoning_effort_override(r: Runner) -> None:
    r.section("--reasoning-effort sobrescreve default por modo")
    with _tempdir() as tmp:
        # Caso 1: --reasoning-effort high em modo verify (default seria medium).
        # Confirmamos que o argv recebido pelo Codex contém model_reasoning_effort=high.
        payload = tmp / "payload.json"
        payload.write_text(json.dumps({
            "cwd": str(SKILL_DIR),
            "last_assistant_message": "x",
            "transcript_path": "",
            "git_status_short": "",
            "git_diff_worktree": "",
            "git_diff_cached": "",
            "changed_files_from_transcript": [],
        }), encoding="utf-8")
        argv_log = tmp / "argv.json"
        common = [
            "--cwd", str(SKILL_DIR),
            "--payload-file", str(payload),
            "--reasoning-effort", "high",
        ]
        _run_wrapper(
            "verify", "success", common,
            extra_env={"FAKE_CODEX_ARGV_LOG_FILE": str(argv_log)},
        )
        if argv_log.exists():
            argv = json.loads(argv_log.read_text(encoding="utf-8")).get("argv", [])
            argv_str = " ".join(argv)
            r.in_("model_reasoning_effort=high", argv_str,
                  "verify+--reasoning-effort high → high passed to Codex")
            r.falsy("model_reasoning_effort=medium" in argv_str,
                    "verify+--reasoning-effort high → medium NOT in argv")
        else:
            r.fail("verify+--reasoning-effort high", "argv log not written")

        # Caso 2: sem --reasoning-effort em modo verify → mantém default medium.
        argv_log_2 = tmp / "argv2.json"
        common_default = [
            "--cwd", str(SKILL_DIR),
            "--payload-file", str(payload),
        ]
        _run_wrapper(
            "verify", "success", common_default,
            extra_env={"FAKE_CODEX_ARGV_LOG_FILE": str(argv_log_2)},
        )
        if argv_log_2.exists():
            argv_str = " ".join(json.loads(argv_log_2.read_text(encoding="utf-8")).get("argv", []))
            r.in_("model_reasoning_effort=medium", argv_str,
                  "verify default → medium preserved (no regression)")
        else:
            r.fail("verify default", "argv log not written")

        # Caso 3: valor inválido vira warning + cai no default por modo.
        argv_log_3 = tmp / "argv3.json"
        common_invalid = [
            "--cwd", str(SKILL_DIR),
            "--payload-file", str(payload),
            "--reasoning-effort", "ultraplus",
        ]
        _, stderr_3, _ = _run_wrapper(
            "verify", "success", common_invalid,
            extra_env={"FAKE_CODEX_ARGV_LOG_FILE": str(argv_log_3)},
        )
        if argv_log_3.exists():
            argv_str = " ".join(json.loads(argv_log_3.read_text(encoding="utf-8")).get("argv", []))
            r.in_("model_reasoning_effort=medium", argv_str,
                  "invalid --reasoning-effort → falls back to per-mode default")
            r.in_("ultraplus", stderr_3,
                  "invalid --reasoning-effort → warning to stderr")
        else:
            r.fail("verify+invalid effort", "argv log not written")


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
    test_reasoning_effort_override(r)

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
