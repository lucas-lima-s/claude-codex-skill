"""Live integration tests against the real Codex CLI.

WARNING: this suite SPENDS TOKENS. Every assertion below makes an actual
``codex exec`` call. Skip when not on a developer machine with credentials.

Run::

    PYTHONIOENCODING=utf-8 PYTHONUTF8=1 "$SKILLS_PYTHON" tests/test_codex_live.py

Coverage:
  * plan-review with a small but real plan;
  * ask with a deterministic question;
  * verify with a diff that contains an obvious bug;
  * delegate creating a file inside the workspace;
  * delegate editing/deleting a file OUTSIDE the workspace
    (``$env:TEMP/codex-test-<random>``) — the original plan's acceptance
    criterion;
  * batch-ask with 3 parallel questions;
  * an ambiguous prompt to exercise the needs_input path (best-effort:
    Codex may also choose to answer with caveats — both are accepted).

Insight is intentionally skipped (5-min timeout, expensive) — the wrapper
contract for that mode is already validated against the fake.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

THIS_DIR = Path(__file__).resolve().parent
SKILL_DIR = THIS_DIR.parent
SCRIPTS = SKILL_DIR / "scripts"
WRAPPER = SCRIPTS / "invoke_codex_with_claude.py"
BATCHER = SCRIPTS / "codex_batch.py"

PYTHON = os.environ.get("SKILLS_PYTHON") or sys.executable


# ----------------------------------------------------------------------- runner


class Runner:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.failures: list[str] = []
        self.token_proxy_bytes = 0
        self.total_seconds = 0.0

    def section(self, name: str) -> None:
        print(f"\n=== {name} ===")

    def passed_(self, label: str) -> None:
        self.passed += 1
        print(f"  PASS  {label}")

    def fail(self, label: str, detail: str) -> None:
        self.failed += 1
        self.failures.append(f"{label}: {detail}")
        print(f"  FAIL  {label}  -- {detail}")

    def skip(self, label: str, why: str) -> None:
        self.skipped += 1
        print(f"  SKIP  {label}  -- {why}")

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

    def in_(self, needle: Any, haystack: Any, label: str) -> None:
        if needle in haystack:
            self.passed_(label)
        else:
            short = (
                (haystack[:200] + "...")
                if isinstance(haystack, str) and len(haystack) > 200
                else haystack
            )
            self.fail(label, f"{needle!r} not found in {short!r}")

    def in_any(self, needles: list[str], haystack: str, label: str) -> None:
        if any(n in haystack for n in needles):
            self.passed_(label)
        else:
            short = haystack[:200] + "..."
            self.fail(label, f"none of {needles!r} found in {short!r}")

    def status_in(self, result: dict[str, Any], allowed: tuple, label: str) -> None:
        if result.get("status") in allowed:
            self.passed_(label)
        else:
            self.fail(
                label,
                f"status={result.get('status')!r} not in {allowed!r}; "
                f"summary={result.get('summary', '')[:120]!r}",
            )


# ----------------------------------------------------------------------- helpers


def _live_env(timeout: str = "300") -> dict[str, str]:
    env = os.environ.copy()
    env.pop("CODEX_WRAPPER_CODEX_OVERRIDE", None)
    env["CODEX_WRAPPER_TIMEOUT_SECONDS"] = timeout
    env["CODEX_WRAPPER_DISABLE_HEARTBEAT"] = "1"
    env["CODEX_LOCALE"] = "en-US"
    return env


def _run_wrapper(
    mode: str,
    args: list[str],
    timeout: int = 300,
    extra_env: dict[str, str] | None = None,
) -> tuple[dict[str, Any], float]:
    cmd = [PYTHON, str(WRAPPER), mode] + args
    env = _live_env(str(timeout))
    if extra_env:
        env.update(extra_env)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 60,
        )
    except subprocess.TimeoutExpired:
        return (
            {"status": "error", "summary": "wrapper subprocess hard-timeout"},
            time.monotonic() - started,
        )
    elapsed = time.monotonic() - started
    try:
        result = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        result = {
            "status": "error",
            "summary": "non-JSON wrapper stdout",
            "_stdout": proc.stdout[:1000],
            "_stderr": proc.stderr[:500],
        }
    return result, elapsed


def _run_batch(
    sub_mode: str, payload: dict[str, Any], timeout: int = 600
) -> tuple[dict[str, Any], float]:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
        path = f.name
    try:
        env = _live_env("300")
        cmd = [PYTHON, str(BATCHER), sub_mode, "--input-file", path]
        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return (
                {"status": "error", "summary": "batch subprocess hard-timeout"},
                time.monotonic() - started,
            )
        elapsed = time.monotonic() - started
        try:
            result = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            result = {"status": "error", "summary": "non-JSON batch stdout"}
        return result, elapsed
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@contextmanager
def _tempdir(prefix: str = "codex-live-"):
    p = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield p
    finally:
        shutil.rmtree(p, ignore_errors=True)


def _is_codex_available() -> bool:
    if shutil.which("codex"):
        return True
    return False


# ----------------------------------------------------------------------- tests


def test_plan_review_real(r: Runner) -> None:
    r.section("LIVE plan-review")
    with _tempdir() as tmp:
        plan = tmp / "plan.md"
        plan.write_text(
            "# Plan: add a sum function\n\n"
            "Create a Python function `add(a, b)` in `mod.py` that returns a + b.\n"
            "Add a basic test in `test_mod.py`.\n",
            encoding="utf-8",
        )
        result, elapsed = _run_wrapper(
            "plan-review",
            ["--cwd", str(tmp), "--last-message-file", str(plan)],
            timeout=300,
        )
        r.total_seconds += elapsed
        r.token_proxy_bytes += len(result.get("raw_codex_output") or "")

        r.status_in(result, ("ok", "needs_input"), "plan-review status ok|needs_input")
        r.eq(result.get("mode"), "plan-review", "plan-review mode")
        r.truthy(result.get("summary"), "plan-review has summary")
        r.truthy("fingerprint" in result, "plan-review fingerprint present")
        # findings is a list (may be empty if Codex thinks the plan is fine)
        r.truthy(isinstance(result.get("findings"), list), "plan-review findings is list")
        print(
            f"     elapsed={elapsed:.1f}s  output_bytes={len(result.get('raw_codex_output') or '')}"
        )


def test_ask_real(r: Runner) -> None:
    r.section("LIVE ask")
    with _tempdir() as tmp:
        q = tmp / "q.txt"
        q.write_text(
            "Reply in English with a single word: is 17 a prime number? Answer just yes or no.",
            encoding="utf-8",
        )
        result, elapsed = _run_wrapper(
            "ask",
            ["--cwd", str(tmp), "--question-file", str(q)],
            timeout=180,
        )
        r.total_seconds += elapsed
        r.token_proxy_bytes += len(result.get("raw_codex_output") or "")

        r.eq(result.get("status"), "ok", "ask status=ok")
        r.eq(result.get("mode"), "ask", "ask mode")
        summary = (result.get("summary") or "").lower()
        r.in_any(["yes", "prime", "17"], summary, "ask summary mentions yes/prime/17")
        print(f"     elapsed={elapsed:.1f}s  summary={result.get('summary', '')[:120]!r}")


def test_verify_real(r: Runner) -> None:
    r.section("LIVE verify (diff with bug)")
    with _tempdir() as tmp:
        # Synthetic diff with a clear Python syntax bug: missing closing paren.
        diff = (
            "diff --git a/buggy.py b/buggy.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/buggy.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+def broken(\n"
            "+    return 42\n"
            "+\n"
        )
        payload = {
            "cwd": str(tmp),
            "last_assistant_message": "I added a Python file with a function called broken().",
            "transcript_path": "",
            "git_status_short": "?? buggy.py",
            "git_diff_worktree": diff,
            "git_diff_cached": "",
            "changed_files_from_transcript": [],
        }
        payload_path = tmp / "payload.json"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        result, elapsed = _run_wrapper(
            "verify",
            ["--cwd", str(tmp), "--payload-file", str(payload_path)],
            timeout=240,
        )
        r.total_seconds += elapsed
        r.token_proxy_bytes += len(result.get("raw_codex_output") or "")

        r.status_in(result, ("ok", "needs_input"), "verify status ok|needs_input")
        r.eq(result.get("mode"), "verify", "verify mode")
        # Codex should call out the syntax problem.
        blob = (
            result.get("summary", "")
            + " "
            + " ".join(
                f.get("title", "") + " " + f.get("detail", "") for f in result.get("findings", [])
            )
        ).lower()
        r.in_any(
            ["syntax", "parenthes", "incomplete", "broken", "invalid"],
            blob,
            "verify spots syntax-ish problem in diff",
        )
        print(f"     elapsed={elapsed:.1f}s  findings={len(result.get('findings', []))}")


def test_delegate_in_workspace(r: Runner) -> None:
    r.section("LIVE delegate (dentro do workspace)")
    with _tempdir() as tmp:
        marker = "Hello from Codex live test"
        task = tmp / "task.txt"
        task.write_text(
            "Create a single text file at the root of the working directory named "
            f"`hello.txt`. The file content must be exactly the literal string: {marker}\n"
            "Do not create any other files. Do not run any extra commands.\n",
            encoding="utf-8",
        )
        result, elapsed = _run_wrapper(
            "delegate",
            ["--cwd", str(tmp), "--task-file", str(task)],
            timeout=300,
        )
        r.total_seconds += elapsed
        r.token_proxy_bytes += len(result.get("raw_codex_output") or "")

        r.status_in(result, ("ok", "needs_input"), "delegate-in status ok|needs_input")
        target = tmp / "hello.txt"
        r.truthy(target.is_file(), "delegate-in: hello.txt was created")
        if target.is_file():
            content = target.read_text(encoding="utf-8", errors="replace").strip()
            r.in_(marker, content, "delegate-in: file contains the marker text")
        print(f"     elapsed={elapsed:.1f}s")


def test_delegate_outside_workspace(r: Runner) -> None:
    r.section("LIVE delegate (outside the workspace — plan criterion)")
    # Workspace where the wrapper is anchored:
    workspace = Path(tempfile.mkdtemp(prefix="codex-live-ws-"))
    # External target dir, the literal pattern from the original plan:
    external_dir = Path(tempfile.gettempdir()) / f"codex-test-{uuid.uuid4().hex[:8]}"
    external_dir.mkdir(parents=True, exist_ok=True)
    target_file = external_dir / "external.txt"
    target_file.write_text("ORIGINAL CONTENT — to be replaced by Codex\n", encoding="utf-8")
    placeholder = external_dir / "deleteme.txt"
    placeholder.write_text("delete me\n", encoding="utf-8")

    try:
        task_path = workspace / "task.txt"
        task_path.write_text(
            f"Two file operations OUTSIDE this working directory:\n"
            f"1. Replace the entire contents of `{target_file}` with exactly the line "
            f"`EDITED-BY-CODEX` (no trailing whitespace, no extra lines).\n"
            f"2. Delete the file `{placeholder}` entirely.\n"
            f"Do not touch any file inside the working directory. Confirm both "
            f"operations in the JSON response (files_edited / files_deleted).\n",
            encoding="utf-8",
        )

        result, elapsed = _run_wrapper(
            "delegate",
            ["--cwd", str(workspace), "--task-file", str(task_path)],
            timeout=300,
        )
        r.total_seconds += elapsed
        r.token_proxy_bytes += len(result.get("raw_codex_output") or "")

        r.status_in(result, ("ok", "needs_input"), "delegate-out status ok|needs_input")

        # Real assertions on the filesystem (not the JSON report).
        edited_ok = (
            target_file.is_file()
            and target_file.read_text(encoding="utf-8", errors="replace").strip()
            == "EDITED-BY-CODEX"
        )
        r.truthy(edited_ok, "delegate-out: external.txt was REWRITTEN to 'EDITED-BY-CODEX'")
        r.truthy(not placeholder.exists(), "delegate-out: deleteme.txt was actually deleted")

        # The wrapper should also surface the change in the JSON output.
        edited_paths = [str(p).lower() for p in result.get("files_edited") or []]
        deleted_paths = [str(p).lower() for p in result.get("files_deleted") or []]
        r.truthy(
            any("external.txt" in p for p in edited_paths),
            "delegate-out: files_edited reports external.txt",
        )
        r.truthy(
            any("deleteme.txt" in p for p in deleted_paths),
            "delegate-out: files_deleted reports deleteme.txt",
        )
        print(f"     elapsed={elapsed:.1f}s  external_dir={external_dir}")
    finally:
        # Cleanup — both the workspace and the external dir.
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(external_dir, ignore_errors=True)


def test_batch_ask_real(r: Runner) -> None:
    r.section("LIVE batch-ask (3 parallel questions)")
    with _tempdir() as tmp:
        payload = {
            "max_parallel": 3,
            "tasks": [
                {
                    "id": "math",
                    "question": "Reply with one short sentence: what is 12 times 12?",
                    "cwd": str(tmp),
                },
                {
                    "id": "geo",
                    "question": "Reply with one word: capital of France?",
                    "cwd": str(tmp),
                },
                {
                    "id": "hex",
                    "question": "Reply with one number only: hexadecimal value of 255 in decimal?",
                    "cwd": str(tmp),
                },
            ],
        }
        result, elapsed = _run_batch("batch-ask", payload, timeout=600)
        r.total_seconds += elapsed
        for it in result.get("items", []):
            r.token_proxy_bytes += len((it.get("result") or {}).get("raw_codex_output") or "")

        r.eq(result.get("status"), "ok", "batch-ask aggregate status=ok")
        items = result.get("items", [])
        r.eq(len(items), 3, "batch-ask returned 3 items")
        ids = sorted(i["id"] for i in items)
        r.eq(ids, ["geo", "hex", "math"], "batch-ask preserved all ids")

        # Inspect each answer
        by_id = {i["id"]: (i.get("result") or {}).get("summary", "").lower() for i in items}
        r.in_any(["144"], by_id.get("math", ""), "batch-ask/math answers 144")
        r.in_any(["paris"], by_id.get("geo", ""), "batch-ask/geo answers Paris")
        r.in_any(["ff", "0xff", "255"], by_id.get("hex", ""), "batch-ask/hex mentions ff or 255")
        print(f"     elapsed={elapsed:.1f}s  items_summary={result.get('summary')}")


def test_needs_input_real(r: Runner) -> None:
    r.section("LIVE plan-review with vague plan (expecting needs_input or findings)")
    with _tempdir() as tmp:
        plan = tmp / "plan.md"
        plan.write_text(
            "# Plan\n\n" "Refactor the system. Improve performance. Add tests.\n",
            encoding="utf-8",
        )
        result, elapsed = _run_wrapper(
            "plan-review",
            ["--cwd", str(tmp), "--last-message-file", str(plan)],
            timeout=240,
        )
        r.total_seconds += elapsed
        r.token_proxy_bytes += len(result.get("raw_codex_output") or "")

        # Either Codex calls it out via findings, asks for clarification, or
        # gracefully reports the plan is too vague — all are valid outcomes.
        r.status_in(result, ("ok", "needs_input"), "vague-plan status ok|needs_input")
        # Expect at least one of: needs_input questions, or low confidence,
        # or findings flagging vagueness.
        signals_vagueness = (
            result.get("status") == "needs_input"
            or len(result.get("findings", [])) > 0
            or any(
                k in (result.get("summary") or "").lower()
                for k in (
                    "vague",
                    "ambig",
                    "unclear",
                    "missing",
                    "specific",
                    "scope",
                    "details",
                    "context",
                )
            )
        )
        r.truthy(
            signals_vagueness, "vague-plan: Codex signals vagueness (needs_input/findings/summary)"
        )
        print(
            f"     elapsed={elapsed:.1f}s  status={result.get('status')}  "
            f"findings={len(result.get('findings', []))}  "
            f"questions={len(result.get('questions', []))}"
        )


# ----------------------------------------------------------------------- main


def main() -> int:
    if not _is_codex_available():
        print("FATAL: codex CLI not on PATH. Run from a developer shell with credentials.")
        return 1

    r = Runner()
    started = time.monotonic()

    test_plan_review_real(r)
    test_ask_real(r)
    test_verify_real(r)
    test_delegate_in_workspace(r)
    test_delegate_outside_workspace(r)
    test_batch_ask_real(r)
    test_needs_input_real(r)

    duration = time.monotonic() - started
    print()
    print(
        f"=== summary: {r.passed} passed, {r.failed} failed, "
        f"{r.skipped} skipped in {duration:.1f}s wall-clock ==="
    )
    print(f"     Codex sub-process time:   {r.total_seconds:.1f}s")
    print(f"     Codex output bytes total: {r.token_proxy_bytes}  (proxy for token spend)")
    if r.failures:
        print()
        print("Failures:")
        for f in r.failures:
            print(f"  - {f}")
    return r.failed


if __name__ == "__main__":
    raise SystemExit(main())
