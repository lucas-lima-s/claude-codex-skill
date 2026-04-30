"""Interactive setup wizard for the codex skill.

Starts in English (so the user can choose the interface locale before any
i18n is resolved), then validates environment and credentials in the
chosen locale via ``codex_config.t``.

Stdout discipline: the wizard NEVER writes to stdout. Banner, prompts,
validation, and final message all go through ``sys.stderr`` via the
``say`` helper. This keeps stdout reserved for canonical JSON when the
wizard is auto-triggered before a wrapper invocation.

Usage:
    python scripts/setup.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

BIN_DIR = Path(__file__).resolve().parent
SKILL_DIR = BIN_DIR.parent
LOCAL_PATH = SKILL_DIR / "config.local.json"

sys.path.insert(0, str(BIN_DIR))
import codex_config  # noqa: E402

VALID_LOCALES = ["en-US", "pt-BR"]
LOCALE_LABELS = {"en-US": "en-US (English)", "pt-BR": "pt-BR (Português)"}


def say(msg: str) -> None:
    """Write a line to stderr — never to stdout."""
    print(msg, file=sys.stderr, flush=True)


def _read_local_config() -> Dict[str, Any]:
    if not LOCAL_PATH.exists():
        return {}
    try:
        data = json.loads(LOCAL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _current_locale(local: Dict[str, Any]) -> str:
    settings = local.get("settings") if isinstance(local, dict) else None
    if isinstance(settings, dict):
        value = settings.get("locale")
        if isinstance(value, str) and value.strip() in VALID_LOCALES:
            return value.strip()
    return "pt-BR"


def _ask_locale(current: str) -> str:
    default_index = 1 if current == "en-US" else 2
    while True:
        say("")
        say("Choose interface language:")
        say(f"  [1] {LOCALE_LABELS['en-US']}")
        say(f"  [2] {LOCALE_LABELS['pt-BR']}")
        prompt = f"Selection [1/2] (default: {default_index}): "
        # Send prompt to stderr too — input() prints its arg to stdout, so
        # we print to stderr first and call input() with empty arg.
        sys.stderr.write(prompt)
        sys.stderr.flush()
        try:
            raw = input("").strip()
        except EOFError:
            raw = ""
        if not raw:
            return "en-US" if default_index == 1 else "pt-BR"
        if raw in ("1", "en-US"):
            return "en-US"
        if raw in ("2", "pt-BR"):
            return "pt-BR"
        say(f"  Invalid selection: {raw!r}. Enter 1 or 2.")


def _persist_locale(chosen: str) -> None:
    current = _read_local_config()
    merged = _deep_merge(current, {"settings": {"locale": chosen}})
    LOCAL_PATH.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    codex_config.clear_cache()
    # Force the active locale immediately so subsequent t() calls use it.
    codex_config._cached_locale = chosen


def _validate_codex_cli() -> None:
    codex_path = shutil.which("codex")
    if not codex_path:
        say(codex_config.t("setup.validation.codex_missing"))
        return
    try:
        proc = subprocess.run(
            [codex_path, "--version"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        say(codex_config.t("setup.validation.codex_missing"))
        return
    version_lines = (proc.stdout or proc.stderr or "").strip().splitlines()
    version_str = version_lines[0] if version_lines else "unknown"
    say(codex_config.t("setup.validation.codex_ok", version=version_str))


def _validate_python() -> None:
    skills_python = os.environ.get("SKILLS_PYTHON")
    if skills_python and Path(skills_python).exists():
        executable = skills_python
    else:
        executable = sys.executable
        say(codex_config.t(
            "setup.validation.python_warn_skills_python",
            executable=executable,
        ))
        return
    version = "{}.{}.{}".format(
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    )
    say(codex_config.t(
        "setup.validation.python_ok",
        executable=executable,
        version=version,
    ))


def _credentials_sources() -> List[Path]:
    raw = codex_config.get("credentials.source", "~/.claude/credentials.env")
    if isinstance(raw, str):
        raw = [raw]
    elif not isinstance(raw, list):
        return []
    paths: List[Path] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        expanded = os.path.expanduser(entry)
        path = Path(expanded)
        if not path.is_absolute():
            path = SKILL_DIR / expanded
        paths.append(path)
    return paths


def _keys_present(sources: List[Path], wanted: List[str]) -> Dict[str, bool]:
    found = {key: False for key in wanted}
    for path in sources:
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, _, _ = stripped.partition("=")
                key = key.strip()
                if key in found:
                    found[key] = True
        except OSError:
            continue
    return found


def _validate_credentials() -> None:
    propagate = codex_config.get("credentials.propagate", []) or []
    if not isinstance(propagate, list):
        propagate = []
    keys: List[str] = [k for k in propagate if isinstance(k, str) and k.strip()]
    if not keys:
        say(codex_config.t("setup.validation.credentials_none"))
    else:
        say(codex_config.t(
            "setup.validation.credentials_header",
            list=", ".join(keys),
        ))
        sources = _credentials_sources()
        present = _keys_present(sources, keys)
        for key in keys:
            if not present.get(key):
                say(codex_config.t(
                    "setup.validation.credentials_missing_warn",
                    key=key,
                ))
    say(codex_config.t("setup.validation.credentials_hint"))


def main() -> int:
    say("Codex skill setup")
    say("=================")
    local = _read_local_config()
    current = _current_locale(local)
    chosen = _ask_locale(current)
    _persist_locale(chosen)
    say("")
    say(codex_config.t("setup.locale_set", locale=chosen))
    say("")
    say(codex_config.t("setup.validation.header"))
    _validate_codex_cli()
    _validate_python()
    _validate_credentials()
    say("")
    say(codex_config.t("setup.complete"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
