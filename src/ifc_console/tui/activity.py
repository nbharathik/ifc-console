"""Folds a burst of read-only tool calls into one line of the feed.

An assistant answering one question makes a dozen reads in a couple of seconds;
a line each buries the one thing worth seeing, an edit or a failure. Reads from
one client that arrive close together are counted and printed once when the
burst ends. Failures and anything that can change the model print at once, in
order, so the feed never reorders what happened.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

from rich.markup import escape

BURST_WINDOW_S = 1.5
_QUIET_CLIENTS = ("http", "ifc-console")


def _who(client: Any) -> str:
    if client and client not in _QUIET_CLIENTS:
        return f"  {escape(str(client))}"
    return ""


def line(event: dict) -> str:
    """One call, printed as it happened."""
    status = "[green]ok[/green]" if event.get("ok") else "[red]err[/red]"
    detail = event.get("detail") or ""
    return (
        f"[dim]{event['ts'][11:19]}[/dim]  {status} [b]{escape(event['tool'])}[/b]  "
        f"[dim]{event.get('duration_ms', 0)}ms  {escape(detail)}{_who(event.get('client'))}[/dim]"
    )


class ActivityCoalescer:
    def __init__(
        self,
        emit: Callable[[str], None],
        is_read: Callable[[str], bool],
        schedule: Callable[[float, Callable[[], None]], Any],
        *,
        window: float = BURST_WINDOW_S,
    ) -> None:
        self._emit = emit
        self._is_read = is_read
        self._schedule = schedule
        self._window = window
        self._events: list[dict] = []
        self._timer: Any = None

    def add(self, event: dict) -> None:
        if not event.get("ok", True) or not self._is_read(event["tool"]):
            self.flush()
            self._emit(line(event))
            return
        if self._events and self._events[-1].get("client") != event.get("client"):
            self.flush()
        self._events.append(event)
        self._stop_timer()
        self._timer = self._schedule(self._window, self.flush)

    def _stop_timer(self) -> None:
        timer, self._timer = self._timer, None
        stop = getattr(timer, "stop", None)
        if callable(stop):
            stop()

    def flush(self) -> None:
        """Print what has been counted so far."""
        self._stop_timer()
        events, self._events = self._events, []
        if not events:
            return
        if len(events) == 1:
            self._emit(line(events[0]))
            return
        counts = Counter(event["tool"] for event in events)
        names = ", ".join(
            f"{escape(name)} x{count}" if count > 1 else escape(name)
            for name, count in counts.most_common()
        )
        total = sum(int(event.get("duration_ms", 0)) for event in events)
        self._emit(
            f"[dim]{events[-1]['ts'][11:19]}[/dim]  [green]ok[/green] "
            f"[b]{len(events)} reads[/b]  [dim]{names}  {total}ms"
            f"{_who(events[-1].get('client'))}[/dim]"
        )
