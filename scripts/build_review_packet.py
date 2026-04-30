"""Build a self-contained review packet for ``plan-review``.

The packet bundles:
  * the plan text (verbatim);
  * cwd, current branch and ``git status --short``;
  * the collected CLAUDE.md context (global + repo + target);
  * the files that the plan cites, numbered and windowed when long;
  * the optional transcript dump produced by ``dump_transcript_for_codex.py``;
  * a manifest describing what was included, truncated, or skipped.

Limits (defaults, all overridable via flags):

  * up to 12 cited files;
  * up to 300 lines per file (±50 around an explicit ``path:line`` citation);
  * 120 KB total packet size.

Selection priority when more than 12 files are cited:

  1. files mentioned with explicit ``path:line``;
  2. files mentioned more than once;
  3. the original mention order.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

DEFAULT_MAX_FILES = 12
DEFAULT_MAX_LINES = 300
DEFAULT_MAX_BYTES = 120 * 1024
WINDOW_LINES = 50

PATH_TOKEN_RE = re.compile(
    r"""
    (?<![A-Za-z0-9_\-/])         # not in the middle of an identifier
    (
        [A-Za-z0-9_\-./\\]+      # path-ish characters
        \.
        [A-Za-z0-9]{1,8}         # extension up to 8 chars
        (?::(\d{1,6}))?          # optional :line
    )
    """,
    re.VERBOSE,
)


def _read_text_safely(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def _resolve_cited(token: str, cwd: Path, target_path: Path | None) -> Path | None:
    candidate = token.replace("\\", "/")
    if candidate.startswith(("./", "../")):
        candidate = candidate[2:] if candidate.startswith("./") else candidate
    bases: list[Path] = []
    if target_path is not None:
        bases.append(target_path)
    bases.append(cwd)
    for base in bases:
        guess = (base / candidate).resolve()
        try:
            if guess.is_file():
                return guess
        except OSError:
            continue
    abs_guess = Path(candidate)
    try:
        if abs_guess.is_absolute() and abs_guess.is_file():
            return abs_guess.resolve()
    except OSError:
        pass
    return None


class _CitedEntry:
    __slots__ = ("lines", "count", "first_index")

    def __init__(self, first_index: int) -> None:
        self.lines: set[int] = set()
        self.count: int = 0
        self.first_index: int = first_index


def _detect_cited_files(plan_text: str, cwd: Path, target_path: Path | None) -> OrderedDict[Path, _CitedEntry]:
    """Resolve every plausible file reference in ``plan_text``.

    Iteration order matches first mention so callers can apply the priority rules.
    """
    found: OrderedDict[Path, _CitedEntry] = OrderedDict()
    index = 0
    for match in PATH_TOKEN_RE.finditer(plan_text):
        token = match.group(1)
        line_str = match.group(2)
        resolved = _resolve_cited(token.split(":", 1)[0], cwd, target_path)
        if resolved is None:
            continue
        entry = found.get(resolved)
        if entry is None:
            entry = _CitedEntry(first_index=index)
            found[resolved] = entry
        entry.count += 1
        if line_str:
            try:
                entry.lines.add(int(line_str))
            except ValueError:
                pass
        index += 1
    return found


def _select_files(cited: OrderedDict[Path, _CitedEntry], limit: int) -> tuple[list[Path], list[Path]]:
    """Return (selected, skipped) honouring the documented priority rules."""
    if not cited:
        return [], []
    items = list(cited.items())
    items.sort(
        key=lambda item: (
            0 if item[1].lines else 1,  # lines-cited first
            -item[1].count,  # frequency desc
            item[1].first_index,  # original order
        )
    )
    selected = [path for path, _ in items[:limit]]
    skipped = [path for path, _ in items[limit:]]
    return selected, skipped


def _file_window(text: str, cited_lines: list[int], max_lines: int) -> tuple[list[tuple[int, str]], int, str]:
    """Return ([(line_no, line_content), ...], total_lines, selection_reason)."""
    raw_lines = text.splitlines()
    total = len(raw_lines)
    if total <= max_lines:
        numbered = [(i + 1, raw_lines[i]) for i in range(total)]
        return numbered, total, "full file"
    if cited_lines:
        anchor = sorted(cited_lines)[0]
        start = max(1, anchor - WINDOW_LINES)
        end = min(total, anchor + WINDOW_LINES)
        if end - start + 1 > max_lines:
            end = start + max_lines - 1
        numbered = [(i, raw_lines[i - 1]) for i in range(start, end + 1)]
        return numbered, total, f"±{WINDOW_LINES} window around line {anchor}"
    numbered = [(i + 1, raw_lines[i]) for i in range(max_lines)]
    return numbered, total, f"first {max_lines} lines (no explicit line cited)"


def _git_branch(cwd: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _git_status(cwd: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "status", "--short"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").rstrip()


def _collect_context(cwd: Path, target_path: Path | None) -> str:
    helper = Path(__file__).resolve().parent / "collect_claude_context.py"
    python = os.environ.get("SKILLS_PYTHON") or os.environ.get("CLAUDE_AUTOMATION_PYTHON") or sys.executable
    cmd = [python, str(helper), "--cwd", str(cwd), "--format", "text"]
    if target_path is not None:
        cmd += ["--target-path", str(target_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


def _format_section_header(title: str) -> str:
    return f"\n## {title}\n"


def _build_packet(
    plan_text: str,
    cwd: Path,
    target_path: Path | None,
    transcript_text: str,
    max_files: int,
    max_lines: int,
    max_bytes: int,
) -> tuple[str, list[str]]:
    cited = _detect_cited_files(plan_text, cwd, target_path)
    selected, skipped = _select_files(cited, max_files)

    branch = _git_branch(cwd)
    status_short = _git_status(cwd)
    context_text = _collect_context(cwd, target_path)

    parts: list[str] = []
    manifest: list[str] = []

    parts.append("# Plan-review packet")
    parts.append(_format_section_header("Plan"))
    parts.append(plan_text.rstrip())

    parts.append(_format_section_header("Environment"))
    parts.append(f"- cwd: {cwd}")
    if branch:
        parts.append(f"- branch: {branch}")
    if target_path is not None:
        parts.append(f"- target_path: {target_path}")

    if status_short:
        parts.append(_format_section_header("git status --short"))
        parts.append("```\n" + status_short + "\n```")

    if context_text.strip():
        parts.append(_format_section_header("CLAUDE.md context"))
        parts.append(context_text.rstrip())

    if selected:
        parts.append(_format_section_header("Cited files"))
        for path in selected:
            entry = cited[path]
            cited_lines = sorted(entry.lines) if entry.lines else []
            text = _read_text_safely(path)
            if not text:
                manifest.append(f"{path}: skipped (could not read)")
                continue
            window, total, reason = _file_window(text, cited_lines, max_lines)
            if not window:
                manifest.append(f"{path}: skipped (empty)")
                continue
            start_line = window[0][0]
            end_line = window[-1][0]
            parts.append(f"\n### {path}\n")
            parts.append(f"_lines {start_line}-{end_line} of {total}; selection: {reason}_\n")
            parts.append("```")
            for line_no, content in window:
                parts.append(f"{line_no:>5}: {content}")
            parts.append("```")
            manifest.append(f"{path}: lines {start_line}-{end_line}/{total} ({reason})")

    for path in skipped:
        manifest.append(f"{path}: skipped (max_files={max_files} exceeded)")

    if transcript_text.strip():
        parts.append(_format_section_header("Recent transcript (filtered)"))
        parts.append(transcript_text.rstrip())
        manifest.append("transcript: included")

    parts.append(_format_section_header("Manifest"))
    if manifest:
        for entry in manifest:
            parts.append(f"- {entry}")
    else:
        parts.append("- no cited files resolved")

    body = "\n".join(parts)
    encoded = body.encode("utf-8")
    truncated = False
    if len(encoded) > max_bytes:
        truncated = True
        cut = encoded[:max_bytes]
        try:
            body = cut.decode("utf-8")
        except UnicodeDecodeError:
            body = cut.decode("utf-8", errors="ignore")
        body += "\n\n[packet truncated: exceeded max_bytes=" f"{max_bytes}; original_size={len(encoded)} bytes]\n"

    final_size = len(body.encode("utf-8"))
    body += f"\n\n_packet bytes: {final_size}_\n"
    if truncated:
        manifest.append(f"packet truncated at {max_bytes} bytes")

    return body, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a plan-review packet.")
    parser.add_argument("--plan-file", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--target-path", default=None)
    parser.add_argument("--transcript-file", default=None)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args(argv)

    plan_text = _read_text_safely(Path(args.plan_file))
    if not plan_text.strip():
        print(f"[build_review_packet] empty plan: {args.plan_file}", file=sys.stderr)
        return 2

    cwd = Path(args.cwd).resolve()
    target_path = Path(args.target_path).resolve() if args.target_path else None
    transcript_text = ""
    if args.transcript_file:
        transcript_text = _read_text_safely(Path(args.transcript_file))

    body, _manifest = _build_packet(
        plan_text=plan_text,
        cwd=cwd,
        target_path=target_path,
        transcript_text=transcript_text,
        max_files=args.max_files,
        max_lines=args.max_lines,
        max_bytes=args.max_bytes,
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
