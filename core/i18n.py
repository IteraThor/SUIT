"""UI language. Source strings are English; tr() looks them up in the German table.

The choice is kept in ~/.config/suit/settings.json. English is the default.
"""
import json
from pathlib import Path

from core.logger import get_logger

logger = get_logger("i18n")

SETTINGS_PATH = Path.home() / ".config" / "suit" / "settings.json"
LANGUAGES = ("en", "de")
DEFAULT_LANGUAGE = "en"

_language = None


def _read_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("Failed reading %s", SETTINGS_PATH)
        return {}


def get_language() -> str:
    global _language
    if _language is None:
        lang = _read_settings().get("language")
        _language = lang if lang in LANGUAGES else DEFAULT_LANGUAGE
    return _language


def set_language(lang: str) -> None:
    global _language
    if lang not in LANGUAGES:
        raise ValueError(f"Unsupported language: {lang}")
    _language = lang
    settings = _read_settings()
    settings["language"] = lang
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Failed saving language to %s", SETTINGS_PATH)


def tr(text: str) -> str:
    """Translate an English UI string into the active language (unknown strings stay English)."""
    if not text or get_language() == "en":
        return text
    from core.i18n_de import DE
    return DE.get(text, text)


__all__ = ["tr", "get_language", "set_language", "LANGUAGES"]
