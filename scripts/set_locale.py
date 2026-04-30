"""Non-interactive locale setter for the codex skill.

Usage:
    python scripts/set_locale.py --locale en-US
    python scripts/set_locale.py --locale pt-BR

Used by the SKILL.md "troca de locale" trigger phrase, where Claude can
invoke this via Bash without violating the global rule against using
``Write`` on existing files. ``setup.py`` is interactive (reads stdin)
and unsuitable for Claude-driven invocation; this helper accepts the
choice via a flag and writes ``settings.locale`` to ``config.local.json``
preserving any other keys.

All output goes to stderr (consistent with ``setup.py``); stdout stays
silent so the caller can chain commands without parsing pollution.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

VALID_LOCALES = ["en-US", "pt-BR"]
LOCAL_PATH = Path(__file__).resolve().parent.parent / "config.local.json"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set the codex skill interface locale (non-interactive).",
    )
    parser.add_argument(
        "--locale",
        required=True,
        choices=VALID_LOCALES,
        help="Interface locale code (en-US or pt-BR).",
    )
    args = parser.parse_args(argv)

    current: dict[str, Any] = {}
    if LOCAL_PATH.exists():
        try:
            loaded = json.loads(LOCAL_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except (OSError, json.JSONDecodeError):
            print(
                f"Warning: existing config.local.json is malformed; rewriting.",
                file=sys.stderr,
            )

    merged = _deep_merge(current, {"settings": {"locale": args.locale}})
    LOCAL_PATH.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Locale set to {args.locale} in {LOCAL_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
