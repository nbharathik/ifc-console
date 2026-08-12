"""Authenticated identity proof for loopback bridge connections."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

IDENTITY_PATH = "/api/identify"
IDENTITY_NONCE_BYTES = 32
IDENTITY_RESPONSE_LIMIT = 4096

_IDENTITY_CONTEXT = "ifc-console-loopback-identity-v1"
_NONCE = re.compile(r"[0-9a-f]{64}\Z")


def valid_identity_nonce(nonce: object) -> bool:
    """Accept exactly one canonical 256-bit hexadecimal nonce."""
    return isinstance(nonce, str) and _NONCE.fullmatch(nonce) is not None


def identity_proof(token: str, nonce: str, port: int) -> str:
    """Return a domain-separated proof bound to one endpoint and port."""
    if not valid_identity_nonce(nonce):
        raise ValueError("identity nonce must be 64 lowercase hexadecimal characters")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("identity port must be between 1 and 65535")
    message = f"{_IDENTITY_CONTEXT}\nGET\n{IDENTITY_PATH}\n{port}\n{nonce}".encode("ascii")
    return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def identity_matches(payload: Any, token: str, nonce: str, port: int) -> bool:
    """Validate a complete identity response with constant-time proof comparison."""
    if not isinstance(payload, dict):
        return False
    if payload.get("name") != "ifc-console":
        return False
    if payload.get("nonce") != nonce or type(payload.get("port")) is not int:
        return False
    if payload["port"] != port or not isinstance(payload.get("proof"), str):
        return False
    try:
        expected = identity_proof(token, nonce, port)
        return hmac.compare_digest(payload["proof"], expected)
    except (UnicodeError, ValueError):
        return False


__all__ = [
    "IDENTITY_NONCE_BYTES",
    "IDENTITY_PATH",
    "IDENTITY_RESPONSE_LIMIT",
    "identity_matches",
    "identity_proof",
    "valid_identity_nonce",
]
