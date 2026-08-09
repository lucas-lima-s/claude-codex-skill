"""Centralised configuration + i18n helper for the codex skill.

Loads ``config.default.json`` (versioned) and merges any ``config.local.json``
override on top. Exposes:

  * ``get(path, default=None)`` — dotted-path access into ``settings``.
  * ``t(key, **kwargs)`` — translation helper. Falls back to pt-BR, then to
    the literal key. Supports ``str.format`` keyword arguments.
  * ``detect_locale()`` — resolves the active locale (CODEX_LOCALE env →
    system locale → pt-BR).
  * ``clear_cache()`` — drops the in-memory cache (used by tests).

The module never raises out of ``get`` / ``t``: a missing config file or a
malformed override falls back to an empty/default state and the literal
key. Callers can always rely on a string return.
"""

from __future__ import annotations

import json
import locale as _stdlib_locale
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULTS_PATH = SKILL_DIR / "config.default.json"
LOCAL_PATH = SKILL_DIR / "config.local.json"

DEFAULT_LOCALE = "pt-BR"

_cached_config: dict[str, Any] | None = None
_cached_locale: str | None = None


def _safe_load_json(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base`` (in-place on a copy)."""
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_config() -> dict[str, Any]:
    global _cached_config
    if _cached_config is not None:
        return _cached_config
    base = _safe_load_json(DEFAULTS_PATH)
    if LOCAL_PATH.exists():
        local = _safe_load_json(LOCAL_PATH)
        if local:
            base = _deep_merge(base, local)
    _cached_config = base
    return base


def clear_cache() -> None:
    """Drop the cached config and locale. Used by tests that mutate the
    config files between runs."""
    global _cached_config, _cached_locale
    _cached_config = None
    _cached_locale = None


def get(path: str, default: Any = None) -> Any:
    """Dotted-path lookup into the ``settings`` tree.

    Example: ``get("dialogue.default_max_turns", 3)``.
    """
    cfg = _load_config()
    node: Any = cfg.get("settings") or {}
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def detect_locale() -> str:
    """Resolve the active locale.

    Priority:
      1. ``CODEX_LOCALE`` env var (raw, e.g. ``en-US`` or ``pt_BR``).
      2. ``settings.locale`` in ``config.local.json``.
      3. System locale via ``locale.getdefaultlocale()``.
      4. ``DEFAULT_LOCALE`` (pt-BR).

    Underscores are normalised to hyphens (``pt_BR`` → ``pt-BR``) so the
    value matches the JSON keys.
    """
    global _cached_locale
    if _cached_locale is not None:
        return _cached_locale

    env_value = os.environ.get("CODEX_LOCALE")
    if env_value and env_value.strip():
        _cached_locale = env_value.replace("_", "-").strip()
        return _cached_locale

    cfg = _load_config()
    settings = cfg.get("settings") if isinstance(cfg, dict) else None
    if isinstance(settings, dict):
        config_locale = settings.get("locale")
        if isinstance(config_locale, str) and config_locale.strip():
            _cached_locale = config_locale.replace("_", "-").strip()
            return _cached_locale

    try:
        sys_locale, _enc = _stdlib_locale.getdefaultlocale()
    except Exception:
        sys_locale = None

    if sys_locale:
        _cached_locale = sys_locale.replace("_", "-")
        return _cached_locale

    _cached_locale = DEFAULT_LOCALE
    return _cached_locale


def _candidate_locales(target: str) -> list:
    """Build a fallback chain for translation lookups.

    Example: ``en-US`` → ``["en-US", "en", "pt-BR"]``.
    """
    candidates = [target]
    if "-" in target:
        candidates.append(target.split("-", 1)[0])
    if DEFAULT_LOCALE not in candidates:
        candidates.append(DEFAULT_LOCALE)
    return candidates


def t(key: str, **kwargs: Any) -> str:
    """Translate a key into the active locale.

    Lookup order: active locale → language prefix → DEFAULT_LOCALE → key.
    ``kwargs`` are passed to ``str.format``. Format errors fall back to the
    raw template (so a missing placeholder never blows up the caller).
    """
    cfg = _load_config()
    locales = cfg.get("locales") or {}
    if not isinstance(locales, dict):
        locales = {}

    target = detect_locale()
    template: str | None = None
    for candidate in _candidate_locales(target):
        bucket = locales.get(candidate)
        if isinstance(bucket, dict) and isinstance(bucket.get(key), str):
            template = bucket[key]
            break

    if template is None:
        return key

    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


def resolve_python() -> str:
    """Centralised Python resolver shared by every entry-point and by
    ``ensure_setup_complete``.

    Priority: ``SKILLS_PYTHON`` env -> ``CLAUDE_AUTOMATION_PYTHON`` env ->
    ``sys.executable``. The first env var that points to an existing file
    wins.
    """
    for env_name in ("SKILLS_PYTHON", "CLAUDE_AUTOMATION_PYTHON"):
        candidate = os.environ.get(env_name)
        if candidate and Path(candidate).exists():
            return candidate
    return sys.executable


def subprocess_timeout_for(mode: str) -> float:
    """How long a caller should wait for the wrapper running ``mode``.

    Derived from the wrapper's own budget — its wall-clock ceiling times the
    retry multiplier — plus a margin for context collection and teardown.
    A hardcoded value here would silently become the real ceiling and kill
    the wrapper mid-run whenever the per-mode timeout is raised.
    """
    base: float | None = None
    env_override = os.environ.get("CODEX_WRAPPER_TIMEOUT_SECONDS")
    if env_override:
        try:
            base = max(1.0, float(env_override))
        except ValueError:
            base = None
    if base is None:
        timeouts = get("wrapper.mode_timeouts", {}) or {}
        default = get("wrapper.default_timeout_seconds", 300.0)
        raw = timeouts.get(mode, default) if isinstance(timeouts, dict) else default
        try:
            base = float(raw)
        except (TypeError, ValueError):
            base = 300.0
    try:
        multiplier = max(1.0, float(get("wrapper.total_deadline_multiplier", 1.5)))
    except (TypeError, ValueError):
        multiplier = 1.5
    return base * multiplier + 90.0


def ensure_setup_complete() -> None:
    """Auto-trigger the setup wizard the first time the skill is used in
    an interactive terminal.

    If ``config.local.json`` already declares ``settings.locale``, this is
    a no-op. If not and ``stdin`` is a TTY, runs ``scripts/setup.py``;
    otherwise returns silently (callers fall back to system locale).

    If the wizard exits non-zero (cancelled, write failure, etc.), emits
    a canonical JSON error on stdout and ``sys.exit(rc)`` so the active
    command does NOT continue with incomplete config.
    """
    local = _safe_load_json(LOCAL_PATH)
    locale_value = (local.get("settings") or {}).get("locale")
    if isinstance(locale_value, str) and locale_value.strip():
        return
    if not sys.stdin.isatty():
        return
    setup_script = SKILL_DIR / "scripts" / "setup.py"
    if not setup_script.exists():
        return
    rc = subprocess.call([resolve_python(), str(setup_script)])
    clear_cache()
    if rc != 0:
        json.dump(
            {
                "status": "error",
                "summary": (
                    "Codex skill setup did not complete (exit code "
                    f"{rc}). Re-run `python scripts/setup.py` to "
                    "configure the interface locale before invoking "
                    "the skill again."
                ),
                "findings": [],
                "severity": "high",
                "confidence": "high",
                "block_recommended": True,
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        sys.stdout.flush()
        sys.exit(rc)
