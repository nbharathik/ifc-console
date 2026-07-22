"""Generate deterministic test IFC files (plan 10 §1).

Doubles as living documentation of the ifcopenshell.api usage the exec tool is
meant to run. Re-run in CI to catch ifcopenshell behavior drift:

    uv run python tests/fixtures/make_fixtures.py
"""

from __future__ import annotations

from pathlib import Path

import ifcopenshell
import ifcopenshell.api.aggregate
import ifcopenshell.api.context
import ifcopenshell.api.geometry
import ifcopenshell.api.owner
import ifcopenshell.api.project
import ifcopenshell.api.pset
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import ifcopenshell.api.unit
import numpy as np

OUT_DIR = Path(__file__).parent / "generated"

# Wall placements (SI meters): two walls form an L on Level 1, the third sits
# on Level 2. Gives the web viewer real geometry and keeps queries stable.
_WALL_LAYOUT = [
    # (origin x, y, z, rotation deg around Z, length m)
    (0.0, 0.0, 0.0, 0.0, 5.0),
    (0.0, 0.0, 0.0, 90.0, 4.0),
    (0.0, 0.0, 3.0, 0.0, 5.0),
]


def _placement_matrix(x: float, y: float, z: float, deg: float) -> np.ndarray:
    theta = np.radians(deg)
    matrix = np.eye(4)
    matrix[0][0] = np.cos(theta)
    matrix[0][1] = -np.sin(theta)
    matrix[1][0] = np.sin(theta)
    matrix[1][1] = np.cos(theta)
    matrix[0][3] = x
    matrix[1][3] = y
    matrix[2][3] = z
    return matrix


def build_minimal(schema: str = "IFC4") -> ifcopenshell.file:
    ifc = ifcopenshell.api.project.create_file(version=schema)
    if schema == "IFC2X3":
        # IFC2X3 makes OwnerHistory mandatory; register a user + application
        # once so every create_entity call below can build it.
        person = ifcopenshell.api.owner.add_person(
            ifc, identification="fixture", family_name="Fixture"
        )
        org = ifcopenshell.api.owner.add_organisation(ifc, identification="ifc-console")
        ifcopenshell.api.owner.add_person_and_organisation(
            ifc, person=person, organisation=org
        )
        ifcopenshell.api.owner.add_application(ifc)
    project = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcProject", name="Duplex")
    length = ifcopenshell.api.unit.add_si_unit(ifc, unit_type="LENGTHUNIT", prefix="MILLI")
    ifcopenshell.api.unit.assign_unit(ifc, units=[length])

    site = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcSite", name="Site")
    building = ifcopenshell.api.root.create_entity(
        ifc, ifc_class="IfcBuilding", name="Building A"
    )
    storey1 = ifcopenshell.api.root.create_entity(
        ifc, ifc_class="IfcBuildingStorey", name="Level 1"
    )
    storey2 = ifcopenshell.api.root.create_entity(
        ifc, ifc_class="IfcBuildingStorey", name="Level 2"
    )
    ifcopenshell.api.aggregate.assign_object(ifc, products=[site], relating_object=project)
    ifcopenshell.api.aggregate.assign_object(ifc, products=[building], relating_object=site)
    ifcopenshell.api.aggregate.assign_object(
        ifc, products=[storey1, storey2], relating_object=building
    )

    space = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcSpace", name="A101")
    ifcopenshell.api.aggregate.assign_object(ifc, products=[space], relating_object=storey1)

    model = ifcopenshell.api.context.add_context(ifc, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        ifc,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=model,
    )

    walls = []
    for i in range(3):
        wall = ifcopenshell.api.root.create_entity(
            ifc, ifc_class="IfcWall", name=f"Wall-{i + 1}"
        )
        storey = storey1 if i < 2 else storey2
        ifcopenshell.api.spatial.assign_container(
            ifc, products=[wall], relating_structure=storey
        )
        x, y, z, deg, length = _WALL_LAYOUT[i]
        representation = ifcopenshell.api.geometry.add_wall_representation(
            ifc, context=body, length=length, height=3.0, thickness=0.2
        )
        ifcopenshell.api.geometry.assign_representation(
            ifc, product=wall, representation=representation
        )
        ifcopenshell.api.geometry.edit_object_placement(
            ifc, product=wall, matrix=_placement_matrix(x, y, z, deg)
        )
        # one wall gets a FireRating; the others intentionally lack it
        props = {"IsExternal": i == 0}
        if i == 0:
            props["FireRating"] = "F30"
        pset = ifcopenshell.api.pset.add_pset(ifc, product=wall, name="Pset_WallCommon")
        ifcopenshell.api.pset.edit_pset(ifc, pset=pset, properties=props)
        walls.append(wall)

    door = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcDoor", name="Door-1")
    ifcopenshell.api.spatial.assign_container(
        ifc, products=[door], relating_structure=storey1
    )
    return ifc


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for schema in ("IFC4", "IFC2X3", "IFC4X3"):
        try:
            ifc = build_minimal(schema)
        except Exception as exc:  # some APIs differ across schemas; IFC4 is required
            if schema == "IFC4":
                raise
            print(f"skip {schema}: {exc}")
            continue
        suffix = schema.lower().replace("x", "x")
        path = OUT_DIR / f"minimal_{suffix}.ifc"
        ifc.write(str(path))
        print(f"wrote {path} ({path.stat().st_size} bytes, {len(ifc.by_type('IfcProduct'))} products)")

    # broken / non-ifc fixtures for error paths
    (OUT_DIR / "broken.ifc").write_text("ISO-10303-21;\nHEADER;\n(truncated", encoding="utf-8")
    (OUT_DIR / "not_ifc.txt").write_text("just some text\n", encoding="utf-8")
    print("wrote broken.ifc, not_ifc.txt")


if __name__ == "__main__":
    main()
