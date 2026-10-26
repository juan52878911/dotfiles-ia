"""Message catalogs. t("key", **fmt) looks the key up in i18n/<locale>.json, falling back
to English. Keep keys stable; add both languages in the same commit."""
import json
import os

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "i18n")
_cache = {}


def _load(locale):
    if locale not in _cache:
        try:
            with open(os.path.join(_DIR, f"{locale}.json"), encoding="utf-8") as f:
                _cache[locale] = json.load(f)
        except (OSError, ValueError):
            _cache[locale] = {}
    return _cache[locale]


def locale():
    return os.environ.get("DELEGA_LOCALE", "en")


def t(key, loc=None, **fmt):
    loc = loc or locale()
    text = _load(loc).get(key) or _load("en").get(key) or key
    try:
        return text.format(**fmt)
    except (KeyError, IndexError):
        return text
