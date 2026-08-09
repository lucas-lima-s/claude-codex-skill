"""Normalize raw Codex output into the wrapper's stable JSON schema.

Input is the free-form text produced by ``codex exec`` (or its mock). The
normalizer is tolerant to:
  * strict JSON output (produced when we pass ``--output-schema``),
  * JSON embedded inside a fenced ``json`` block,
  * free-form text (delegate mode packages it as ``summary``; review modes
    report ``status=error``).

The output always conforms to::

    {
        "status":             "ok" | "error",
        "severity":           "low" | "medium" | "high",
        "confidence":         "low" | "medium" | "high",
        "summary":            str,
        "findings":           [ {severity, category, title, detail, location} ],
        "block_recommended":  bool,
        "fingerprint":        str,           # 16-hex; empty when findings=[]
        "raw_codex_output":   str,
        "mode":               "plan-review" | "verify" | "delegate",
        "coverage":           [ {category, findings_count} ],   # optional
    }
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_config import t  # noqa: E402

SEVERITY_VALUES = ("low", "medium", "high")
CONFIDENCE_VALUES = ("low", "medium", "high")
REVIEW_MODES = ("plan-review", "verify", "ask", "insight")
ALL_MODES = ("plan-review", "verify", "delegate", "ask", "insight")

FENCED_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
LOOSE_JSON_RE = re.compile(r"(\{.*\})", re.DOTALL)


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Try to parse a JSON object out of ``raw``. Returns None on failure."""
    candidates: list[str] = []
    stripped = raw.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    fenced = FENCED_JSON_RE.search(raw)
    if fenced:
        candidates.append(fenced.group(1))
    loose = LOOSE_JSON_RE.search(raw)
    if loose and loose.group(1) not in candidates:
        candidates.append(loose.group(1))
    for c in candidates:
        try:
            data = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _clamp_enum(value: Any, allowed: tuple, default: str) -> str:
    if isinstance(value, str) and value in allowed:
        return value
    return default


def _normalize_findings(raw_findings: Any) -> list[dict[str, str]]:
    if not isinstance(raw_findings, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        detail = item.get("detail")
        if not isinstance(title, str) or not isinstance(detail, str):
            continue
        if not title.strip() or not detail.strip():
            continue
        out.append(
            {
                "severity": _clamp_enum(item.get("severity"), SEVERITY_VALUES, "low"),
                "category": str(item.get("category") or ""),
                "title": title,
                "detail": detail,
                "location": str(item.get("location") or ""),
            }
        )
    return out


def _normalize_coverage(raw_coverage: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_coverage, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw_coverage:
        if not isinstance(item, dict):
            continue
        category = item.get("category")
        if not isinstance(category, str) or not category.strip():
            continue
        count = item.get("findings_count")
        if isinstance(count, bool) or not isinstance(count, int):
            count = 0
        out.append({"category": category, "findings_count": max(0, count)})
    return out


def _compute_fingerprint(findings: list[dict[str, str]], severity: str, block_recommended: bool) -> str:
    if not findings:
        return ""
    canonical = [
        {
            "severity": f["severity"],
            "category": f.get("category", ""),
            "title": f["title"],
            "location": f.get("location", ""),
        }
        for f in findings
    ]
    canonical.sort(key=lambda x: (x["severity"], x["category"], x["title"], x["location"]))
    payload = {
        "findings": canonical,
        "severity": severity,
        "block_recommended": block_recommended,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return digest[:16]


def _build_defaults(mode: str, raw: str) -> dict[str, Any]:
    return {
        "status": "ok",
        "severity": "low",
        "confidence": "low",
        "summary": "",
        "findings": [],
        "block_recommended": False,
        "fingerprint": "",
        "raw_codex_output": raw,
        "mode": mode,
    }


def normalize(raw: str, mode: str) -> dict[str, Any]:
    if mode not in ALL_MODES:
        raise ValueError(f"unknown mode: {mode!r}")

    result = _build_defaults(mode, raw)
    data = _extract_json_object(raw)

    if data is None:
        if mode == "delegate":
            result["summary"] = raw.strip()
            return result
        result["status"] = "error"
        result["error_class"] = "not_structured_json"
        result["summary"] = t("normalize.summary.not_structured")
        return result

    result["severity"] = _clamp_enum(data.get("severity"), SEVERITY_VALUES, "low")
    result["confidence"] = _clamp_enum(data.get("confidence"), CONFIDENCE_VALUES, "low")
    summary = data.get("summary")
    result["summary"] = summary if isinstance(summary, str) else ""
    result["findings"] = _normalize_findings(data.get("findings"))
    coverage = _normalize_coverage(data.get("coverage"))
    if coverage:
        result["coverage"] = coverage

    requested_block = bool(data.get("block_recommended"))
    if result["severity"] == "high" and result["confidence"] == "high":
        result["block_recommended"] = requested_block
    else:
        result["block_recommended"] = False

    result["fingerprint"] = _compute_fingerprint(result["findings"], result["severity"], result["block_recommended"])
    return result
