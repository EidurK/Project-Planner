"""User settings persisted in data/settings.json (currently: the model the agents reason with)."""
import json
import os

import anthropic

from . import storage

# id, label, price per million tokens (input/output)
MODELS = [
    ("claude-sonnet-5-5", "Claude Sonnet 5.5", "$2 / $10"),
    ("claude-opus-5-5", "Claude Opus 5.5", "$4 / $20"),
    ("claude-fable-5-1", "Claude Fable 5.1", "$10 / $50"),
    ("claude-opus-5", "Claude Opus 5", "$5 / $25"),
    ("claude-sonnet-5", "Claude Sonnet 5", "$2 / $10"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6", "$3 / $15"),
    ("claude-haiku-4-5", "Claude Haiku 4.5", "$1 / $5"),
]
MODEL_IDS = [m[0] for m in MODELS]
FILE = "settings.json"


def _load() -> dict:
    raw = storage.read_text(FILE)
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


def get_model() -> str:
    model = _load().get("model") or os.environ.get("MODEL") or MODEL_IDS[0]
    return model


def set_model(model: str) -> bool:
    """Save the model. Returns True if it changed."""
    if model not in MODEL_IDS:
        raise ValueError(f"Unknown model '{model}'")
    data = _load()
    changed = data.get("model") != model and get_model() != model
    data["model"] = model
    storage.write_text(FILE, json.dumps(data, indent=2) + "\n")
    return changed


def public() -> dict:
    current = get_model()
    models = [{"id": i, "label": label, "price": price} for i, label, price in MODELS]
    if current not in MODEL_IDS:  # e.g. an unusual MODEL from .env: still show what is in use
        models.append({"id": current, "label": current, "price": ""})
    return {"model": current, "models": models, "api_key": api_key_status()}


# ---- API key ----
# Kept in data/secrets.json (written atomically with owner-only permissions), separate from settings.json.
# Never returned to the browser in full and not reachable by any agent tool.

SECRETS = "secrets.json"


def get_api_key() -> str | None:
    """The key saved in the app, if any (it takes priority over ANTHROPIC_API_KEY)."""
    raw = storage.read_text(SECRETS)
    try:
        return (json.loads(raw).get("api_key") or None) if raw else None
    except json.JSONDecodeError:
        return None


def _mask(key: str) -> str:
    return f"{key[:7]}…{key[-4:]}" if len(key) > 14 else "…" + key[-4:]


def api_key_status() -> dict:
    saved = get_api_key()
    env = os.environ.get("ANTHROPIC_API_KEY")
    if saved:
        return {"configured": True, "source": "app", "hint": _mask(saved)}
    if env:
        return {"configured": True, "source": "environment", "hint": _mask(env)}
    return {"configured": False, "source": None, "hint": ""}


def verify_key(key: str) -> str | None:
    """Ask Anthropic whether the key works. Raises ValueError if it is rejected;
    returns a warning string if it could not be checked (e.g. no network)."""
    try:
        anthropic.Anthropic(api_key=key, max_retries=0, timeout=15).models.list(limit=1)
    except anthropic.AuthenticationError:
        raise ValueError("Anthropic rejected this API key.") from None
    except anthropic.APIError:
        return "Saved, but the key could not be verified right now (network or API problem)."
    return None


def set_api_key(key: str) -> str | None:
    key = (key or "").strip()
    if len(key) < 20 or any(c.isspace() for c in key):
        raise ValueError("That doesn't look like an API key.")
    warning = verify_key(key)
    storage.write_text(SECRETS, json.dumps({"api_key": key}) + "\n")
    return warning


def clear_api_key() -> None:
    storage.delete_text(SECRETS)
