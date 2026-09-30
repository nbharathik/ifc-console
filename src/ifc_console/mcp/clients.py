"""Who is connected: every MCP request names its client, and the console lists them.

The HTTP transport is stateless, so a client is not a connection but a label
seen on requests: the bridge sends the `clientInfo` it read from `initialize`,
and any other client is known by its User-Agent.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

HEADER = b"x-ifc-client"
_AGENT = b"user-agent"
_LABEL_MAX = 48
_SAFE = re.compile(r"[^A-Za-z0-9._/+ -]")
# A client that has been quiet this long is no longer listed as connected.
_ACTIVE_S = 15 * 60


def _clean(value: str) -> str:
    return _SAFE.sub("", value).strip()[:_LABEL_MAX]


def label_for(scope: dict[str, Any]) -> tuple[str, str]:
    """(label, transport) for one request scope."""
    named = agent = None
    for name, value in scope.get("headers", []):
        lowered = name.lower()
        if lowered == HEADER:
            named = value.decode("latin-1", errors="replace")
        elif lowered == _AGENT:
            agent = value.decode("latin-1", errors="replace")
    if named and _clean(named):
        return _clean(named), "bridge"
    if agent:
        product = _clean(agent.split(" ", 1)[0])
        if product:
            return product, "http"
    return "unknown", "http"


@dataclass
class ClientRecord:
    label: str
    transport: str
    profile: str | None = None
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    calls: int = 0
    errors: int = 0
    last_tool: str | None = None


class ClientRegistry:
    def __init__(self) -> None:
        self._clients: dict[str, ClientRecord] = {}

    def seen(self, label: str, transport: str, profile: str | None = None) -> ClientRecord:
        record = self._clients.get(label)
        if record is None:
            record = self._clients[label] = ClientRecord(label, transport, profile)
        record.transport = transport
        record.last_seen = time.time()
        if profile:
            record.profile = profile
        return record

    def called(self, label: str | None, tool: str, ok: bool) -> None:
        if not label or label not in self._clients:
            return
        record = self._clients[label]
        record.calls += 1
        record.last_tool = tool
        record.last_seen = time.time()
        if not ok:
            record.errors += 1

    def active(self, *, within: float = _ACTIVE_S) -> list[ClientRecord]:
        cutoff = time.time() - within
        rows = [record for record in self._clients.values() if record.last_seen >= cutoff]
        return sorted(rows, key=lambda record: -record.last_seen)

    def count(self) -> int:
        return len(self.active())
