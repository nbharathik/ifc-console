"""Process memory readings never raise and always carry the same keys."""

from __future__ import annotations

from ifc_console import resources


def test_process_memory_reports_the_same_shape_everywhere() -> None:
    reading = resources.process_memory()
    assert set(reading) == {"rss_bytes", "peak_rss_bytes", "total_bytes", "available_bytes"}
    for value in reading.values():
        assert value is None or (isinstance(value, int) and value >= 0)


def test_process_memory_sees_this_process_on_supported_platforms() -> None:
    reading = resources.process_memory()
    # Every supported platform reports at least the machine size; the resident
    # size is expected wherever a reader exists for it.
    assert reading["total_bytes"] is None or reading["total_bytes"] > 0
    if reading["rss_bytes"] is not None:
        assert reading["rss_bytes"] > 1024 * 1024


def test_a_broken_platform_reader_degrades_to_unknown(monkeypatch) -> None:
    def explode() -> tuple[int | None, int | None]:
        raise OSError("no psapi here")

    monkeypatch.setattr(resources, "_windows_process", explode)
    monkeypatch.setattr(resources, "_posix_process", explode)
    monkeypatch.setattr(resources, "_windows_system", explode)
    monkeypatch.setattr(resources, "_posix_system", explode)
    assert resources.process_memory() == {
        "rss_bytes": None,
        "peak_rss_bytes": None,
        "total_bytes": None,
        "available_bytes": None,
    }
