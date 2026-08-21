"""File-kind detection: the sniff wins over a lying extension."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from ifc_console.mcp.envelope import ToolError
from ifc_console.workspace.kinds import describe_file, detect_kind

IDS_FIXTURE = Path(__file__).parents[1] / "fixtures" / "wall_firerating.ids"


def test_detects_ifc_by_header(minimal_ifc4_path: Path) -> None:
    assert detect_kind(minimal_ifc4_path) == "ifc"
    assert describe_file(minimal_ifc4_path)["schema"] == "IFC4"


def test_lying_extension_is_classified_by_content(tmp_path: Path, minimal_ifc4_path: Path) -> None:
    liar = tmp_path / "model.csv"
    liar.write_bytes(minimal_ifc4_path.read_bytes())
    assert detect_kind(liar) == "ifc"


def test_ids_detected_and_described() -> None:
    assert detect_kind(IDS_FIXTURE) == "ids"
    detail = describe_file(IDS_FIXTURE)
    assert detail["title"] == "Wall fire rating"
    assert detail["specifications"] == 1


def test_ids_without_the_extension_still_sniffs(tmp_path: Path) -> None:
    copy = tmp_path / "requirements.xml"
    copy.write_bytes(IDS_FIXTURE.read_bytes())
    assert detect_kind(copy) == "ids"


def test_ids_title_with_namespace_prefix(tmp_path: Path) -> None:
    prefixed = tmp_path / "prefixed.ids"
    prefixed.write_text(
        '<ids:ids xmlns:ids="http://standards.buildingsmart.org/IDS">'
        "<ids:info><ids:title>Fire spec</ids:title></ids:info>"
        "<ids:specifications><ids:specification/></ids:specifications>"
        "</ids:ids>",
        encoding="utf-8",
    )
    detail = describe_file(prefixed, "ids")
    assert detail["title"] == "Fire spec"
    assert detail["specifications"] == 1


def test_zip_members_decide_ifczip_and_bcf(tmp_path: Path, minimal_ifc4_path: Path) -> None:
    ifczip = tmp_path / "model.ifczip"
    with zipfile.ZipFile(ifczip, "w") as zf:
        zf.write(minimal_ifc4_path, arcname="model.ifc")
    assert detect_kind(ifczip) == "ifc"

    bcf = tmp_path / "issues.bcfzip"
    with zipfile.ZipFile(bcf, "w") as zf:
        zf.writestr("bcf.version", "<Version/>")
        zf.writestr("topic-1/markup.bcf", "<Markup/>")
    assert detect_kind(bcf) == "bcf"
    assert describe_file(bcf)["topics"] == 1


def test_unknown_and_unreadable_files_are_not_kinds(tmp_path: Path) -> None:
    plain = tmp_path / "notes.xyz"
    plain.write_text("no recognized kind", encoding="utf-8")
    assert detect_kind(plain) is None
    assert detect_kind(tmp_path / "missing.ifc") == "ifc"  # extension only, no content
    empty = tmp_path / "empty.ids"
    empty.write_bytes(b"")
    assert detect_kind(empty) == "ids"
    assert describe_file(empty) == {"specifications": 0}


def test_broken_zip_does_not_raise(tmp_path: Path) -> None:
    fake = tmp_path / "broken.bcfzip"
    fake.write_bytes(b"PK\x03\x04garbage")
    assert detect_kind(fake) == "bcf"  # extension fallback
    assert describe_file(fake) == {}


async def test_attachment_alias_must_match_the_requested_kind(core, tmp_path: Path) -> None:
    table = tmp_path / "schedule.csv"
    table.write_text("name,value\nwall,1\n", encoding="utf-8")
    attachment = await core.attach_file(table, alias="schedule")
    assert attachment.kind == "csv"

    with pytest.raises(ToolError) as excinfo:
        core.resolve_attachment("schedule", kind="ids")
    assert excinfo.value.code == "INVALID_INPUT"
