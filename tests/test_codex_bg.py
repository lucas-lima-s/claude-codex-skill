"""End-to-end tests for ``scripts/codex_bg.py``.

Self-contained — no pytest, no fixtures, no third-party deps. Drives the
real codex_bg subprocess against the existing fake_codex (via
CODEX_WRAPPER_CODEX_OVERRIDE) so it runs offline and deterministically.

Run::

    "$SKILLS_PYTHON" tests/test_codex_bg.py

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

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

THIS_DIR = Path(__file__).resolve().parent
SKILL_DIR = THIS_DIR.parent
SCRIPTS = SKILL_DIR / "scripts"
WRAPPER = SCRIPTS / "invoke_codex_with_claude.py"
BG_SCRIPT = SCRIPTS / "codex_bg.py"
FAKE = THIS_DIR / "fake_codex.py"
# Never the real cache: run directories and runs.jsonl rows produced against
# the fake Codex would otherwise be indistinguishable from production runs.
CACHE_SANDBOX = Path(tempfile.mkdtemp(prefix="codex-test-bgcache-"))
BG_RUNS_DIR = CACHE_SANDBOX / "bg_runs"


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
            self.fail(label, f"value not truthy: {value!r}")

    def falsy(self, value: Any, label: str) -> None:
        if not value:
            self.passed_(label)
        else:
            self.fail(label, f"expected falsy, got {value!r}")

    def in_(self, needle: Any, haystack: Any, label: str) -> None:
        try:
            ok = needle in haystack
        except TypeError:
            ok = False
        if ok:
            self.passed_(label)
        else:
            self.fail(label, f"{needle!r} not in {haystack!r}")

    def le(self, actual: float, ceiling: float, label: str) -> None:
        if actual <= ceiling:
            self.passed_(label)
        else:
            self.fail(label, f"{actual} > ceiling {ceiling}")


# ----------------------------------------------------------------------- helpers


def _bg_env(behavior: str = "success", extra: dict[str, str] | None = None) -> dict[str, str]:
    """Env passed to the codex_bg subprocess; the spawned wrapper child
    inherits these (in particular CODEX_WRAPPER_CODEX_OVERRIDE).

    Wrapper timeout is set generously so tests using ``delay_long`` can
    observe the running state for a few seconds without the wrapper killing
    the fake mid-sleep.
    """
    env = os.environ.copy()
    env["CODEX_WRAPPER_CODEX_OVERRIDE"] = str(FAKE)
    env["CODEX_WRAPPER_TIMEOUT_SECONDS"] = "60"
    env["CODEX_WRAPPER_DISABLE_HEARTBEAT"] = "1"
    env["FAKE_CODEX_BEHAVIOR"] = behavior
    env["CODEX_WRAPPER_CACHE_DIR"] = str(CACHE_SANDBOX)
    if extra:
        env.update(extra)
    return env


_LAST_RETURNCODE = 0


def _run_bg(args: list[str], env: dict[str, str] | None = None, timeout: float = 30.0) -> tuple[dict[str, Any], str]:
    global _LAST_RETURNCODE
    cmd = [PYTHON, str(BG_SCRIPT), *args]
    proc = subprocess.run(
        cmd,
        env=env or os.environ.copy(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    _LAST_RETURNCODE = proc.returncode
    try:
        result = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        result = {"_parse_error": True, "_stdout": proc.stdout}
    return result, proc.stderr


def _wait_for_done(
    run_id: str, env: dict[str, str], max_wait: float = 30.0, poll_interval: float = 0.5
) -> dict[str, Any]:
    """Polls bg-status until status != running (or timeout)."""
    deadline = time.monotonic() + max_wait
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last, _ = _run_bg(["status", run_id], env=env)
        if last.get("status") != "running":
            return last
        time.sleep(poll_interval)
    return last


@contextmanager
def _tempdir():
    path = Path(tempfile.mkdtemp(prefix="codex-bg-test-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _purge_bg_runs() -> None:
    """Clear cache/bg_runs/ between tests so concurrency caps and listing
    behavior are deterministic. Only removes directories — does not touch
    siblings of the bg_runs folder."""
    if BG_RUNS_DIR.exists():
        for child in BG_RUNS_DIR.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)


# ----------------------------------------------------------------------- tests


def test_start_status_output(r: Runner) -> None:
    r.section("bg start → status → output (happy path)")
    _purge_bg_runs()
    with _tempdir() as tmp:
        plan = tmp / "p.md"
        plan.write_text("background test plan\n", encoding="utf-8")
        env = _bg_env(behavior="success")
        start_args = [
            "start",
            "plan-review",
            "--cwd",
            str(SKILL_DIR),
            "--last-message-file",
            str(plan),
        ]
        started_at = time.monotonic()
        result, _ = _run_bg(start_args, env=env)
        elapsed = time.monotonic() - started_at
        r.eq(result.get("status"), "ok", "start returns status=ok")
        r.le(elapsed, 5.0, "start returns control quickly (<5s)")
        run_id = result.get("run_id")
        r.truthy(run_id, "start emits a run_id")
        r.truthy(result.get("pid"), "start emits a pid")
        if not run_id:
            return

        final_status = _wait_for_done(run_id, env=env, max_wait=20.0)
        r.eq(
            final_status.get("status"),
            "done",
            "status converges to 'done' after fake codex finishes",
        )
        r.eq(final_status.get("pid_alive"), False, "pid_alive=false after subprocess exits")
        r.truthy(final_status.get("finished_at"), "finished_at is populated on terminal status")

        out, _ = _run_bg(["output", run_id], env=env)
        r.eq(out.get("status"), "ok", "output returns wrapper canonical JSON (status=ok)")
        r.eq(out.get("mode"), "plan-review", "output preserves the wrapper's mode field")
        findings = out.get("findings") or []
        r.eq(len(findings), 1, "output preserves the fake's 1 finding")


def test_output_while_running(r: Runner) -> None:
    r.section("bg output × still running")
    _purge_bg_runs()
    with _tempdir() as tmp:
        plan = tmp / "p.md"
        plan.write_text("delay test\n", encoding="utf-8")
        # delay_long makes the fake sleep 35s — plenty of time to check
        # status while running. Override to 8s to avoid slowing the suite down.
        env = _bg_env(behavior="delay_long", extra={"FAKE_CODEX_DELAY_SECONDS": "8"})
        result, _ = _run_bg(
            ["start", "plan-review", "--cwd", str(SKILL_DIR), "--last-message-file", str(plan)],
            env=env,
        )
        run_id = result.get("run_id")
        if not run_id:
            r.fail("start produced no run_id", str(result))
            return

        # right after start: still running
        time.sleep(1.0)
        st, _ = _run_bg(["status", run_id], env=env)
        r.eq(st.get("status"), "running", "status=running while subprocess alive")
        r.eq(st.get("pid_alive"), True, "pid_alive=true while subprocess alive")

        # output while running: must refuse with still_running
        out, _ = _run_bg(["output", run_id], env=env)
        r.eq(out.get("status"), "error", "output returns error while still running")
        r.eq(out.get("reason"), "still_running", "reason=still_running")

        # wait for completion to clean up
        _wait_for_done(run_id, env=env, max_wait=20.0)


def test_cancel(r: Runner) -> None:
    r.section("bg cancel kills the run and marks cancelled")
    _purge_bg_runs()
    with _tempdir() as tmp:
        plan = tmp / "p.md"
        plan.write_text("cancel test\n", encoding="utf-8")
        env = _bg_env(behavior="delay_long", extra={"FAKE_CODEX_DELAY_SECONDS": "30"})
        result, _ = _run_bg(
            ["start", "plan-review", "--cwd", str(SKILL_DIR), "--last-message-file", str(plan)],
            env=env,
        )
        run_id = result.get("run_id")
        if not run_id:
            r.fail("start produced no run_id", str(result))
            return

        time.sleep(1.0)
        cancel_result, _ = _run_bg(["cancel", run_id], env=env)
        r.eq(cancel_result.get("status"), "ok", "cancel returns status=ok")
        r.eq(cancel_result.get("cancelled"), True, "cancel marks cancelled=true")
        r.eq(cancel_result.get("was_alive"), True, "cancel reports the run was alive at the time")

        # The flag is written and the meta is updated — status must reflect cancelled.
        time.sleep(1.5)
        st, _ = _run_bg(["status", run_id], env=env)
        r.eq(st.get("status"), "cancelled", "status converges to 'cancelled'")

        # Cancel idempotente
        cancel_again, _ = _run_bg(["cancel", run_id], env=env)
        r.eq(cancel_again.get("status"), "ok", "second cancel still ok")


def test_list_orders_runs(r: Runner) -> None:
    r.section("bg list orders by started_at desc")
    _purge_bg_runs()
    with _tempdir() as tmp:
        plan = tmp / "p.md"
        plan.write_text("list test\n", encoding="utf-8")
        env = _bg_env(behavior="success")

        run_ids: list[str] = []
        for _ in range(3):
            res, _ = _run_bg(
                ["start", "plan-review", "--cwd", str(SKILL_DIR), "--last-message-file", str(plan)],
                env=env,
            )
            rid = res.get("run_id")
            if rid:
                run_ids.append(rid)
            time.sleep(1.1)  # ensure distinct started_at (1s precision)

        for rid in run_ids:
            _wait_for_done(rid, env=env, max_wait=10.0)

        listing, _ = _run_bg(["list", "--limit", "10"], env=env)
        r.eq(listing.get("status"), "ok", "list returns status=ok")
        runs = listing.get("runs") or []
        r.truthy(len(runs) >= 3, f"list contains the 3 runs we just created (got {len(runs)})")

        listed_ids = [r_["run_id"] for r_ in runs[:3]]
        # The top 3 entries in the list must be the freshly created ones,
        # in descending started_at order (most recent first).
        expected_top = list(reversed(run_ids))
        r.eq(listed_ids, expected_top, "list orders runs by started_at desc")


def test_max_concurrent(r: Runner) -> None:
    r.section("bg start respeita --max-concurrent")
    _purge_bg_runs()
    with _tempdir() as tmp:
        plan = tmp / "p.md"
        plan.write_text("max concurrent test\n", encoding="utf-8")
        env = _bg_env(behavior="delay_long", extra={"FAKE_CODEX_DELAY_SECONDS": "20"})

        first_args = [
            "start",
            "plan-review",
            "--max-concurrent",
            "2",
            "--cwd",
            str(SKILL_DIR),
            "--last-message-file",
            str(plan),
        ]
        a, _ = _run_bg(first_args, env=env)
        b, _ = _run_bg(first_args, env=env)
        r.eq(a.get("status"), "ok", "1st start ok")
        r.eq(b.get("status"), "ok", "2nd start ok")

        # 3rd start with limit=2 should be refused.
        c, _ = _run_bg(first_args, env=env)
        r.eq(c.get("status"), "error", "3rd start refused (status=error)")
        r.eq(c.get("reason"), "max_concurrent_reached", "reason=max_concurrent_reached")
        active_ids = c.get("active_run_ids") or []
        r.eq(len(active_ids), 2, "active_run_ids lists the 2 currently running")
        r.eq(c.get("limit"), 2, "limit echoed back in error response")

        # Cleanup: cancel the running ones so the test exits quickly.
        for rid in (a.get("run_id"), b.get("run_id")):
            if rid:
                _run_bg(["cancel", rid], env=env)


def test_status_not_found(r: Runner) -> None:
    r.section("bg status × run_id desconhecido")
    _purge_bg_runs()
    res, _ = _run_bg(["status", "deadbeefcafe"], env=_bg_env())
    r.eq(res.get("status"), "error", "unknown run_id → status=error")
    r.eq(res.get("reason"), "not_found", "reason=not_found")

    listing, _ = _run_bg(["list", "--limit", "5"], env=_bg_env())
    r.eq(listing.get("status"), "ok", "list of an empty cache is still a success")
    r.eq(_LAST_RETURNCODE, 0, "a successful subcommand exits zero")


def test_invalid_mode(r: Runner) -> None:
    r.section("bg start × modo que não existe no wrapper")
    _purge_bg_runs()
    env = _bg_env()

    res, _ = _run_bg(["start", "plan-review-iter", "--cwd", str(SKILL_DIR)], env=env)
    r.eq(res.get("status"), "error", "plan-review-iter → status=error")
    r.eq(_LAST_RETURNCODE, 2, "an invalid mode also exits non-zero")
    r.eq(res.get("reason"), "invalid_mode", "reason=invalid_mode")
    r.in_("plan-review", res.get("valid_modes") or [], "valid_modes lists the wrapper modes")
    r.truthy(res.get("run_id") is None, "no run_id handed out for an invalid mode")

    created = list(BG_RUNS_DIR.iterdir()) if BG_RUNS_DIR.exists() else []
    r.eq(len(created), 0, "no run directory created for an invalid mode")

    res, _ = _run_bg(["start", "batch-ask", "--cwd", str(SKILL_DIR)], env=env)
    r.eq(res.get("reason"), "invalid_mode", "batch-ask is also rejected")


def test_died_on_startup(r: Runner) -> None:
    r.section("bg start × wrapper morre no arranque")
    _purge_bg_runs()
    env = _bg_env()

    # An unknown flag is forwarded to the wrapper, whose argparse rejects it
    # and exits before writing a single byte of JSON.
    res, _ = _run_bg(
        ["start", "ask", "--cwd", str(SKILL_DIR), "--flag-that-does-not-exist"],
        env=env,
        timeout=40.0,
    )
    r.eq(res.get("status"), "error", "dead wrapper → status=error")
    r.eq(_LAST_RETURNCODE, 2, "a dead wrapper also exits non-zero")
    r.eq(res.get("reason"), "died_on_startup", "reason=died_on_startup")
    r.truthy(res.get("run_id") is None or res.get("stderr_tail"), "stderr is surfaced to the caller")
    r.in_("unrecognized arguments", res.get("stderr_tail") or "", "stderr_tail carries the argparse error")

    listing, _ = _run_bg(["list", "--limit", "5"], env=env)
    runs = listing.get("runs") or []
    r.truthy(
        all(run.get("status") != "running" for run in runs),
        "a run that died on startup is never left as running",
    )


# ----------------------------------------------------------------------- main


def main() -> int:
    if not BG_SCRIPT.exists():
        print(f"FATAL: codex_bg.py not found at {BG_SCRIPT}")
        return 1
    if not WRAPPER.exists():
        print(f"FATAL: wrapper not found at {WRAPPER}")
        return 1
    if not FAKE.exists():
        print(f"FATAL: fake_codex.py not found at {FAKE}")
        return 1

    r = Runner()
    started = time.monotonic()

    test_start_status_output(r)
    test_output_while_running(r)
    test_cancel(r)
    test_list_orders_runs(r)
    test_max_concurrent(r)
    test_status_not_found(r)
    test_invalid_mode(r)
    test_died_on_startup(r)

    duration = time.monotonic() - started
    print()
    print(f"=== summary: {r.passed} passed, {r.failed} failed in {duration:.1f}s ===")
    if r.failures:
        print()
        print("Failures:")
        for f in r.failures:
            print(f"  - {f}")

    _purge_bg_runs()
    return r.failed


if __name__ == "__main__":
    raise SystemExit(main())
