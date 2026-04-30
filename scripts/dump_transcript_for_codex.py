"""Dump the last N user/assistant turns of the current Claude Code session.

Locates the most recent `.jsonl` under `~/.claude/projects/<slug>/`, where `<slug>`
is derived from `--cwd` by replacing `\\`, `/` and `:` with `-`. Filters to
`type in ("user", "assistant")`, extracts plain text (drops thinking,
tool_use details, tool_result payloads to keep the dump compact), and formats
as a dialog block suitable for prefixing a Codex prompt.

Output goes to stdout (UTF-8) or `--output <file>`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional


def _slug_from_cwd(cwd: str) -> str:
    s = cwd.replace("\\", "-").replace("/", "-").replace(":", "-")
    while "--" in s:
        s_new = s.replace("---", "--")
        if s_new == s:
            break
        s = s_new
    return s


def _find_latest_transcript(cwd: str) -> Optional[Path]:
    projects_root = Path(os.path.expanduser("~/.claude/projects"))
    slug = _slug_from_cwd(cwd)
    candidate_dir = projects_root / slug
    if not candidate_dir.is_dir():
        return None
    jsonl_files = sorted(candidate_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonl_files[0] if jsonl_files else None


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "text":
            txt = item.get("text", "")
            if isinstance(txt, str) and txt.strip():
                parts.append(txt.strip())
        elif t == "tool_use":
            name = item.get("name", "?")
            tool_input = item.get("input", {})
            desc = tool_input.get("description") or tool_input.get("command") or ""
            if isinstance(desc, str):
                desc = desc.splitlines()[0][:200] if desc else ""
            parts.append(f"[tool: {name}] {desc}".strip())
    return "\n".join(parts).strip()


def _read_turns(path: Path, last_turns: int) -> List[str]:
    turns: List[str] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            if t not in ("user", "assistant"):
                continue
            msg = d.get("message", {}) or {}
            role = msg.get("role", t)
            text = _extract_text(msg.get("content"))
            if not text:
                continue
            turns.append(f"{role.upper()}:\n{text}")
    if last_turns > 0:
        turns = turns[-last_turns:]
    return turns


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--cwd", required=True, help="current working directory; used to find the session slug")
    parser.add_argument("--last-turns", type=int, default=10, help="keep only the last N turns (0 = all)")
    parser.add_argument("--output", default=None, help="output file (default: stdout)")
    parser.add_argument("--transcript-path", default=None, help="override transcript jsonl path (skip auto-discovery)")
    args = parser.parse_args(argv)

    if args.transcript_path:
        jsonl_path: Optional[Path] = Path(args.transcript_path)
        if not jsonl_path.is_file():
            jsonl_path = None
    else:
        jsonl_path = _find_latest_transcript(args.cwd)

    if jsonl_path is None:
        payload = ""
    else:
        turns = _read_turns(jsonl_path, args.last_turns)
        payload = "\n\n---\n\n".join(turns)

    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    else:
        try:
            sys.stdout.buffer.write(payload.encode("utf-8"))
        except AttributeError:
            sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
