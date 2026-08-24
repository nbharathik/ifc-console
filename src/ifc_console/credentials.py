"""Provider API keys in the system keyring, never in plain text.

The secret lives in the operating system's credential store (Windows
Credential Manager, macOS Keychain, Secret Service on Linux) under the
service name "ifc-console". Only the provider names are indexed in a
non-secret file, because keyrings cannot enumerate portably. Environment
variables remain a supported alternative when an OS credential backend is not
available.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

from ifc_console.core.results import ToolError

log = logging.getLogger("ifc-console.credentials")

SERVICE = "ifc-console"
_INDEX_NAME = "keys.json"


def _backend():
    try:
        import keyring
        from keyring.errors import KeyringError  # noqa: F401
    except ImportError:
        return None
    return keyring


def keyring_available() -> bool:
    return _backend() is not None


def _require_backend():
    backend = _backend()
    if backend is None:
        from ifc_console.agents.environment import missing_dependency_hint

        raise ToolError(
            "EXTRA_NOT_INSTALLED",
            "storing keys needs the bundled keyring package, but it is not installed.",
            missing_dependency_hint("keyring")
            + " Until then, set the provider's environment variable.",
        )
    return backend


def _index_path(home: Path) -> Path:
    return home / _INDEX_NAME


def _load_index(home: Path) -> list[str]:
    try:
        data = json.loads(_index_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    providers = data.get("providers") if isinstance(data, dict) else None
    return sorted({str(p) for p in providers}) if isinstance(providers, list) else []


def _save_index(home: Path, providers: list[str]) -> None:
    path = _index_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps({"version": 1, "providers": sorted(set(providers))}, indent=2) + "\n"
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def set_api_key(home: Path, provider: str, key: str) -> None:
    """Store one provider key in the system keyring."""
    backend = _require_backend()
    cleaned = key.strip()
    if not cleaned:
        raise ToolError(
            "INVALID_INPUT",
            "the key is empty",
            "Pass the provider's API key; `ifc-console keys delete` removes one.",
        )
    try:
        backend.set_password(SERVICE, provider, cleaned)
    except Exception as exc:
        raise ToolError(
            "INTERNAL_ERROR",
            f"the system keyring refused the key: {exc}",
            "Check the OS credential store; environment variables still work.",
        ) from exc
    _save_index(home, [*_load_index(home), provider])


def get_api_key(provider: str) -> str | None:
    """The stored key for one provider, or None. Never raises."""
    backend = _backend()
    if backend is None:
        return None
    try:
        value = backend.get_password(SERVICE, provider)
    except Exception:
        log.debug("keyring read failed for %s", provider, exc_info=True)
        return None
    return value.strip() if value else None


def delete_api_key(home: Path, provider: str) -> bool:
    """Remove one stored key; True when something was deleted."""
    backend = _require_backend()
    existed = False
    try:
        if backend.get_password(SERVICE, provider):
            backend.delete_password(SERVICE, provider)
            existed = True
    except Exception:
        log.debug("keyring delete failed for %s", provider, exc_info=True)
    _save_index(home, [p for p in _load_index(home) if p != provider])
    return existed


def stored_providers(home: Path) -> list[str]:
    """Provider names with a stored key, per the non-secret index."""
    if _backend() is None:
        return []
    return [p for p in _load_index(home) if get_api_key(p)]


__all__ = [
    "SERVICE",
    "delete_api_key",
    "get_api_key",
    "keyring_available",
    "set_api_key",
    "stored_providers",
]
