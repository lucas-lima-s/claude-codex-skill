"""Background runner for the codex wrapper.

Lets Claude fire off long Codex runs (delegate, insight, plan-review) without
blocking the foreground session and reclaim results later.

Subcommands (each prints a single JSON object on stdout)::

    start <mode> [--cwd ...] [...wrapper args]
        Spawns a detached subprocess running invoke_codex_with_claude.py
        with the given args. Returns ``{run_id, started_at, pid}``.

        ``mode`` is validated against the canonical wrapper modes before
        anything is spawned: ``plan-review-iter`` (a codex_dialogue.py flow)
        and ``batch-*`` (codex_batch.py) are rejected with
        ``reason=invalid_mode`` instead of dying inside the wrapper's own
        argparse, where nothing would observe the failure.

        After spawning, the process is probed for
        ``background.startup_probe_seconds``. A process already gone by then
        without leaving parseable JSON returns
        ``{status: error, reason: died_on_startup, exit_code, stderr_tail}``
        rather than a ``run_id`` a caller would go on to poll forever.

    status <run_id>
        Reports current state. Returns
        ``{status, started_at, finished_at, mode, pid, pid_alive}`` where
        status is one of ``running | done | error | cancelled``.

    output <run_id>
        Returns the wrapper's canonical JSON output once status==done.
        Returns ``{status: "error", reason: "still_running"}`` while the
        process is alive; ``{status: "error", reason: "no_output"}`` when
        the run terminated without writing usable JSON.

    cancel <run_id>
        Kills the subprocess tree and marks the run as cancelled. Idempotent.

    list [--limit N]
        Lists runs (default 50) ordered by ``started_at`` desc.

Persistence layout (``cache/bg_runs/<run_id>/``)::

    meta.json       wrapper args + pid + started_at + finished_at + status
    output.json     canonical wrapper output (raw stdout of the subprocess)
    stderr.log      subprocess stderr (heartbeat lines included with the
                    [codex-heartbeat] prefix)
    cancelled.flag  empty file; presence indicates explicit cancel

Concurrency cap: ``--max-concurrent`` / ``CODEX_BG_MAX_CONCURRENT``
(default 5). When exceeded, ``start`` refuses with
``status=error, reason=max_concurrent_reached`` and lists the active runs.

Cleanup: directories with ``mtime > 7d`` AND status in
``{done, error, cancelled}`` are pruned at the start of every subcommand.
Active runs are never touched.

This script never raises: every error path returns a JSON object with
``status=error`` and a short ``reason`` so callers can react programmatically.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
SKILL_DIR = BIN_DIR.parent
WRAPPER = BIN_DIR / "invoke_codex_with_claude.py"
# Mirrors the wrapper's own CACHE_DIR resolution so a test run can redirect
# both the run directories and the telemetry away from the real cache.
CACHE_ROOT = Path(os.environ.get("CODEX_WRAPPER_CACHE_DIR") or (SKILL_DIR / "cache"))
CACHE_DIR = CACHE_ROOT / "bg_runs"

sys.path.insert(0, str(BIN_DIR))
from codex_config import (  # noqa: E402
    EXIT_ERROR,
    EXIT_OK,
    ensure_setup_complete,
    t,
)
from codex_config import (
    get as _config_get,
)
from codex_config import (
    resolve_python as _resolve_python,
)

DEFAULT_MAX_CONCURRENT = int(_config_get("background.default_max_concurrent", 5))
DEFAULT_CLEANUP_MAX_AGE_DAYS = int(_config_get("background.default_cleanup_max_age_days", 7))
LIST_DEFAULT_LIMIT = int(_config_get("background.list_default_limit", 50))
STARTUP_PROBE_SECONDS = float(_config_get("background.startup_probe_seconds", 3.0))

VALID_MODES = tuple(_config_get("wrapper.modes", ("plan-review", "verify", "delegate", "ask", "insight")))

TERMINAL_STATUSES = ("done", "error", "cancelled")
MAX_STARTUP_STDERR_CHARS = 4000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_last_emitted_status = ""


def _emit(payload: dict[str, Any]) -> None:
    global _last_emitted_status
    _last_emitted_status = str(payload.get("status") or "")
    text = json.dumps(payload, ensure_ascii=False)
    try:
        sys.stdout.buffer.write(text.encode("utf-8"))
    except AttributeError:
        sys.stdout.write(text)


def _read_meta(run_dir: Path) -> dict[str, Any] | None:
    meta_path = run_dir / "meta.json"
    try:
        text = meta_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _write_meta(run_dir: Path, meta: dict[str, Any]) -> None:
    try:
        (run_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        out = (proc.stdout or "").strip()
        return str(pid) in out and "INFO:" not in out.upper()
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _kill_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        os.killpg(os.getpgid(pid), 15)
    except (OSError, ProcessLookupError):
        pass


def _output_has_json(output_path: Path) -> bool:
    if not output_path.exists():
        return False
    try:
        json.loads(output_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return False
    return True


def _read_stderr_tail(stderr_path: Path) -> str:
    try:
        text = stderr_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-MAX_STARTUP_STDERR_CHARS:].strip()


def _probe_startup(proc: subprocess.Popen, run_dir: Path) -> dict[str, Any] | None:
    """Report a startup failure, or ``None`` when the run looks healthy.

    Exiting inside the probe window is not a failure on its own: the fake
    Codex the test suite runs against answers in milliseconds. Only a process
    that is already gone *and* left no parseable JSON behind counts as one.
    """
    try:
        exit_code = proc.wait(timeout=STARTUP_PROBE_SECONDS)
    except subprocess.TimeoutExpired:
        return None
    except OSError as exc:
        return {"exit_code": -1, "stderr_tail": f"{exc.__class__.__name__}"}
    if _output_has_json(run_dir / "output.json"):
        return None
    return {
        "exit_code": exit_code,
        "stderr_tail": _read_stderr_tail(run_dir / "stderr.log"),
    }


def _resolve_status(run_dir: Path, meta: dict[str, Any]) -> tuple[str, bool]:
    """Returns (status, pid_alive). Updates meta in place when terminal."""
    pid = int(meta.get("pid") or 0)
    pid_alive = _is_pid_alive(pid)
    if pid_alive:
        return "running", True

    cancelled = (run_dir / "cancelled.flag").exists()
    output_has_json = _output_has_json(run_dir / "output.json")

    if cancelled:
        status = "cancelled"
    elif output_has_json:
        status = "done"
    else:
        status = "error"

    if meta.get("status") != status:
        meta["status"] = status
        meta.setdefault("finished_at", _now_iso())
    return status, False


def _list_active_run_ids() -> list[str]:
    if not CACHE_DIR.exists():
        return []
    active: list[str] = []
    for run_dir in CACHE_DIR.iterdir():
        if not run_dir.is_dir():
            continue
        meta = _read_meta(run_dir)
        if not meta:
            continue
        if meta.get("status") != "running":
            continue
        if _is_pid_alive(int(meta.get("pid") or 0)):
            active.append(run_dir.name)
    return active


def _cleanup_old_runs(max_age_days: int = DEFAULT_CLEANUP_MAX_AGE_DAYS) -> None:
    if not CACHE_DIR.exists():
        return
    cutoff = time.time() - max_age_days * 86400
    for run_dir in CACHE_DIR.iterdir():
        if not run_dir.is_dir():
            continue
        meta = _read_meta(run_dir)
        if not meta:
            continue
        if meta.get("status") not in TERMINAL_STATUSES:
            # Re-check liveness; if process is dead but meta is stale, skip
            # cleanup for this round and let a status() call fix it.
            if _is_pid_alive(int(meta.get("pid") or 0)):
                continue
        try:
            mtime = run_dir.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            shutil.rmtree(run_dir, ignore_errors=True)


def _resolve_max_concurrent(cli_value: int | None) -> int:
    if cli_value is not None:
        return max(1, int(cli_value))
    env_value = os.environ.get("CODEX_BG_MAX_CONCURRENT")
    if env_value:
        try:
            return max(1, int(env_value))
        except ValueError:
            pass
    return DEFAULT_MAX_CONCURRENT


# --------------------------------------------------------------- subcommands


def cmd_start(args: argparse.Namespace) -> int:
    _cleanup_old_runs()

    if args.mode not in VALID_MODES:
        _emit(
            {
                "status": "error",
                "reason": "invalid_mode",
                "mode": args.mode,
                "valid_modes": list(VALID_MODES),
                "hint": t("bg.hint.invalid_mode"),
            }
        )
        return 0

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    max_conc = _resolve_max_concurrent(args.max_concurrent)
    active = _list_active_run_ids()
    if len(active) >= max_conc:
        _emit(
            {
                "status": "error",
                "reason": "max_concurrent_reached",
                "active_run_ids": active,
                "limit": max_conc,
            }
        )
        return 0

    run_id = uuid.uuid4().hex[:12]
    run_dir = CACHE_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    output_path = run_dir / "output.json"
    stderr_path = run_dir / "stderr.log"

    wrapper_args = list(args.passthrough or [])
    cwd = args.cwd or os.getcwd()
    if "--cwd" not in wrapper_args:
        wrapper_args.extend(["--cwd", cwd])

    cmd = [_resolve_python(), str(WRAPPER), args.mode, *wrapper_args]

    try:
        out_f = open(output_path, "wb", buffering=0)
        err_f = open(stderr_path, "wb", buffering=0)
    except OSError as exc:
        shutil.rmtree(run_dir, ignore_errors=True)
        _emit({"status": "error", "reason": f"cannot_open_logs: {exc.__class__.__name__}"})
        return 0

    creationflags = 0
    start_new_session = False
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        start_new_session = True

    try:
        # close_fds is left at the platform default. On Windows, that means
        # only stdin/stdout/stderr handles passed here are inherited; all
        # other handles from this script (including its own stdout) stay
        # private. Without this, a parent subprocess.run() call blocks
        # waiting for our stdout to drain because the child wrapper
        # inherits it indirectly.
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=out_f,
            stderr=err_f,
            cwd=cwd,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
    except (OSError, FileNotFoundError) as exc:
        out_f.close()
        err_f.close()
        shutil.rmtree(run_dir, ignore_errors=True)
        _emit({"status": "error", "reason": f"spawn_failed: {exc.__class__.__name__}"})
        return 0
    finally:
        try:
            out_f.close()
        except OSError:
            pass
        try:
            err_f.close()
        except OSError:
            pass

    started_at = _now_iso()
    meta = {
        "run_id": run_id,
        "mode": args.mode,
        "cwd": cwd,
        "args": wrapper_args,
        "pid": proc.pid,
        "started_at": started_at,
        "finished_at": None,
        "status": "running",
    }

    startup_failure = _probe_startup(proc, run_dir)
    if startup_failure is not None:
        meta["status"] = "error"
        meta["finished_at"] = _now_iso()
        _write_meta(run_dir, meta)
        _emit(
            {
                "status": "error",
                "reason": "died_on_startup",
                "run_id": run_id,
                "mode": args.mode,
                "exit_code": startup_failure["exit_code"],
                "stderr_tail": startup_failure["stderr_tail"],
                "hint": t("bg.hint.died_on_startup"),
            }
        )
        return 0

    _write_meta(run_dir, meta)

    _emit(
        {
            "status": "ok",
            "run_id": run_id,
            "pid": proc.pid,
            "started_at": started_at,
            "mode": args.mode,
        }
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _cleanup_old_runs()
    run_dir = CACHE_DIR / args.run_id
    meta = _read_meta(run_dir) if run_dir.exists() else None
    if meta is None:
        _emit({"status": "error", "reason": "not_found", "run_id": args.run_id})
        return 0

    status, pid_alive = _resolve_status(run_dir, meta)
    if status in TERMINAL_STATUSES and meta.get("status") != "running":
        # already reflected; no rewrite needed unless missing finished_at
        if meta.get("finished_at") is None:
            meta["finished_at"] = _now_iso()
            _write_meta(run_dir, meta)
    elif status in TERMINAL_STATUSES:
        _write_meta(run_dir, meta)

    _emit(
        {
            "status": status,
            "run_id": args.run_id,
            "mode": meta.get("mode"),
            "pid": meta.get("pid"),
            "pid_alive": pid_alive,
            "started_at": meta.get("started_at"),
            "finished_at": meta.get("finished_at"),
        }
    )
    return 0


def cmd_output(args: argparse.Namespace) -> int:
    _cleanup_old_runs()
    run_dir = CACHE_DIR / args.run_id
    meta = _read_meta(run_dir) if run_dir.exists() else None
    if meta is None:
        _emit({"status": "error", "reason": "not_found", "run_id": args.run_id})
        return 0

    status, _alive = _resolve_status(run_dir, meta)
    if status == "running":
        _emit({"status": "error", "reason": "still_running", "run_id": args.run_id})
        return 0

    output_path = run_dir / "output.json"
    if not output_path.exists() or output_path.stat().st_size == 0:
        _emit({"status": "error", "reason": "no_output", "run_id": args.run_id})
        return 0

    try:
        text = output_path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        _emit(
            {
                "status": "error",
                "reason": f"unreadable_output: {exc.__class__.__name__}",
                "run_id": args.run_id,
            }
        )
        return 0

    if not isinstance(data, dict):
        _emit({"status": "error", "reason": "output_not_object", "run_id": args.run_id})
        return 0

    _emit(data)
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    _cleanup_old_runs()
    run_dir = CACHE_DIR / args.run_id
    meta = _read_meta(run_dir) if run_dir.exists() else None
    if meta is None:
        _emit({"status": "error", "reason": "not_found", "run_id": args.run_id})
        return 0

    pid = int(meta.get("pid") or 0)
    was_alive = _is_pid_alive(pid)
    _kill_process_tree(pid)
    try:
        (run_dir / "cancelled.flag").write_text("", encoding="utf-8")
    except OSError:
        pass

    # Update meta so subsequent status calls see "cancelled" deterministically.
    meta["status"] = "cancelled"
    meta.setdefault("finished_at", _now_iso())
    _write_meta(run_dir, meta)

    _emit(
        {
            "status": "ok",
            "run_id": args.run_id,
            "cancelled": True,
            "was_alive": was_alive,
        }
    )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    _cleanup_old_runs()
    runs: list[dict[str, Any]] = []
    if CACHE_DIR.exists():
        for run_dir in CACHE_DIR.iterdir():
            if not run_dir.is_dir():
                continue
            meta = _read_meta(run_dir)
            if not meta:
                continue
            status, _alive = _resolve_status(run_dir, meta)
            runs.append(
                {
                    "run_id": run_dir.name,
                    "mode": meta.get("mode"),
                    "status": status,
                    "started_at": meta.get("started_at"),
                    "finished_at": meta.get("finished_at"),
                    "pid": meta.get("pid"),
                }
            )

    runs.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    limit = max(1, int(args.limit or LIST_DEFAULT_LIMIT))
    _emit({"status": "ok", "runs": runs[:limit], "count": len(runs)})
    return 0


# --------------------------------------------------------------------- main


def _parse_cli(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=t("bg.cli.description"))
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_start = sub.add_parser("start", help=t("bg.cli.help.start"))
    p_start.add_argument("mode", help=t("bg.cli.help.mode"))
    p_start.add_argument("--cwd", default=None, help=t("bg.cli.help.cwd"))
    p_start.add_argument(
        "--max-concurrent",
        type=int,
        default=None,
        help=t("bg.cli.help.max_concurrent", default=DEFAULT_MAX_CONCURRENT),
    )
    # Wrapper-specific flags (--last-message-file, --task-file, --target-path,
    # --reasoning-effort, etc.) are forwarded as-is. We collect them via
    # parse_known_args below; using nargs=REMAINDER would otherwise eat our
    # own flags like --max-concurrent.

    p_status = sub.add_parser("status", help=t("bg.cli.help.status"))
    p_status.add_argument("run_id")

    p_output = sub.add_parser("output", help=t("bg.cli.help.output"))
    p_output.add_argument("run_id")

    p_cancel = sub.add_parser("cancel", help=t("bg.cli.help.cancel"))
    p_cancel.add_argument("run_id")

    p_list = sub.add_parser("list", help=t("bg.cli.help.list"))
    p_list.add_argument("--limit", type=int, default=LIST_DEFAULT_LIMIT)

    args, unknown = parser.parse_known_args(argv)
    args.passthrough = unknown if args.subcommand == "start" else []
    if args.subcommand != "start" and unknown:
        # Non-start subcommands shouldn't carry stray args; surface them as
        # an error rather than silently swallow.
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    return args


_PASSIVE_SUBCOMMANDS = ("status", "output", "cancel", "list")


def main(argv: list[str] | None = None) -> int:
    args = _parse_cli(argv)
    if args.subcommand not in _PASSIVE_SUBCOMMANDS:
        ensure_setup_complete()
    handler = {
        "start": cmd_start,
        "status": cmd_status,
        "output": cmd_output,
        "cancel": cmd_cancel,
        "list": cmd_list,
    }[args.subcommand]
    try:
        rc = handler(args)
    except Exception as exc:  # never raise; same contract as the wrapper
        _emit(
            {
                "status": "error",
                "reason": f"internal_error: {exc.__class__.__name__}",
                "subcommand": args.subcommand,
            }
        )
        return EXIT_ERROR
    # Handlers report through the emitted JSON; the exit code mirrors it so a
    # caller that only checks the exit status cannot read a refusal as a live
    # run. Only "error" is a failure here: status/list legitimately emit run
    # states like "running" or "cancelled".
    if rc:
        return rc
    return EXIT_ERROR if _last_emitted_status == "error" else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
