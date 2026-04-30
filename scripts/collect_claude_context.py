"""Collect CLAUDE.md files along the precedence chain for a target path.

Reads, in order:
    1. Global `~/.claude/CLAUDE.md` (overrideable via --global-claude-md).
    2. `<repo-root>/CLAUDE.md`.
    3. `CLAUDE.md` in each ancestor directory strictly between repo-root and
       the target directory.
    4. `CLAUDE.md` in the target directory (if distinct from the repo root).

Deduplicates entries by SHA-256 of their content, preserving first-seen order.
Never reads AGENTS.md. Never reads SKILL.md (only CLAUDE.md is ever a
candidate). Emits JSON by default, or a formatted text block suitable for
prefixing a Codex prompt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple


CLAUDE_MD = "CLAUDE.md"


def _resolve(p: Path) -> Path:
    try:
        return Path(os.path.realpath(str(p)))
    except OSError:
        return p.absolute()


def _target_directory(target: Path) -> Path:
    if target.is_file():
        return target.parent
    return target


def _find_repo_root(start: Path) -> Optional[Path]:
    """Walk from `start` up to the filesystem root and return the outermost
    directory that contains a `.git/` **directory**. This yields the top-level
    superproject when the caller sits inside a git submodule (submodules mark
    their root with a `.git` file, not a dir).
    """
    current = start if start.is_dir() else start.parent
    current = _resolve(current)
    outermost: Optional[Path] = None
    while True:
        git_entry = current / ".git"
        if git_entry.is_dir():
            outermost = current
        if current.parent == current:
            break
        current = current.parent
    return outermost


def _is_ancestor_or_equal(ancestor: Path, descendant: Path) -> bool:
    try:
        descendant.relative_to(ancestor)
        return True
    except ValueError:
        return False


def _ancestor_dirs_between(repo_root: Path, target_dir: Path) -> List[Path]:
    """Return directories strictly between repo_root and target_dir, in order
    from shallowest (closest to repo_root) to deepest (closest to target_dir).
    """
    if repo_root == target_dir:
        return []
    if not _is_ancestor_or_equal(repo_root, target_dir):
        return []
    rel = target_dir.relative_to(repo_root)
    parts = list(rel.parts)
    if len(parts) <= 1:
        return []
    ancestors: List[Path] = []
    current = repo_root
    for part in parts[:-1]:
        current = current / part
        ancestors.append(current)
    return ancestors


def _build_candidates(
    repo_root: Optional[Path],
    target_dir: Path,
    global_path: Path,
) -> List[Tuple[Path, str]]:
    candidates: List[Tuple[Path, str]] = [(global_path, "global")]
    if repo_root is not None and _is_ancestor_or_equal(repo_root, target_dir):
        candidates.append((repo_root / CLAUDE_MD, "repo"))
        for anc in _ancestor_dirs_between(repo_root, target_dir):
            candidates.append((anc / CLAUDE_MD, "ancestor"))
        if target_dir != repo_root:
            candidates.append((target_dir / CLAUDE_MD, "target"))
    else:
        candidates.append((target_dir / CLAUDE_MD, "target"))
    return candidates


def _read_safely(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="latin-1")
        except Exception:
            return None
    except OSError:
        return None


def collect(
    cwd: Path,
    target_path: Optional[Path],
    repo_root_arg: Optional[Path],
    global_claude_md: Optional[Path],
) -> dict:
    cwd_abs = _resolve(cwd)
    target_abs = _resolve(target_path) if target_path is not None else cwd_abs
    target_dir = _target_directory(target_abs)
    target_dir = _resolve(target_dir)

    if repo_root_arg is not None:
        repo_root: Optional[Path] = _resolve(repo_root_arg)
    else:
        detected = _find_repo_root(target_dir) if target_dir.exists() else None
        if detected is None:
            detected = _find_repo_root(cwd_abs)
        repo_root = detected if detected is not None else cwd_abs

    if global_claude_md is None:
        global_claude_md = Path(os.path.expanduser("~/.claude")) / CLAUDE_MD
    global_path = _resolve(global_claude_md)

    candidates = _build_candidates(repo_root, target_dir, global_path)

    entries: List[dict] = []
    seen: set = set()
    total = 0
    for path, scope in candidates:
        if path.name != CLAUDE_MD:
            continue
        if not path.is_file():
            continue
        content = _read_safely(path)
        if content is None:
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        bytes_len = len(content.encode("utf-8"))
        total += bytes_len
        entries.append(
            {
                "path": str(_resolve(path)),
                "content": content,
                "bytes": bytes_len,
                "sha256": digest,
                "scope": scope,
            }
        )

    return {
        "entries": entries,
        "total_bytes": total,
        "cwd": str(cwd_abs),
        "target_path": str(target_abs),
        "repo_root": str(repo_root) if repo_root is not None else None,
        "global_claude_md": str(global_path),
    }


def format_text(result: dict) -> str:
    n = len(result["entries"])
    total = result["total_bytes"]
    lines: List[str] = [f"=== CLAUDE.md contexto ({n} arquivos, {total} bytes) ==="]
    for entry in result["entries"]:
        lines.append("")
        lines.append(f"--- {entry['path']} (scope={entry['scope']}) ---")
        lines.append(entry["content"].rstrip("\n"))
    lines.append("")
    lines.append("=== fim do contexto ===")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--target-path", default=None)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--global-claude-md", default=None)
    parser.add_argument("--format", choices=["json", "text"], default="json")
    args = parser.parse_args(argv)

    result = collect(
        cwd=Path(args.cwd),
        target_path=Path(args.target_path) if args.target_path else None,
        repo_root_arg=Path(args.repo_root) if args.repo_root else None,
        global_claude_md=Path(args.global_claude_md) if args.global_claude_md else None,
    )

    payload = json.dumps(result, ensure_ascii=False) if args.format == "json" else format_text(result)
    # Force UTF-8 on stdout regardless of the console's code page (Windows
    # defaults to cp1252 in Python 3.8, which crashes on non-ASCII content).
    try:
        sys.stdout.buffer.write(payload.encode("utf-8"))
    except AttributeError:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
