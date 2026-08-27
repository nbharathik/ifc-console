"""Analysis tools: schema validation, IDS checking, quantities, georeferencing."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

IDS_FIXTURE = Path(__file__).parent.parent / "fixtures" / "wall_firerating.ids"


async def test_validate_model_clean_fixture(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    out = await h.call("validate_model")
    assert out["ok"] is True
    assert out["data"]["valid"] is True
    assert out["data"]["issue_count"] == 0
    warm = await h.call("validate_model")
    assert warm["meta"]["cached"] is True


async def test_validate_model_requires_model(harness_factory):
    h = await harness_factory(model=None)
    out = await h.call("validate_model")
    assert out["ok"] is False
    assert out["error"]["code"] == "NO_MODEL_LOADED"


async def test_validate_ids_reports_failing_walls(
    harness_factory, work_model: Path, tmp_path: Path
):
    h = await harness_factory(model=work_model)
    ids_copy = tmp_path / "rules.ids"
    shutil.copy2(IDS_FIXTURE, ids_copy)
    out = await h.call("validate_ids", ids_path=str(ids_copy))
    assert out["ok"] is True
    data = out["data"]
    assert data["passed"] is False
    assert data["totals"] == {"specifications": 1, "passed": 0, "failed": 1}
    spec = data["specifications"][0]
    assert spec["applicable_count"] == 3
    assert spec["pass_count"] == 1
    assert spec["fail_count"] == 2
    failed = spec["requirements"][0]["failed"]
    assert len(failed) == 2
    assert all(item["global_id"] for item in failed)
    assert all(item["reason"] for item in failed)


async def test_validate_ids_path_outside_allowed(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    out = await h.call("validate_ids", ids_path="/definitely/not/allowed/rules.ids")
    assert out["ok"] is False
    assert out["error"]["code"] == "PATH_NOT_ALLOWED"


async def test_validate_ids_missing_file(harness_factory, work_model: Path, tmp_path: Path):
    h = await harness_factory(model=work_model)
    out = await h.call("validate_ids", ids_path=str(tmp_path / "missing.ids"))
    assert out["ok"] is False
    assert out["error"]["code"] == "FILE_NOT_FOUND"


async def test_compute_quantities_by_class(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    out = await h.call("compute_quantities", selector="IfcWall")
    assert out["ok"] is True
    data = out["data"]
    assert data["matched"] == 3
    assert data["source"] == "stored"
    assert sum(group["count"] for group in data["groups"]) == 3
    assert data["groups"][0]["group"] == "IfcWall"
    assert isinstance(data["totals"], dict)
    assert "length" in data["units"]


async def test_compute_quantities_by_storey(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    out = await h.call("compute_quantities", selector="IfcWall", aggregate_by="storey")
    assert out["ok"] is True
    counts = {g["group"]: g["count"] for g in out["data"]["groups"]}
    assert sum(counts.values()) == 3
    assert len(counts) == 2  # walls sit on two storeys in the fixture


async def test_compute_quantities_bad_selector(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    out = await h.call("compute_quantities", selector="NotAnIfcClass!!!")
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_QUERY"


async def test_export_csv_in_ask_mode(harness_factory, work_model: Path, tmp_path: Path):
    """Writing a report file is not editing the model, so ask mode allows it."""
    h = await harness_factory(model=work_model)
    target = tmp_path / "walls.csv"
    out = await h.call(
        "export_csv",
        selector="IfcWall",
        path=str(target),
        properties=["Pset_WallCommon.FireRating"],
    )
    assert out["ok"] is True
    assert out["data"]["rows"] == 3
    assert "Pset_WallCommon.FireRating" in out["data"]["columns"]
    text = target.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line]
    assert len(lines) == 4  # header + three walls
    assert "global_id" in lines[0]
    assert "F30" in text
    # the write lands in the audit trail
    events = [record["ev"] for record in h.core.audit.tail(20)]
    assert "artifact_write" in events


async def test_analyze_element_geometry_reads_profile_and_mesh(
    harness_factory, work_model: Path
):
    h = await harness_factory(model=work_model)
    out = await h.call("analyze_element_geometry", selector="IfcWall, Name=Wall-1")
    assert out["ok"] is True
    record = out["data"]["elements"][0]
    dims = record["dimensions"]
    # profile plane: the wall's plan footprint, in file units (mm)
    assert dims["width"]["file"] == pytest.approx(5000.0, rel=1e-3)
    assert dims["width"]["source"] == "profile_curve"
    assert dims["length"]["file"] == pytest.approx(3000.0, rel=1e-3)
    assert dims["length"]["source"] == "extrusion_depth"
    # the mesh cuts across the run, a different plane, and says so
    assert "profile_plane_differs" in record["flags"]
    cut = record["cross_section"]
    assert cut["width"] == pytest.approx(3.0, rel=1e-2)
    assert cut["height"] == pytest.approx(0.2, rel=1e-2)
    assert cut["thickness"]["median"] == pytest.approx(0.2, rel=1e-2)
    warm = await h.call("analyze_element_geometry", selector="IfcWall, Name=Wall-1")
    assert warm["meta"]["cached"] is True


async def test_geometry_tools_tessellate_an_element_once(
    harness_factory, work_model: Path, monkeypatch
):
    """A probe followed by a measurement of the same wall pays for the mesh once."""
    from ifc_console.ifc import geometry

    h = await harness_factory(model=work_model)
    real = geometry._tessellate
    calls: list[list[int]] = []

    def counting(ifc, elements):
        calls.append(sorted(e.id() for e in elements))
        return real(ifc, elements)

    monkeypatch.setattr(geometry, "_tessellate", counting)

    probed = await h.call("get_element_geometry", selector="IfcWall, Name=Wall-1")
    assert probed["ok"] is True
    gid = probed["data"]["elements"][0]["global_id"]
    assert len(calls) == 1

    measured = await h.call(
        "measure_elements", method="geometry_extent", global_ids=[gid], axis="local_y"
    )
    assert measured["ok"] is True
    assert measured["data"]["elements"][0]["value"] == pytest.approx(200.0, rel=1e-2)
    assert len(calls) == 1  # served from the mesh cache


async def test_mesh_cache_never_serves_a_mutated_model(
    harness_factory, work_model: Path, monkeypatch
):
    from ifc_console.ifc import geometry

    h = await harness_factory(model=work_model)
    real = geometry._tessellate
    calls: list[list[int]] = []

    def counting(ifc, elements):
        calls.append(sorted(e.id() for e in elements))
        return real(ifc, elements)

    monkeypatch.setattr(geometry, "_tessellate", counting)
    await h.call("get_element_geometry", selector="IfcWall, Name=Wall-1")
    h.core.session.mark_dirty()
    await h.call("get_element_geometry", selector="IfcWall, Name=Wall-1")
    assert len(calls) == 2


async def test_geometry_tools_page_with_offset(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    every = await h.call("get_element_geometry", selector="IfcWall")
    assert every["data"]["returned"] == 3

    page = await h.call("get_element_geometry", selector="IfcWall", offset=2)
    assert page["ok"] is True
    assert page["data"]["returned"] == 1
    assert page["meta"]["offset"] == 2
    last = every["data"]["elements"][2]["global_id"]
    assert page["data"]["elements"][0]["global_id"] == last

    measured = await h.call(
        "measure_elements", method="stored_qto", quantity="Width", selector="IfcWall", offset=2
    )
    assert measured["ok"] is True
    assert measured["data"]["summary"]["count"] == 1
    assert measured["data"]["elements"][0]["global_id"] == last

    analyzed = await h.call("analyze_element_geometry", selector="IfcWall", offset=2)
    assert analyzed["ok"] is True
    assert analyzed["data"]["matched"] == 1
    assert analyzed["data"]["selector"] == "IfcWall"
    assert analyzed["data"]["elements"][0]["global_id"] == last

    past = await h.call("get_element_geometry", selector="IfcWall", offset=9)
    assert past["ok"] is False
    assert past["error"]["code"] == "NO_MATCH"


async def test_export_csv_empty_fields_means_no_extra_columns(
    harness_factory, work_model: Path, tmp_path: Path
):
    h = await harness_factory(model=work_model)
    target = tmp_path / "bare.csv"
    out = await h.call("export_csv", selector="IfcWall", path=str(target), fields=[])
    assert out["ok"] is True
    assert out["data"]["columns"] == ["global_id", "class"]


async def test_export_csv_property_hint_names_a_real_tool(
    harness_factory, work_model: Path, tmp_path: Path
):
    h = await harness_factory(model=work_model)
    out = await h.call(
        "export_csv",
        selector="IfcWall",
        path=str(tmp_path / "walls.csv"),
        properties=["FireRating"],
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"
    assert "get_element_details" not in out["error"]["hint"]
    assert "get_element " in out["error"]["hint"]


async def test_export_measurement_report_writes_and_registers(
    harness_factory, work_model: Path, tmp_path: Path
):
    h = await harness_factory(model=work_model)
    target = tmp_path / "wall-report.md"
    out = await h.call(
        "export_measurement_report",
        selector="IfcWall, Name=Wall-1",
        path=str(target),
        title="Wall 1 measurement",
        notes="fixture check",
    )
    assert out["ok"] is True
    assert out["data"]["elements"] == 1
    assert out["data"]["artifact_id"].startswith("sha256:")
    text = target.read_text(encoding="utf-8")
    assert "# Wall 1 measurement" in text
    assert "| Width (b) | 5000" in text
    assert "MILLIMETRE" in text
    listed = await h.call("list_artifacts")
    kinds = [row["kind"] for row in listed["data"]["artifacts"]]
    assert "measurement-report" in kinds

    again = await h.call(
        "export_measurement_report", selector="IfcWall", path=str(target)
    )
    assert again["ok"] is False
    assert again["error"]["code"] == "FILE_EXISTS"


async def test_export_csv_refuses_overwrite_and_bad_paths(
    harness_factory, work_model: Path, tmp_path: Path
):
    h = await harness_factory(model=work_model)
    target = tmp_path / "walls.csv"
    target.write_text("existing", encoding="utf-8")
    out = await h.call("export_csv", selector="IfcWall", path=str(target))
    assert out["ok"] is False
    assert out["error"]["code"] == "FILE_EXISTS"

    out = await h.call("export_csv", selector="IfcWall", path=str(target), overwrite=True)
    assert out["ok"] is True

    out = await h.call("export_csv", selector="IfcWall", path=str(tmp_path / "walls.txt"))
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"

    out = await h.call(
        "export_csv", selector="IfcWall", path="/definitely/not/allowed/walls.csv"
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "PATH_NOT_ALLOWED"


async def test_exec_namespace_helpers(harness_factory, work_model: Path):
    """The injected read helpers work in guarded (ask-mode) runs."""
    h = await harness_factory(model=work_model)
    out = await h.call(
        "execute_ifc_code",
        code=(
            "walls = by_class('IfcWall')\n"
            "rated = [w for w in walls if psets(w).get('Pset_WallCommon', {})"
            ".get('FireRating')]\n"
            "(len(walls), len(rated), container(walls[0]).is_a(), "
            "isinstance(qtos(walls[0]), dict))"
        ),
    )
    assert out["ok"] is True
    assert out["data"]["result"] == "(3, 1, 'IfcBuildingStorey', True)"


async def test_get_georeferencing_on_plain_fixture(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    out = await h.call("get_georeferencing")
    assert out["ok"] is True
    data = out["data"]
    assert data["georeferenced"] is False
    assert "true_north_degrees" in data
    assert "map_conversion" in data


async def test_orient_bundles_status_project_and_tree(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    out = await h.call("orient")
    assert out["ok"] is True
    data = out["data"]
    assert data["status"]["model"]["loaded"] is True
    assert data["status"]["mode"] == "ask"
    assert data["project"]["schema"] == "IFC4"
    assert data["spatial_tree"]["class"] == "IfcProject"
    warm = await h.call("orient")
    assert warm["meta"]["cached"] is True


async def test_orient_without_model(harness_factory):
    h = await harness_factory(model=None)
    out = await h.call("orient")
    assert out["ok"] is True
    assert out["data"]["status"]["model"]["loaded"] is False
    assert "project" not in out["data"]
    assert "open_ifc_file" in out["data"]["hint"]


async def test_describe_capabilities_tracks_viewer_category(
    harness_factory, work_model: Path
):
    h = await harness_factory(model=work_model)
    out = await h.call("describe_capabilities")
    assert out["ok"] is True
    names = {tool["name"] for tool in out["data"]["tools"]}
    assert {"orient", "validate_model", "compute_quantities", "export_csv"} <= names
    assert "highlight_elements" not in names
    assert out["data"]["mode"]["current"] == "ask"
    purposes = {tool["name"]: tool["purpose"] for tool in out["data"]["tools"]}
    assert not purposes["orient"].startswith("[")

    h.core.enable_viewer()
    out = await h.call("describe_capabilities")
    names = {tool["name"] for tool in out["data"]["tools"]}
    assert "highlight_elements" in names and "apply_color_theme" in names
