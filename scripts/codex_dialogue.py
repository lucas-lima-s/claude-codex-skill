"""Iterative multi-turn plan-review dialogue.

Drives a Claude ↔ Codex back-and-forth where each turn:

  1. Claude provides a plan revision (turn 1 = original plan).
  2. The dialogue runner invokes ``invoke_codex_with_claude.py plan-review``
     with the plan + a compact history of past turns.
  3. The wrapper output is persisted next to the plan in the dialogue
     state directory.

Subcommands::

    start --plan-file <path> [--max-turns N] [--cwd ...]
        Initialise a new dialogue. Returns ``{dialogue_id, turn, status,
        findings, summary}``.

    next-turn --dialogue-id <id> --plan-file <path>
        Submit a revised plan for the next turn. Returns the new turn's
        review (same shape as ``start``).

    status --dialogue-id <id>
        Inspect dialogue state and stop signals.

    finish --dialogue-id <id>
        Consolidate the dialogue: write ``final_plan.md`` (last plan) and
        ``dialogue_log.md`` (delta + per-turn summary). Returns the paths.

    abort --dialogue-id <id>
        Mark the dialogue as aborted by the user.

State layout (``$TEMP/codex_dialogue_<id>/``)::

    meta.json              dialogue metadata (turns, status, max_turns, ...)
    turn_<N>_plan.md       plan submitted at turn N
    turn_<N>_findings.json wrapper canonical output for turn N
    final_plan.md          (after finish) last submitted plan
    dialogue_log.md        (after finish) human-readable consolidation

Stop signals reported by ``status``:
- ``converged``: 2 consecutive turns with empty findings + severity=low
                 + block_recommended=false.
- ``limit``: ``current_turn >= max_turns``.
- ``divergence``: same finding (matched by ``title + location``) with
                  severity=high in 2 consecutive turns.
- ``aborted``: ``abort`` was called.

The runner never raises: every error path emits ``status=error`` JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BIN_DIR = Path(__file__).resolve().parent
WRAPPER = BIN_DIR / "invoke_codex_with_claude.py"

DIALOGUE_PREFIX = "codex_dialogue_"
DEFAULT_MAX_TURNS = 3
MIN_MAX_TURNS = 1
MAX_MAX_TURNS = 20
MAX_HISTORY_BYTES = 8 * 1024  # cap for the inline history we feed Codex


def _resolve_python() -> str:
    for env_name in ("SKILLS_PYTHON", "CLAUDE_AUTOMATION_PYTHON"):
        candidate = os.environ.get(env_name)
        if candidate and Path(candidate).exists():
            return candidate
    return sys.executable


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _emit(payload: Dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    try:
        sys.stdout.buffer.write(text.encode("utf-8"))
    except AttributeError:
        sys.stdout.write(text)


def _resolve_max_turns(cli_value: Optional[int]) -> int:
    if cli_value is not None:
        candidate = int(cli_value)
    else:
        env_value = os.environ.get("CODEX_DIALOGUE_MAX_TURNS")
        try:
            candidate = int(env_value) if env_value else DEFAULT_MAX_TURNS
        except ValueError:
            candidate = DEFAULT_MAX_TURNS
    return max(MIN_MAX_TURNS, min(MAX_MAX_TURNS, candidate))


def _dialogue_dir(dialogue_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"{DIALOGUE_PREFIX}{dialogue_id}"


def _read_meta(dialogue_dir: Path) -> Optional[Dict[str, Any]]:
    meta_path = dialogue_dir / "meta.json"
    try:
        text = meta_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _write_meta(dialogue_dir: Path, meta: Dict[str, Any]) -> None:
    try:
        (dialogue_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _read_findings(dialogue_dir: Path, turn: int) -> Optional[Dict[str, Any]]:
    path = dialogue_dir / f"turn_{turn}_findings.json"
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _write_findings(dialogue_dir: Path, turn: int, data: Dict[str, Any]) -> None:
    try:
        (dialogue_dir / f"turn_{turn}_findings.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _summarise_turn(turn: int, findings_payload: Dict[str, Any]) -> str:
    """Produce a short pt-BR summary of a turn for the inline history feed."""
    severity = findings_payload.get("severity", "low")
    findings = findings_payload.get("findings") or []
    summary = findings_payload.get("summary", "") or ""
    summary_short = summary[:240] + ("…" if len(summary) > 240 else "")
    titles = []
    for f in findings[:5]:
        if not isinstance(f, dict):
            continue
        title = (f.get("title") or "").strip()
        sev = (f.get("severity") or "").strip()
        loc = (f.get("location") or "").strip()
        bits = [b for b in [sev, title, loc] if b]
        titles.append(" / ".join(bits) if bits else "<sem título>")
    bullets = "\n".join(f"  - {t}" for t in titles) or "  - (nenhum)"
    return (
        f"Turno {turn} (severity={severity}, {len(findings)} finding(s))\n"
        f"  Resumo: {summary_short}\n"
        f"  Findings:\n{bullets}"
    )


def _build_history(dialogue_dir: Path, up_to_turn: int) -> str:
    parts: List[str] = []
    for n in range(1, up_to_turn + 1):
        f = _read_findings(dialogue_dir, n)
        if not f:
            continue
        parts.append(_summarise_turn(n, f))
    text = "\n\n".join(parts)
    if len(text.encode("utf-8")) > MAX_HISTORY_BYTES:
        # Trim from the oldest turns first; Codex cares most about the
        # immediately preceding turn.
        trimmed: List[str] = []
        running = 0
        for piece in reversed(parts):
            piece_bytes = len(piece.encode("utf-8"))
            if running + piece_bytes > MAX_HISTORY_BYTES:
                break
            trimmed.insert(0, piece)
            running += piece_bytes
        text = "\n\n".join(trimmed)
    return text


def _stop_signal(dialogue_dir: Path, meta: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    """Return (stop_reason, details). stop_reason is None when we should
    keep going."""
    if meta.get("status") == "aborted":
        return "aborted", {}

    current_turn = int(meta.get("current_turn") or 0)
    max_turns = int(meta.get("max_turns") or DEFAULT_MAX_TURNS)
    if current_turn >= max_turns:
        return "limit", {"current_turn": current_turn, "max_turns": max_turns}

    # Convergence: 2 consecutive clean turns
    if current_turn >= 2:
        prev = _read_findings(dialogue_dir, current_turn - 1) or {}
        last = _read_findings(dialogue_dir, current_turn) or {}
        def _clean(p: Dict[str, Any]) -> bool:
            return (
                not (p.get("findings") or [])
                and (p.get("severity") or "low") == "low"
                and not p.get("block_recommended")
            )
        if _clean(prev) and _clean(last):
            return "converged", {}

    # Divergence: same {title, location} severity=high finding repeated
    if current_turn >= 2:
        prev = _read_findings(dialogue_dir, current_turn - 1) or {}
        last = _read_findings(dialogue_dir, current_turn) or {}
        def _high_keys(p: Dict[str, Any]) -> set:
            keys = set()
            for f in p.get("findings") or []:
                if not isinstance(f, dict):
                    continue
                if (f.get("severity") or "").lower() != "high":
                    continue
                keys.add(((f.get("title") or "").strip(),
                          (f.get("location") or "").strip()))
            return keys
        common_high = _high_keys(prev) & _high_keys(last)
        if common_high:
            sample = next(iter(common_high))
            return "divergence", {"finding": {"title": sample[0], "location": sample[1]}}

    return None, {}


def _invoke_wrapper_plan_review(
    plan_path: Path,
    cwd: Path,
    dialogue_dir: Path,
    history_text: str,
) -> Dict[str, Any]:
    """Run plan-review on the given plan, injecting prior history as
    transcript context. Returns the wrapper's canonical JSON.

    History is fed via a transcript file because that path already merges
    cleanly into the prompt builder of the wrapper.
    """
    transcript_path: Optional[Path] = None
    if history_text.strip():
        transcript_path = dialogue_dir / "history_inline.txt"
        try:
            transcript_path.write_text(history_text, encoding="utf-8")
        except OSError:
            transcript_path = None

    cmd = [
        _resolve_python(), str(WRAPPER), "plan-review",
        "--cwd", str(cwd),
        "--last-message-file", str(plan_path),
    ]
    if transcript_path is not None:
        cmd += ["--transcript-file", str(transcript_path)]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=600,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "summary": "wrapper timeout in dialogue turn",
            "findings": [],
            "block_recommended": False,
            "severity": "low",
        }

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {
            "status": "error",
            "summary": "wrapper produced unparseable JSON",
            "findings": [],
            "block_recommended": False,
            "severity": "low",
        }
    if not isinstance(data, dict):
        return {
            "status": "error",
            "summary": "wrapper returned non-object JSON",
            "findings": [],
            "block_recommended": False,
            "severity": "low",
        }
    return data


# --------------------------------------------------------------- subcommands

def cmd_start(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan_file)
    if not plan_path.is_file():
        _emit({"status": "error", "reason": "plan_file_not_found"})
        return 0

    cwd = Path(args.cwd or os.getcwd()).resolve()
    max_turns = _resolve_max_turns(args.max_turns)
    dialogue_id = uuid.uuid4().hex[:10]
    dialogue_dir = _dialogue_dir(dialogue_id)
    dialogue_dir.mkdir(parents=True, exist_ok=False)

    # Persist the plan as turn_1_plan.md
    plan_text = plan_path.read_text(encoding="utf-8", errors="replace")
    (dialogue_dir / "turn_1_plan.md").write_text(plan_text, encoding="utf-8")

    findings = _invoke_wrapper_plan_review(
        plan_path=plan_path, cwd=cwd, dialogue_dir=dialogue_dir,
        history_text="",
    )
    _write_findings(dialogue_dir, 1, findings)

    meta = {
        "dialogue_id": dialogue_id,
        "started_at": _now_iso(),
        "cwd": str(cwd),
        "max_turns": max_turns,
        "current_turn": 1,
        "status": "running",
    }
    _write_meta(dialogue_dir, meta)

    stop_reason, stop_details = _stop_signal(dialogue_dir, meta)
    _emit({
        "status": "ok",
        "dialogue_id": dialogue_id,
        "turn": 1,
        "max_turns": max_turns,
        "stop_reason": stop_reason,
        "stop_details": stop_details,
        "findings_payload": findings,
    })
    return 0


def cmd_next_turn(args: argparse.Namespace) -> int:
    dialogue_dir = _dialogue_dir(args.dialogue_id)
    meta = _read_meta(dialogue_dir)
    if meta is None:
        _emit({"status": "error", "reason": "dialogue_not_found"})
        return 0
    if meta.get("status") != "running":
        _emit({"status": "error", "reason": f"dialogue_status_{meta.get('status')}"})
        return 0

    plan_path = Path(args.plan_file)
    if not plan_path.is_file():
        _emit({"status": "error", "reason": "plan_file_not_found"})
        return 0

    next_turn = int(meta.get("current_turn") or 0) + 1
    plan_text = plan_path.read_text(encoding="utf-8", errors="replace")
    (dialogue_dir / f"turn_{next_turn}_plan.md").write_text(plan_text, encoding="utf-8")

    history_text = _build_history(dialogue_dir, up_to_turn=next_turn - 1)
    history_preface = (
        "Histórico do diálogo iterativo (turnos anteriores). "
        "Foque na revisão das mudanças do plano atual em relação ao turno anterior.\n\n"
        + history_text
    )

    cwd = Path(meta.get("cwd") or os.getcwd())
    findings = _invoke_wrapper_plan_review(
        plan_path=plan_path, cwd=cwd, dialogue_dir=dialogue_dir,
        history_text=history_preface,
    )
    _write_findings(dialogue_dir, next_turn, findings)

    meta["current_turn"] = next_turn
    _write_meta(dialogue_dir, meta)

    stop_reason, stop_details = _stop_signal(dialogue_dir, meta)
    _emit({
        "status": "ok",
        "dialogue_id": args.dialogue_id,
        "turn": next_turn,
        "max_turns": int(meta.get("max_turns") or DEFAULT_MAX_TURNS),
        "stop_reason": stop_reason,
        "stop_details": stop_details,
        "findings_payload": findings,
    })
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    dialogue_dir = _dialogue_dir(args.dialogue_id)
    meta = _read_meta(dialogue_dir)
    if meta is None:
        _emit({"status": "error", "reason": "dialogue_not_found"})
        return 0
    stop_reason, stop_details = _stop_signal(dialogue_dir, meta)
    _emit({
        "status": "ok",
        "dialogue_id": args.dialogue_id,
        "current_turn": int(meta.get("current_turn") or 0),
        "max_turns": int(meta.get("max_turns") or DEFAULT_MAX_TURNS),
        "dialogue_status": meta.get("status"),
        "stop_reason": stop_reason,
        "stop_details": stop_details,
        "started_at": meta.get("started_at"),
    })
    return 0


def cmd_abort(args: argparse.Namespace) -> int:
    dialogue_dir = _dialogue_dir(args.dialogue_id)
    meta = _read_meta(dialogue_dir)
    if meta is None:
        _emit({"status": "error", "reason": "dialogue_not_found"})
        return 0
    meta["status"] = "aborted"
    meta.setdefault("aborted_at", _now_iso())
    _write_meta(dialogue_dir, meta)
    _emit({"status": "ok", "dialogue_id": args.dialogue_id, "dialogue_status": "aborted"})
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    dialogue_dir = _dialogue_dir(args.dialogue_id)
    meta = _read_meta(dialogue_dir)
    if meta is None:
        _emit({"status": "error", "reason": "dialogue_not_found"})
        return 0

    current_turn = int(meta.get("current_turn") or 0)
    if current_turn < 1:
        _emit({"status": "error", "reason": "no_turns_completed"})
        return 0

    last_plan_path = dialogue_dir / f"turn_{current_turn}_plan.md"
    final_plan_path = dialogue_dir / "final_plan.md"
    try:
        shutil.copyfile(last_plan_path, final_plan_path)
    except OSError as exc:
        _emit({"status": "error", "reason": f"copy_failed: {exc.__class__.__name__}"})
        return 0

    log_lines: List[str] = []
    log_lines.append(f"# Dialogue log — {args.dialogue_id}")
    log_lines.append("")
    log_lines.append(f"Started: {meta.get('started_at')}")
    log_lines.append(f"Turns completed: {current_turn} / {meta.get('max_turns')}")
    stop_reason, stop_details = _stop_signal(dialogue_dir, meta)
    if stop_reason:
        log_lines.append(f"Stop reason: {stop_reason}")
        if stop_details:
            log_lines.append(f"Stop details: {json.dumps(stop_details, ensure_ascii=False)}")
    log_lines.append("")

    log_lines.append("## Resumo por turno")
    log_lines.append("")
    log_lines.append("| Turno | severity | findings | block | summary (curto) |")
    log_lines.append("|---|---|---|---|---|")
    for n in range(1, current_turn + 1):
        f = _read_findings(dialogue_dir, n) or {}
        sev = f.get("severity", "?")
        nf = len(f.get("findings") or [])
        block = "sim" if f.get("block_recommended") else "não"
        summary = (f.get("summary") or "").replace("|", "/").replace("\n", " ")
        if len(summary) > 80:
            summary = summary[:77] + "..."
        log_lines.append(f"| {n} | {sev} | {nf} | {block} | {summary} |")
    log_lines.append("")

    log_lines.append("## Findings pendentes")
    log_lines.append("")
    last_payload = _read_findings(dialogue_dir, current_turn) or {}
    pending = last_payload.get("findings") or []
    if pending:
        for i, f in enumerate(pending, 1):
            if not isinstance(f, dict):
                continue
            sev = f.get("severity", "?")
            title = f.get("title", "?")
            loc = f.get("location", "?")
            log_lines.append(f"{i}. **[{sev}] {title}** — `{loc}`")
    else:
        log_lines.append("_Nenhum finding pendente no último turno._")
    log_lines.append("")

    log_lines.append("## Plano final")
    log_lines.append("")
    log_lines.append(f"Caminho: `{final_plan_path}`")
    log_lines.append("")

    log_path = dialogue_dir / "dialogue_log.md"
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    meta["status"] = "finished"
    meta.setdefault("finished_at", _now_iso())
    _write_meta(dialogue_dir, meta)

    _emit({
        "status": "ok",
        "dialogue_id": args.dialogue_id,
        "final_plan_path": str(final_plan_path),
        "dialogue_log_path": str(log_path),
        "turns_completed": current_turn,
        "stop_reason": stop_reason,
    })
    return 0


# --------------------------------------------------------------- main

def _parse_cli(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Iterative multi-turn plan-review dialogue runner.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_start = sub.add_parser("start", help="initialise a dialogue")
    p_start.add_argument("--plan-file", required=True)
    p_start.add_argument("--cwd", default=None)
    p_start.add_argument("--max-turns", type=int, default=None,
                         help=f"override max turns (default {DEFAULT_MAX_TURNS}, "
                              "env CODEX_DIALOGUE_MAX_TURNS, range 1-20)")

    p_next = sub.add_parser("next-turn", help="submit a revised plan for the next turn")
    p_next.add_argument("--dialogue-id", required=True)
    p_next.add_argument("--plan-file", required=True)

    p_status = sub.add_parser("status", help="inspect dialogue state")
    p_status.add_argument("--dialogue-id", required=True)

    p_abort = sub.add_parser("abort", help="mark dialogue as aborted")
    p_abort.add_argument("--dialogue-id", required=True)

    p_finish = sub.add_parser("finish", help="consolidate dialogue artefacts")
    p_finish.add_argument("--dialogue-id", required=True)

    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_cli(argv)
    handler = {
        "start":     cmd_start,
        "next-turn": cmd_next_turn,
        "status":    cmd_status,
        "abort":     cmd_abort,
        "finish":    cmd_finish,
    }[args.subcommand]
    try:
        return handler(args)
    except Exception as exc:  # never raise out of CLI
        _emit({
            "status": "error",
            "reason": f"internal_error: {exc.__class__.__name__}",
            "subcommand": args.subcommand,
        })
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
