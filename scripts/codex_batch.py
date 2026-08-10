"""Synchronous batch runner for ``codex ask`` / ``delegate`` / ``plan-review``.

Three sub-modes:

  * ``batch-ask`` — fan-out of read-only questions. Failure of one item never
    cancels the others; the aggregator marks the run as ``partial`` when any
    item failed.
  * ``batch-delegate`` — fan-out of write-capable tasks. Each item must
    declare an explicit ``write_set`` (list of paths). The batcher rejects
    overlapping write-sets up front, then validates that each item's
    reported file changes stayed inside its declared write-set; otherwise
    the item is marked ``write_set_violated=true``.
  * ``batch-plan-review`` — fan-out of plan slices, normally the output of
    ``split_plan_by_phase.py``. Each item runs the wrapper in the real
    ``plan-review`` mode, so every slice gets the review checklist and the
    per-mode reasoning effort; routing this through ``ask`` would be faster
    and much weaker. On top of ``items`` the result carries an ``aggregate``
    block whose findings are deduplicated across slices.

Input is a JSON file::

    {
        "max_parallel": 4,
        "tasks": [
            { "id": "t1", "question": "..."  },
            { "id": "t2", "question": "...", "cwd": "..." }
        ]
    }

For ``batch-delegate`` each task uses ``"task"`` and ``"write_set"`` keys
instead of ``"question"``; for ``batch-plan-review`` it uses ``"plan_file"``.

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
import threading
import time
import uuid
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
WRAPPER = BIN_DIR / "invoke_codex_with_claude.py"

sys.path.insert(0, str(BIN_DIR))
from codex_config import (  # noqa: E402
    EXIT_ERROR,
    EXIT_OK,
    ensure_setup_complete,
    subprocess_timeout_for,
    t,
)
from codex_config import (
    get as _config_get,
)
from codex_config import (
    resolve_python as _resolve_python,
)

DEFAULT_MAX_PARALLEL = int(_config_get("batch.default_max_parallel", 4))
MAX_PARALLEL_CEILING = int(_config_get("batch.max_parallel_ceiling", 8))
ITEM_SAFETY_TIMEOUT = int(_config_get("batch.item_safety_timeout_seconds", 900))

SUB_MODES = ("batch-ask", "batch-delegate", "batch-plan-review")
WRAPPER_MODE_BY_SUB_MODE = {
    "batch-ask": "ask",
    "batch-delegate": "delegate",
    "batch-plan-review": "plan-review",
}
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def _normalize_paths(paths: list[str], base: Path) -> list[Path]:
    out: list[Path] = []
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


def _validate_disjoint_write_sets(tasks: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Return None when no overlap, else a list of overlap descriptors.

    Each descriptor: {ids: [a, b], path: <conflict>}.
    """
    items: list[tuple[str, list[Path]]] = []
    for task in tasks:
        cwd = Path(task.get("cwd") or os.getcwd()).resolve()
        write_set = _normalize_paths(task.get("write_set") or [], cwd)
        items.append((str(task.get("id") or ""), write_set))

    overlaps: list[dict[str, Any]] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a_id, a_paths = items[i]
            b_id, b_paths = items[j]
            common = set(a_paths) & set(b_paths)
            for path in common:
                overlaps.append({"ids": [a_id, b_id], "path": str(path)})
    return overlaps or None


def _check_write_set_violation(declared: list[Path], reported: dict[str, list[str]], base: Path) -> bool:
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
    sub_mode: str,
    task: dict[str, Any],
    batch_id: str,
    quota_stop: threading.Event | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run one wrapper invocation. Returns (id, result_dict)."""
    task_id = str(task.get("id") or uuid.uuid4().hex[:8])
    cwd = task.get("cwd") or os.getcwd()
    started = time.monotonic()

    # Every item draws on the same Codex quota, so once one item reports it
    # exhausted the rest cannot succeed. Starting them anyway only makes the
    # user wait longer for the same failure.
    if quota_stop is not None and quota_stop.is_set():
        return task_id, {
            "status": "error",
            "summary": t("batch.summary.skipped_quota"),
            "error_class": "quota_exhausted",
            "duration_seconds": 0.0,
            "result": {},
        }

    payload_dir = Path(tempfile.mkdtemp(prefix=f"codex-batch-{batch_id}-"))
    try:
        if sub_mode == "batch-plan-review":
            # Deliberately the real plan-review mode, not ask: only plan-review
            # carries the review checklist and its per-mode reasoning effort.
            cmd = [
                _resolve_python(),
                str(WRAPPER),
                "plan-review",
                "--cwd",
                str(cwd),
                "--last-message-file",
                str(task.get("plan_file") or ""),
            ]
        elif sub_mode == "batch-ask":
            question = task.get("question") or ""
            q_path = payload_dir / f"{task_id}.txt"
            q_path.write_text(question, encoding="utf-8")
            cmd = [
                _resolve_python(),
                str(WRAPPER),
                "ask",
                "--cwd",
                str(cwd),
                "--question-file",
                str(q_path),
            ]
        else:  # batch-delegate
            task_text = task.get("task") or ""
            t_path = payload_dir / f"{task_id}.txt"
            t_path.write_text(task_text, encoding="utf-8")
            cmd = [
                _resolve_python(),
                str(WRAPPER),
                "delegate",
                "--cwd",
                str(cwd),
                "--task-file",
                str(t_path),
            ]
        target = task.get("target_path")
        if target:
            cmd += ["--target-path", str(target)]
        effort = task.get("reasoning_effort")
        if effort:
            cmd += ["--reasoning-effort", str(effort)]

        env = os.environ.copy()
        env["CODEX_WRAPPER_DISABLE_HEARTBEAT"] = "1"
        env["CODEX_BATCH_ID"] = batch_id
        env["CODEX_BATCH_ITEM_ID"] = task_id

        item_timeout = max(ITEM_SAFETY_TIMEOUT, subprocess_timeout_for(WRAPPER_MODE_BY_SUB_MODE[sub_mode]))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=item_timeout,
            )
        except subprocess.TimeoutExpired:
            return task_id, {
                "status": "error",
                "summary": t("batch.summary.timeout", timeout=item_timeout),
                "duration_seconds": time.monotonic() - started,
                "result": {},
            }

        try:
            wrapper_result = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            wrapper_result = {}

        if wrapper_result.get("error_class") == "quota_exhausted" and quota_stop is not None:
            quota_stop.set()

        item: dict[str, Any] = {
            "status": wrapper_result.get("status", "error"),
            "error_class": wrapper_result.get("error_class", ""),
            "summary": wrapper_result.get("summary", "")[:500],
            "duration_seconds": time.monotonic() - started,
            "result": wrapper_result,
        }
        if sub_mode == "batch-delegate":
            declared = _normalize_paths(task.get("write_set") or [], Path(cwd).resolve())
            reported = {k: wrapper_result.get(k) or [] for k in ("files_created", "files_edited", "files_deleted")}
            item["write_set"] = [str(p) for p in declared]
            item["write_set_violated"] = _check_write_set_violation(declared, reported, Path(cwd).resolve())
        return task_id, item
    finally:
        try:
            for child in payload_dir.iterdir():
                child.unlink(missing_ok=True)
            payload_dir.rmdir()
        except OSError:
            pass


def _finding_key(finding: dict[str, Any]) -> tuple[str, str]:
    return (
        " ".join((finding.get("title") or "").lower().split()),
        " ".join((finding.get("location") or "").lower().split()),
    )


def _aggregate_findings(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge per-slice reviews into one result.

    The same defect reported from two slices is one defect: dedupe by
    (title, location) and keep the highest severity seen, recording every
    slice that raised it. Coverage is the union of the categories swept, so
    the count still means "categories reviewed", not "categories with
    findings".
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    coverage: dict[str, int] = {}
    block_recommended = False
    severity = "low"

    for item in items:
        result = item.get("result") or {}
        slice_id = str(item.get("id") or "")
        if result.get("block_recommended"):
            block_recommended = True
        if SEVERITY_ORDER.get(result.get("severity") or "low", 0) > SEVERITY_ORDER.get(severity, 0):
            severity = result.get("severity") or "low"

        for entry in result.get("coverage") or []:
            if not isinstance(entry, dict):
                continue
            category = str(entry.get("category") or "")
            if not category:
                continue
            coverage[category] = coverage.get(category, 0) + int(entry.get("findings_count") or 0)

        for finding in result.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            key = _finding_key(finding)
            existing = merged.get(key)
            if existing is None:
                clone = dict(finding)
                clone["slices"] = [slice_id]
                merged[key] = clone
                continue
            if slice_id and slice_id not in existing["slices"]:
                existing["slices"].append(slice_id)
            if SEVERITY_ORDER.get(finding.get("severity") or "low", 0) > SEVERITY_ORDER.get(
                existing.get("severity") or "low", 0
            ):
                existing["severity"] = finding.get("severity")

    findings = sorted(
        merged.values(),
        key=lambda f: (-SEVERITY_ORDER.get(f.get("severity") or "low", 0), f.get("title") or ""),
    )
    return {
        "severity": severity,
        "block_recommended": block_recommended,
        "findings": findings,
        "coverage": [{"category": c, "findings_count": n} for c, n in sorted(coverage.items())],
        "duplicates_merged": sum(len(f["slices"]) for f in findings) - len(findings),
    }


def _run_batch(sub_mode: str, batch: dict[str, Any]) -> dict[str, Any]:
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
    max_parallel = max(1, min(max_parallel, MAX_PARALLEL_CEILING))

    batch_id = uuid.uuid4().hex[:10]

    items: list[dict[str, Any]] = []
    quota_stop = threading.Event()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {pool.submit(_run_wrapper, sub_mode, task, batch_id, quota_stop): task for task in tasks}
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

    summary_parts: list[str] = []
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
    quota_hit = sum(1 for i in items if i.get("error_class") == "quota_exhausted")
    if quota_hit:
        summary_parts.append(f"{quota_hit} quota_exhausted")

    payload: dict[str, Any] = {
        "status": agg_status,
        "batch_id": batch_id,
        "summary": "; ".join(summary_parts),
        "partial": agg_status == "partial",
        "quota_exhausted": bool(quota_hit),
        "items": items,
    }
    if sub_mode == "batch-plan-review":
        payload["aggregate"] = _aggregate_findings(items)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=t("batch.cli.description"))
    parser.add_argument(
        "sub_mode",
        choices=list(SUB_MODES),
        help=t("batch.cli.help.sub_mode"),
    )
    parser.add_argument("--input-file", required=True, help=t("batch.cli.help.input_file"))
    args = parser.parse_args(argv)
    # Both batch sub-modes (batch-ask, batch-delegate) are active and
    # invoke Codex; no passive subcommand here.
    ensure_setup_complete()

    try:
        payload = json.loads(Path(args.input_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "summary": t("batch.summary.read_failure", exc=exc.__class__.__name__),
                },
                ensure_ascii=False,
            )
        )
        return EXIT_ERROR

    result = _run_batch(args.sub_mode, payload)
    print(json.dumps(result, ensure_ascii=False))
    # "partial" is a failure for exit-code purposes: some item was not done,
    # and a caller reading only the exit code has to notice that.
    return EXIT_OK if result.get("status") == "ok" else EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
