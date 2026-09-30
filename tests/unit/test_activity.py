"""A burst of reads is one line; failures and edits print at once and in order."""

from __future__ import annotations

from ifc_console.tui.activity import ActivityCoalescer, line


class Clock:
    """A scheduler the test drives by hand."""

    def __init__(self) -> None:
        self.pending: list = []

    def schedule(self, _delay, callback):
        timer = _Timer(callback)
        self.pending.append(timer)
        return timer

    def fire(self) -> None:
        live = [timer for timer in self.pending if not timer.stopped]
        self.pending = []
        for timer in live:
            timer.callback()


class _Timer:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _event(tool, *, ok=True, client="claude-code", ms=10, ts="2026-09-29T12:00:01+00:00"):
    return {"tool": tool, "ok": ok, "client": client, "duration_ms": ms, "ts": ts, "detail": ""}


def _folder(reads=("orient", "get_element", "query_elements")):
    printed: list[str] = []
    clock = Clock()
    folder = ActivityCoalescer(printed.append, lambda tool: tool in reads, clock.schedule)
    return folder, printed, clock


def test_a_burst_of_reads_prints_once_when_it_ends() -> None:
    folder, printed, clock = _folder()

    for tool in ("orient", "get_element", "get_element", "query_elements"):
        folder.add(_event(tool))
    assert printed == []

    clock.fire()

    assert len(printed) == 1
    assert "4 reads" in printed[0]
    assert "get_element x2" in printed[0]
    assert "orient" in printed[0] and "query_elements" in printed[0]
    assert "40ms" in printed[0]


def test_a_lone_read_prints_as_the_call_it_was() -> None:
    folder, printed, clock = _folder()

    folder.add(_event("orient", ms=7))
    clock.fire()

    assert printed == [line(_event("orient", ms=7))]


def test_a_failure_prints_at_once_after_the_reads_before_it() -> None:
    folder, printed, _ = _folder()

    folder.add(_event("orient"))
    folder.add(_event("get_element"))
    folder.add(_event("get_element", ok=False))

    assert len(printed) == 2
    assert "2 reads" in printed[0]
    assert "err" in printed[1]


def test_a_call_that_can_change_the_model_prints_at_once() -> None:
    folder, printed, _ = _folder()

    folder.add(_event("orient"))
    folder.add(_event("set_properties"))

    assert "set_properties" in printed[-1]
    assert len(printed) == 2


def test_reads_from_different_clients_are_not_mixed() -> None:
    folder, printed, clock = _folder()

    folder.add(_event("orient", client="a"))
    folder.add(_event("orient", client="b"))
    clock.fire()

    assert len(printed) == 2
    assert "a" in printed[0] and "b" in printed[1]


def test_each_new_read_pushes_the_end_of_the_burst_back() -> None:
    folder, printed, clock = _folder()

    folder.add(_event("orient"))
    first = clock.pending[-1]
    folder.add(_event("get_element"))

    assert first.stopped is True
    clock.fire()
    assert len(printed) == 1 and "2 reads" in printed[0]


def test_the_client_is_named_unless_it_is_the_generic_http_one() -> None:
    assert "claude-code" in line(_event("orient"))
    assert "http" not in line(_event("orient", client="http"))


def test_markup_in_a_tool_or_client_name_is_escaped() -> None:
    assert "\\[red]x" in line(_event("orient", client="[red]x"))
