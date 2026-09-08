"""Table-like lines of a PDF page become positional rows, and pages become a digest."""

from __future__ import annotations

from ifc_console.knowledge.ingest import pdf_digest, pdf_table_rows

PAGE = """6 ArcelorMittal | General Catalogue 2027
Section Width Height Thickness Sectional Mass
b h tf tw
mm mm mm mm cm2/m kg/m
AZ-800
AZ 18-8003) 800 449 8.5 8.5 129 80.7
AZ 19-800 800 450 9.0 9.0 135 84.5
GU 6N 600 309 6.0 6.0 89 41.9 70 9670 625 375 765 - 3 3 3 4 4 4 4 –
1) Global Warming Potential data according to the EPD.
To optimise the design use our free software Durability.
"""

PER_PILE = """S = Single pile
GU 6N Per S 89 41.9 1 2
Per D 178 83.8 3 4
Per m of wall 148 69.8 5 6
GU 7N Per S 95 44.8 7 8
Per D 190 89.6 9 10
"""


def test_rows_keep_names_values_and_the_header_above_the_block():
    rows = pdf_table_rows(PAGE)
    names = [row["name"] for row in rows]
    assert names == ["AZ 18-800", "AZ 19-800", "GU 6N"]
    first = rows[0]
    assert first["values"][:4] == [800, 449, 8.5, 8.5]
    assert first["v0"] == 800 and first["v5"] == 80.7
    assert "b h tf tw" in first["header"] and "mm mm mm mm" in first["header"]
    gu = rows[2]
    assert gu["values"][11] is None and gu["values"][-1] is None
    assert gu["v1"] == 309


def test_continuation_rows_inherit_the_previous_name():
    names = [row["name"] for row in pdf_table_rows(PER_PILE)]
    assert names == [
        "GU 6N Per S",
        "GU 6N Per D",
        "GU 6N Per m of wall",
        "GU 7N Per S",
        "GU 7N Per D",
    ]


def test_prose_lines_and_footnotes_are_not_rows():
    rows = pdf_table_rows("Some heading\n1) Footnote about 2023 and 2024 values.\nTotal 3 4\n")
    assert rows == []


def test_digest_lists_titles_and_row_names_per_page():
    rows = {1: pdf_table_rows(PAGE)}
    digest = pdf_digest([(1, PAGE), (2, "Delivery conditions\nsome text")], rows)
    assert digest.startswith(
        "p1: 6 ArcelorMittal | General Catalogue 2027 | 3 table rows: AZ 18-800, AZ 19-800, GU 6N"
    )
    assert "p2: Delivery conditions" in digest
