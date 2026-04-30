"""Synchronous batch runner for ``codex ask`` / ``codex delegate``.

Two sub-modes:

  * ``batch-ask`` — fan-out of read-only questions. Failure of one item never
    cancels the others; the aggregator marks the run as ``partial`` when any
    item failed.
  * ``batch-delegate`` — fan-out of write-capable tasks. Each item must
    declare an explicit ``write_set`` (list of paths). The batcher rejects
    overlapping write-sets up front, then validates that each item's
    reported file changes stayed inside its declared write-set; otherwise
    the item is marked ``write_set_violated=true``.

Input is a JSON file::

    {
        "max_parallel": 4,
        "tasks": [
            { "id": "t1", "question": "..."  },
            { "id": "t2", "question": "...", "cwd": "..." }
        ]
    }

For ``batch-delegate`` each task uses ``"task"`` and ``"write_set"`` keys
instead of ``"question"``.

Output is a JSON object on stdout::

    {
        "status": "ok" | "partial" | "error",
        "items": [ { id, status, summary, duration_seconds, result, ... } ]
    }

Telemetry rows for each child run are written by the underlying wrapper
(``cache/runs.jsonl``); this script also emits one ``batch_id`` summary row
through the wrapper-private channel so the runs can be correlated.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BIN_DIR = Path(__file__).resolve().parent
WRAPPER = BIN_DIR / "invoke_codex_with_claude.py"

sys.path.insert(0, str(BIN_DIR))
from codex_config import get as _config_get, t  # noqa: E402

DEFAULT_MAX_PARALLEL = int(_config_get("batch.default_max_parallel", 4))
MAX_PARALLEL_CEILING = int(_config_get("batch.max_parallel_ceiling", 8))
ITEM_SAFETY_TIMEOUT = int(_config_get("batch.item_safety_timeout_seconds", 900))


def _resolve_python() -> str:
    for env_name in ("SKILLS_PYTHON", "CLAUDE_AUTOMATION_PYTHON"):
        candidate = os.environ.get(env_name)
        if candidate and Path(candidate).exists():
            return candidate
    return sys.executable


def _normalize_paths(paths: List[str], base: Path) -> List[Path]:
    out: List[Path] = []
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            continue
        candidate = Path(p)
        if not candidate.is_absolute():
            candidate = base / candidate
        try:
            out.append(candidate.resolve())
        except OSError:
            out.append(candidate)
    return out


def _validate_disjoint_write_sets(
    tasks: List[Dict[str, Any]]
) -> Optional[List[Dict[str, Any]]]:
    """Return None when no overlap, else a list of overlap descriptors.

    Each descriptor: {ids: [a, b], path: <conflict>}.
    """
    items: List[Tuple[str, List[Path]]] = []
    for task in tasks:
        cwd = Path(task.get("cwd") or os.getcwd()).resolve()
        write_set = _normalize_paths(task.get("write_set") or [], cwd)
        items.append((str(task.get("id") or ""), write_set))

    overlaps: List[Dict[str, Any]] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a_id, a_paths = items[i]
            b_id, b_paths = items[j]
            common = set(a_paths) & set(b_paths)
            for path in common:
                overlaps.append({"ids": [a_id, b_id], "path": str(path)})
    return overlaps or None


def _check_write_set_violation(
    declared: List[Path], reported: Dict[str, List[str]], base: Path
) -> bool:
    declared_set = set(declared)
    if not declared_set:
        return False
    for key in ("files_created", "files_edited", "files_deleted"):
        for path in reported.get(key) or []:
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = base / candidate
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            if resolved not in declared_set:
                return True
    return False


def _run_wrapper(
    sub_mode: str, task: Dict[str, Any], batch_id: str
) -> Tuple[str, Dict[str, Any]]:
    """Run one wrapper invocation. Returns (id, result_dict)."""
    task_id = str(task.get("id") or uuid.uuid4().hex[:8])
    cwd = task.get("cwd") or os.getcwd()
    started = time.monotonic()

    payload_dir = Path(tempfile.mkdtemp(prefix=f"codex-batch-{batch_id}-"))
    try:
        if sub_mode == "batch-ask":
            question = task.get("question") or ""
            q_path = payload_dir / f"{task_id}.txt"
            q_path.write_text(question, encoding="utf-8")
            cmd = [
                _resolve_python(), str(WRAPPER), "ask",
                "--cwd", str(cwd),
                "--question-file", str(q_path),
            ]
        else:  # batch-delegate
            task_text = task.get("task") or ""
            t_path = payload_dir / f"{task_id}.txt"
            t_path.write_text(task_text, encoding="utf-8")
            cmd = [
                _resolve_python(), str(WRAPPER), "delegate",
                "--cwd", str(cwd),
                "--task-file", str(t_path),
            ]
        target = task.get("target_path")
        if target:
            cmd += ["--target-path", str(target)]

        env = os.environ.copy()
        env["CODEX_WRAPPER_DISABLE_HEARTBEAT"] = "1"
        env["CODEX_BATCH_ID"] = batch_id
        env["CODEX_BATCH_ITEM_ID"] = task_id

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=env, timeout=900,
            )
        except subprocess.TimeoutExpired:
            return task_id, {
                "status": "error",
                "summary": t("batch.summary.timeout", timeout=ITEM_SAFETY_TIMEOUT),
                "duration_seconds": time.monotonic() - started,
                "result": {},
            }

        try:
            wrapper_result = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            wrapper_result = {}

        item: Dict[str, Any] = {
            "status": wrapper_result.get("status", "error"),
            "summary": wrapper_result.get("summary", "")[:500],
            "duration_seconds": time.monotonic() - started,
            "result": wrapper_result,
        }
        if sub_mode == "batch-delegate":
            declared = _normalize_paths(task.get("write_set") or [], Path(cwd).resolve())
            reported = {
                k: wrapper_result.get(k) or []
                for k in ("files_created", "files_edited", "files_deleted")
            }
            item["write_set"] = [str(p) for p in declared]
            item["write_set_violated"] = _check_write_set_violation(
                declared, reported, Path(cwd).resolve()
            )
        return task_id, item
    finally:
        try:
            for child in payload_dir.iterdir():
                child.unlink(missing_ok=True)
            payload_dir.rmdir()
        except OSError:
            pass


def _run_batch(sub_mode: str, batch: Dict[str, Any]) -> Dict[str, Any]:
    tasks = batch.get("tasks") or []
    if not isinstance(tasks, list) or not tasks:
        return {
            "status": "error",
            "summary": t("batch.summary.no_tasks"),
            "items": [],
        }

    if sub_mode == "batch-delegate":
        overlaps = _validate_disjoint_write_sets(tasks)
        if overlaps:
            return {
                "status": "error",
                "summary": t("batch.summary.overlap"),
                "overlaps": overlaps,
                "items": [],
            }

    max_parallel = int(batch.get("max_parallel") or DEFAULT_MAX_PARALLEL)
    max_parallel = max(1, min(max_parallel, 8))

    batch_id = uuid.uuid4().hex[:10]

    items: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {
            pool.submit(_run_wrapper, sub_mode, task, batch_id): task
            for task in tasks
        }
        for future in concurrent.futures.as_completed(futures):
            task = futures[future]
            try:
                task_id, item = future.result()
            except Exception as exc:
                task_id = str(task.get("id") or "?")
                item = {
                    "status": "error",
                    "summary": t("batch.summary.worker_crash", exc=exc.__class__.__name__),
                    "duration_seconds": 0.0,
                    "result": {},
                }
            item["id"] = task_id
            items.append(item)

    items.sort(key=lambda x: str(x.get("id")))

    statuses = {item.get("status") for item in items}
    if any(s == "error" for s in statuses):
        agg_status = "partial" if any(s == "ok" for s in statuses) else "error"
    else:
        agg_status = "ok"

    summary_parts: List[str] = []
    ok_count = sum(1 for i in items if i.get("status") == "ok")
    err_count = sum(1 for i in items if i.get("status") == "error")
    needs_count = sum(1 for i in items if i.get("status") == "needs_input")
    summary_parts.append(f"{ok_count}/{len(items)} ok")
    if err_count:
        summary_parts.append(f"{err_count} error")
    if needs_count:
        summary_parts.append(f"{needs_count} needs_input")
    if sub_mode == "batch-delegate":
        violated = sum(1 for i in items if i.get("write_set_violated"))
        if violated:
            summary_parts.append(f"{violated} write_set_violated")

    return {
        "status": agg_status,
        "batch_id": batch_id,
        "summary": "; ".join(summary_parts),
        "partial": agg_status == "partial",
        "items": items,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=t("batch.cli.description"))
    parser.add_argument(
        "sub_mode",
        choices=["batch-ask", "batch-delegate"],
        help=t("batch.cli.help.sub_mode"),
    )
    parser.add_argument("--input-file", required=True, help=t("batch.cli.help.input_file"))
    args = parser.parse_args(argv)

    try:
        payload = json.loads(Path(args.input_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({
            "status": "error",
            "summary": t("batch.summary.read_failure", exc=exc.__class__.__name__),
        }, ensure_ascii=False))
        return 0

    result = _run_batch(args.sub_mode, payload)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
