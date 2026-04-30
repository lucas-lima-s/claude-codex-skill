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
from pathlib import Path
from typing import Any, Dict, Optional

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULTS_PATH = SKILL_DIR / "config.default.json"
LOCAL_PATH = SKILL_DIR / "config.local.json"

DEFAULT_LOCALE = "pt-BR"

_cached_config: Optional[Dict[str, Any]] = None
_cached_locale: Optional[str] = None


def _safe_load_json(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` into ``base`` (in-place on a copy)."""
    out = dict(base)
    for key, value in override.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_config() -> Dict[str, Any]:
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
      2. System locale via ``locale.getdefaultlocale()``.
      3. ``DEFAULT_LOCALE`` (pt-BR).

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
    template: Optional[str] = None
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
