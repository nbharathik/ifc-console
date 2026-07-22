"""Tiny in-process pub/sub.

Events are plain dicts with a "type" key. Emission is synchronous on the
caller's thread; code that must touch the UI marshals itself (the TUI
subscriber does that). By design, tool handlers emit from the event loop;
worker threads return results and the loop-side caller emits.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone

log = logging.getLogger("ifc-console.events")

Subscriber = Callable[[dict], None]


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Subscriber] = []

    def subscribe(self, cb: Subscriber) -> Callable[[], None]:
        self._subs.append(cb)

        def unsubscribe() -> None:
            if cb in self._subs:
                self._subs.remove(cb)

        return unsubscribe

    def emit(self, type_: str, **payload) -> None:
        event = {"type": type_, "ts": datetime.now(timezone.utc).isoformat(), **payload}
        for cb in list(self._subs):
            try:
                cb(event)
            except Exception:  # subscriber bugs must never break tool calls
                log.exception("event subscriber failed for %s", type_)
