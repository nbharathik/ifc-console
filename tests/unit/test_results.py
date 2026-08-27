"""Envelope paging: an oversized result keeps rows, not a clipped string."""

from __future__ import annotations

from ifc_console.core.results import dump, ok


def _rows(count: int) -> list[dict[str, str]]:
    return [
        {"global_id": f"{i:022d}", "class": "IfcWall", "name": f"Basic Wall:Interior:{i}"}
        for i in range(count)
    ]


def _rendered(envelope) -> str:
    return dump({"ok": True, "data": envelope.data, "meta": envelope.meta})


def test_oversized_list_is_paged_not_stringified() -> None:
    out = ok({"rows": _rows(200)}, {"mode": "ask"}, char_limit=6000, total=200, returned=200)

    assert isinstance(out.data["rows"], list)
    assert "preview" not in out.data
    assert 0 < len(out.data["rows"]) < 200
    assert out.meta["truncated"] is True


def test_paged_envelope_fits_the_char_limit() -> None:
    out = ok({"rows": _rows(500)}, {"mode": "ask"}, char_limit=6000)
    assert len(_rendered(out)) <= 6000


def test_preview_fallback_also_fits_the_char_limit() -> None:
    """The old preview re-embedded the whole envelope, so escaping pushed it
    past the very limit it enforced."""
    out = ok({"blob": 'quote"and\\slash' * 900}, {"mode": "ask"}, char_limit=4000)

    assert "preview" in out.data
    assert out.meta["truncated"] is True
    assert len(_rendered(out)) <= 4000


def test_truncation_survives_a_front_cut() -> None:
    out = ok({"rows": _rows(500)}, {"mode": "ask"}, char_limit=40_000, total=500, offset=0)
    head = _rendered(out)[:1000]

    assert "truncation" in head
    assert "next_offset" in head


def test_a_tool_without_an_offset_is_told_to_shrink_the_batch() -> None:
    out = ok({"elements": _rows(200)}, {"mode": "ask"}, char_limit=6000, returned=200)
    cut = out.data["truncation"]

    assert cut["key"] == "elements"
    assert "next_offset" not in cut
    assert "smaller batch" in cut["retry"]


def test_next_offset_continues_from_the_requested_offset() -> None:
    out = ok({"rows": _rows(200)}, {"mode": "ask"}, char_limit=6000, offset=150, returned=200)
    cut = out.data["truncation"]

    assert cut["key"] == "rows"
    assert cut["kept"] == len(out.data["rows"])
    assert cut["of"] == 200
    assert cut["next_offset"] == 150 + cut["kept"]
    assert out.meta["returned"] == cut["kept"]


def test_paging_keeps_the_other_data_keys() -> None:
    out = ok(
        {"rows": _rows(200), "note": "walls only", "unknown_classes": []},
        {"mode": "ask"},
        char_limit=6000,
    )

    assert out.data["note"] == "walls only"
    assert out.data["unknown_classes"] == []
    assert list(out.data)[0] == "truncation"


def test_a_result_that_fits_is_untouched() -> None:
    out = ok({"rows": _rows(2)}, {"mode": "ask"}, char_limit=40_000)

    assert len(out.data["rows"]) == 2
    assert "truncation" not in out.data
    assert "truncated" not in out.meta
