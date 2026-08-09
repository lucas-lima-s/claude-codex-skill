"""Split a phased plan into per-phase review slices plus a coherence slice.

A single ``plan-review`` over a very large plan is the shape that fails: one
run, one wall-clock ceiling, one context. Splitting it into N slices lets the
batcher review them in parallel, and each slice cites fewer files so its
review packet stays well under the truncation limit.

What splitting costs is the analysis *between* phases — ordering, cross-phase
dependencies, contradictions at the boundaries. The extra ``coherence`` slice
exists to buy that back: it carries the whole plan with an instruction to look
only at structure, never at implementation detail.

Every slice repeats two things so a reviewer is never reading blind:
  * the plan head (everything before the first phase heading, usually the
    context and the goals), truncated at ``split.max_head_bytes``;
  * the outline of every phase heading, with the slice's own phase marked.

CLI::

    split_plan_by_phase.py --plan-file <path> [--output-dir <dir>]

Output JSON (stdout, single line, UTF-8)::

    {
        "status": "ok",
        "output_dir": "...",
        "phases": 6,
        "slices": [
            {"id": "phase-1", "kind": "phase", "heading": "## Phase 1 — ...",
             "path": ".../phase_1.md", "bytes": 4211},
            ...
            {"id": "coherence", "kind": "coherence", "heading": "",
             "path": ".../coherence.md", "bytes": 39467}
        ]
    }

A plan with no ``Phase N`` / ``Fase N`` headings is not an error and not a
degraded split: it returns ``{"status": "not_splittable", "reason":
"no_phase_headings"}`` so the caller falls back to the monolithic review
instead of silently reviewing one slice and believing it covered everything.

Read-only with respect to the plan, and never raises: every error path emits
``status=error`` JSON.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_plan_complexity import PHASE_HEADING_RE  # noqa: E402
from codex_config import get as _config_get  # noqa: E402
from codex_config import t  # noqa: E402

MIN_PHASES_TO_SPLIT = int(_config_get("split.min_phases", 2))
MAX_HEAD_BYTES = int(_config_get("split.max_head_bytes", 6144))
OUTPUT_PREFIX = "codex_split_"


def _emit(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    try:
        sys.stdout.buffer.write(text.encode("utf-8"))
    except AttributeError:
        sys.stdout.write(text)


def _truncate_bytes(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore").rstrip() + "\n\n[...]\n"


def _heading_line(text: str, start: int) -> str:
    end = text.find("\n", start)
    return text[start : end if end != -1 else len(text)].strip()


def split_plan(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Return ``(head, [(heading, body), ...])``.

    ``head`` is everything before the first phase heading. Each body starts at
    its own heading and runs to the next one.
    """
    matches = list(PHASE_HEADING_RE.finditer(text))
    if len(matches) < MIN_PHASES_TO_SPLIT:
        return text, []

    head = text[: matches[0].start()].strip()
    phases: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        phases.append((_heading_line(text, start), text[start:end].strip()))
    return head, phases


def _outline(headings: list[str], current: int | None) -> str:
    lines = []
    for index, heading in enumerate(headings, 1):
        marker = t("split.outline.current") if current == index else ""
        lines.append(f"{index}. {heading}{marker}")
    return "\n".join(lines)


def _compose_phase_slice(head: str, headings: list[str], number: int, body: str) -> str:
    parts = [t("split.preface.phase", n=number, total=len(headings))]
    if head:
        parts.append(t("split.section.head") + "\n\n" + _truncate_bytes(head, MAX_HEAD_BYTES))
    parts.append(t("split.section.outline") + "\n\n" + _outline(headings, number))
    parts.append(t("split.section.slice") + "\n\n" + body)
    return "\n\n---\n\n".join(parts) + "\n"


def _compose_coherence_slice(text: str, headings: list[str]) -> str:
    parts = [
        t("split.preface.coherence", total=len(headings)),
        t("split.section.outline") + "\n\n" + _outline(headings, None),
        t("split.section.full_plan") + "\n\n" + text.strip(),
    ]
    return "\n\n---\n\n".join(parts) + "\n"


def _output_dir(plan_path: Path, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    digest = hashlib.sha256(str(plan_path.resolve()).encode("utf-8")).hexdigest()[:10]
    return Path(tempfile.gettempdir()) / f"{OUTPUT_PREFIX}{digest}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=t("split.cli.description"))
    parser.add_argument("--plan-file", required=True, help=t("split.cli.help.plan_file"))
    parser.add_argument("--output-dir", default=None, help=t("split.cli.help.output_dir"))
    args = parser.parse_args(argv)

    plan_path = Path(args.plan_file)
    if not plan_path.is_file():
        _emit({"status": "error", "reason": "plan_file_not_found", "path": str(plan_path)})
        return 0

    try:
        text = plan_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _emit({"status": "error", "reason": f"unreadable_plan: {exc.__class__.__name__}"})
        return 0

    head, phases = split_plan(text)
    if not phases:
        _emit(
            {
                "status": "not_splittable",
                "reason": "no_phase_headings",
                "hint": t("split.hint.not_splittable"),
            }
        )
        return 0

    out_dir = _output_dir(plan_path, args.output_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _emit({"status": "error", "reason": f"cannot_create_output_dir: {exc.__class__.__name__}"})
        return 0

    headings = [heading for heading, _body in phases]
    slices: list[dict[str, Any]] = []
    try:
        for index, (heading, body) in enumerate(phases, 1):
            path = out_dir / f"phase_{index}.md"
            content = _compose_phase_slice(head, headings, index, body)
            path.write_text(content, encoding="utf-8")
            slices.append(
                {
                    "id": f"phase-{index}",
                    "kind": "phase",
                    "heading": heading,
                    "path": str(path),
                    "bytes": len(content.encode("utf-8")),
                }
            )

        coherence_path = out_dir / "coherence.md"
        coherence = _compose_coherence_slice(text, headings)
        coherence_path.write_text(coherence, encoding="utf-8")
        slices.append(
            {
                "id": "coherence",
                "kind": "coherence",
                "heading": "",
                "path": str(coherence_path),
                "bytes": len(coherence.encode("utf-8")),
            }
        )
    except OSError as exc:
        _emit({"status": "error", "reason": f"cannot_write_slice: {exc.__class__.__name__}"})
        return 0

    _emit(
        {
            "status": "ok",
            "output_dir": str(out_dir),
            "phases": len(phases),
            "slices": slices,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
