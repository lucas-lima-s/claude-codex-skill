#!/usr/bin/env python3
"""Fake Codex CLI used to test the wrapper deterministically.

The real ``codex exec`` is replaced by this script when the wrapper is launched
with ``CODEX_WRAPPER_CODEX_OVERRIDE`` pointing here. Behavior is selected via
``FAKE_CODEX_BEHAVIOR`` (default ``success``):

    success        Valid JSON review packet written to -o; exit 0.
    delegate_ok    Valid JSON delegate packet (created/edited/deleted/...).
    nonzero        Exit 2 with no output.
    invalid_json   Plain prose written to -o; exit 0.
    needs_input    JSON with status=needs_input and questions[].
    partial        Truncated JSON object (half-written).
    noisy_stderr   Valid JSON to -o + lots of stderr noise.
    timeout        Sleep ``FAKE_CODEX_TIMEOUT_SECONDS`` (default 9999).
    delay_short    Sleep 5s then succeed (under heartbeat threshold).
    delay_long     Sleep ``FAKE_CODEX_DELAY_SECONDS`` (default 35) then succeed.

We intentionally accept the same flag surface as the real Codex CLI so the
wrapper can call us unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _parse_codex_args(argv):
    """Mirror enough of ``codex exec`` to satisfy the wrapper.

    We only need ``-o`` (output file) and the flag that consumes stdin (the
    final ``-``). Everything else can be ignored for testing.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("subcommand", nargs="?", default="exec")
    parser.add_argument("-o", "--output", dest="output", default=None)
    parser.add_argument("--sandbox", default=None)
    parser.add_argument("--skip-git-repo-check", action="store_true")
    parser.add_argument("--ephemeral", action="store_true")
    parser.add_argument("--color", default=None)
    parser.add_argument("-C", dest="cwd", default=None)
    parser.add_argument("-c", dest="config", action="append", default=[])
    parser.add_argument("--output-schema", default=None)
    parser.add_argument("--json", dest="json_stream", action="store_true")
    parser.add_argument("rest", nargs="*")
    return parser.parse_known_args(argv)


def _success_payload(mode_hint: str) -> dict:
    if mode_hint == "delegate":
        return {
            "status": "ok",
            "severity": "low",
            "confidence": "high",
            "summary": "Fake delegate succeeded.",
            "findings": [],
            "block_recommended": False,
            "files_created": ["fake/new.txt"],
            "files_edited": ["fake/touched.py"],
            "files_deleted": [],
            "commands_run": ["echo fake"],
            "tests_run": [],
        }
    return {
        "status": "ok",
        "severity": "medium",
        "confidence": "high",
        "summary": "Fake Codex review succeeded.",
        "findings": [
            {
                "severity": "medium",
                "category": "design",
                "title": "Sample finding",
                "detail": "This is a synthetic finding produced by fake_codex.py.",
                "location": "fake.py:42",
            }
        ],
        "block_recommended": False,
    }


def _needs_input_payload() -> dict:
    return {
        "status": "needs_input",
        "severity": "low",
        "confidence": "high",
        "summary": "Fake Codex needs more information to proceed.",
        "findings": [],
        "block_recommended": False,
        "questions": [
            {
                "id": "q1",
                "question": "Which target environment should the migration run against?",
                "context": "The plan mentions both staging and production.",
            }
        ],
    }


def _write(path: str | None, text: str) -> None:
    if path:
        Path(path).write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    sys.stdout.flush()


def _detect_mode_hint(stdin_text: str) -> str:
    lowered = stdin_text.lower()
    if "perform the following work" in lowered:
        return "delegate"
    return "review"


def main() -> int:
    args, _ = _parse_codex_args(sys.argv[1:])
    behavior = os.environ.get("FAKE_CODEX_BEHAVIOR", "success").strip().lower()

    stdin_text = ""
    try:
        stdin_text = sys.stdin.read()
    except Exception:
        stdin_text = ""

    mode_hint = _detect_mode_hint(stdin_text)

    if behavior == "timeout":
        seconds = float(os.environ.get("FAKE_CODEX_TIMEOUT_SECONDS", "9999"))
        time.sleep(seconds)
        return 0

    if behavior == "delay_short":
        time.sleep(5)

    if behavior == "delay_long":
        seconds = float(os.environ.get("FAKE_CODEX_DELAY_SECONDS", "35"))
        time.sleep(seconds)

    if behavior == "nonzero":
        sys.stderr.write("fake codex: simulated failure\n")
        return 2

    if behavior == "invalid_json":
        _write(args.output, "I am not JSON. Just some prose Codex sometimes leaks.\n")
        return 0

    if behavior == "partial":
        _write(args.output, '{"status": "ok", "summary": "Truncated mid-sentenc')
        return 0

    if behavior == "needs_input":
        _write(args.output, json.dumps(_needs_input_payload(), ensure_ascii=False))
        return 0

    if behavior == "noisy_stderr":
        for i in range(50):
            sys.stderr.write(f"fake codex noise line {i}\n")
        sys.stderr.flush()
        _write(args.output, json.dumps(_success_payload(mode_hint), ensure_ascii=False))
        return 0

    if behavior == "delegate_ok":
        _write(args.output, json.dumps(_success_payload("delegate"), ensure_ascii=False))
        return 0

    # default: success / delay_short / delay_long
    _write(args.output, json.dumps(_success_payload(mode_hint), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
