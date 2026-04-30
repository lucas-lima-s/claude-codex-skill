"""Heuristic plan-complexity scorer.

Pure-function CLI: read a plan file, return a JSON object describing
whether the plan is "complex enough" to justify suggesting the iterative
multi-turn review (``plan-review-iter``) over the one-shot ``plan-review``.

Output JSON (stdout, single line, UTF-8)::

    {
        "score": 7,
        "suggest_iterative": true,
        "reasons": [
            "8 distinct files mentioned (>5)",
            "3 explicit phases (Phase 1/2/3)",
            "cross-module: auth/ + api/",
            "size 6.2 KB (>4 KB)"
        ]
    }

Signal table (each worth 1 point; suggest when ``score >= 3``):

    +------------------------+-----------------------------------------------+
    | Signal                 | Detection                                     |
    +------------------------+-----------------------------------------------+
    | Plan size > 4 KB       | os.path.getsize                               |
    | > 5 distinct files     | regex on common code/doc extensions           |
    | > 2 explicit phases    | regex on ``Phase N`` / ``Fase N`` headings    |
    | Sensitive keywords     | auth, permission, payment, fiscal, deploy,    |
    |                        | migration, schema, breaking, rollback         |
    | Cross-module           | mentions of >=2 of handler/service/repo/api/  |
    |                        | frontend/backend                              |
    +------------------------+-----------------------------------------------+

Constraints:
- Read-only. Never spawns Codex, never touches the network.
- Deterministic. Same input → same output.
- Never raises. On error, returns ``{score: 0, suggest_iterative: false,
  reasons: ["could not read plan: <ClassName>"]}`` so the caller can
  proceed with the regular ``plan-review`` flow.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_config import get as _config_get, t  # noqa: E402

THRESHOLD_SCORE = int(_config_get("complexity.threshold_score", 3))
SIZE_THRESHOLD_BYTES = int(_config_get("complexity.size_threshold_bytes", 4 * 1024))
DISTINCT_FILES_THRESHOLD = int(_config_get("complexity.distinct_files_threshold", 5))
PHASES_THRESHOLD = int(_config_get("complexity.phases_threshold", 2))

# A single regex for paths matching common code/doc extensions. Anchored to
# word boundaries to avoid false positives inside URLs or random tokens.
FILE_PATH_RE = re.compile(
    r"\b[\w./-]+\.(?:py|ts|tsx|js|jsx|md|sh|ps1|json|yml|yaml|toml|sql|"
    r"go|rs|java|kt|cpp|c|h|hpp|rb|php|cs|swift)\b"
)

PHASE_HEADING_RE = re.compile(
    r"^#{1,3}\s*(?:Phase|Fase)\s*\d+\b",
    flags=re.IGNORECASE | re.MULTILINE,
)

SENSITIVE_KEYWORDS = tuple(_config_get(
    "complexity.sensitive_keywords",
    ("auth", "permission", "payment", "fiscal", "deploy", "migration",
     "schema", "breaking", "rollback"),
))

CROSS_MODULE_DIRS = tuple(_config_get(
    "complexity.cross_module_dirs",
    ("handler", "service", "repo", "api", "frontend", "backend"),
))


def _emit(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    try:
        sys.stdout.buffer.write(text.encode("utf-8"))
    except AttributeError:
        sys.stdout.write(text)


def _safe_failure(reason: str) -> dict[str, Any]:
    return {"score": 0, "suggest_iterative": False, "reasons": [reason]}


def _count_distinct_files(text: str) -> int:
    found = set()
    for match in FILE_PATH_RE.findall(text):
        # Skip dotfile-only fragments (e.g., ".py" alone) that the boundary
        # regex sometimes captures.
        if "." not in match or match.startswith(".") or len(match) < 4:
            continue
        found.add(match)
    return len(found)


def _count_phases(text: str) -> int:
    return len(PHASE_HEADING_RE.findall(text))


def _sensitive_keywords_hit(text: str) -> list[str]:
    lowered = text.lower()
    hits: list[str] = []
    for kw in SENSITIVE_KEYWORDS:
        # Match as whole word to avoid false positives like "deployment"
        # being driven by "deploy" plus extra letters; \b handles Unicode
        # boundaries acceptably for ASCII keywords.
        if re.search(rf"\b{re.escape(kw)}\b", lowered):
            hits.append(kw)
    return hits


def _cross_module_hits(text: str) -> list[str]:
    lowered = text.lower()
    hits: list[str] = []
    for d in CROSS_MODULE_DIRS:
        if re.search(rf"\b{re.escape(d)}\b", lowered):
            hits.append(d)
    return hits


def _score_plan(plan_path: Path) -> tuple[int, list[str]]:
    try:
        text = plan_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError) as exc:
        # Surface as exception caller treats as "soft failure"
        raise RuntimeError(t("complexity.error.read_failure", exc=exc.__class__.__name__)) from exc

    try:
        size_bytes = plan_path.stat().st_size
    except OSError:
        size_bytes = len(text.encode("utf-8"))

    score = 0
    reasons: list[str] = []

    if size_bytes > SIZE_THRESHOLD_BYTES:
        score += 1
        reasons.append(t(
            "complexity.reasons.size",
            kb=f"{size_bytes / 1024:.1f}",
            threshold=SIZE_THRESHOLD_BYTES // 1024,
        ))

    distinct_files = _count_distinct_files(text)
    if distinct_files > DISTINCT_FILES_THRESHOLD:
        score += 1
        reasons.append(t(
            "complexity.reasons.distinct_files",
            n=distinct_files,
            threshold=DISTINCT_FILES_THRESHOLD,
        ))

    phases = _count_phases(text)
    if phases > PHASES_THRESHOLD:
        score += 1
        reasons.append(t(
            "complexity.reasons.phases",
            n=phases,
            threshold=PHASES_THRESHOLD,
        ))

    sensitive_hits = _sensitive_keywords_hit(text)
    if sensitive_hits:
        score += 1
        kws = ", ".join(sensitive_hits[:5])
        reasons.append(t("complexity.reasons.sensitive_keywords", kws=kws))

    cross_hits = _cross_module_hits(text)
    if len(cross_hits) >= 2:
        score += 1
        kws = " + ".join(cross_hits[:4])
        reasons.append(t("complexity.reasons.cross_module", kws=kws))

    return score, reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=t("complexity.cli.description"),
    )
    parser.add_argument("--plan-file", required=True,
                        help=t("complexity.cli.help.plan_file"))
    parser.add_argument("--cwd", default=None,
                        help=t("complexity.cli.help.cwd"))
    args = parser.parse_args(argv)

    plan_path = Path(args.plan_file)
    if not plan_path.is_file():
        _emit(_safe_failure(t("complexity.error.file_not_found", path=plan_path)))
        return 0

    try:
        score, reasons = _score_plan(plan_path)
    except RuntimeError as exc:
        _emit(_safe_failure(str(exc)))
        return 0
    except Exception as exc:  # never raise out of CLI
        _emit(_safe_failure(t("complexity.error.internal", exc=exc.__class__.__name__)))
        return 0

    _emit({
        "score": score,
        "suggest_iterative": score >= THRESHOLD_SCORE,
        "reasons": reasons,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
