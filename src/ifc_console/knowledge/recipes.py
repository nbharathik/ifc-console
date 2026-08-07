"""The recipe cookbook: verified snippets for execute_ifc_code.

Every recipe with `verify` set is executed against a fixture model by
tests/unit/test_recipes.py, so what the LLM retrieves is code that ran, not
code that looked plausible. `read` recipes run in ask mode (no mutation),
`edit` recipes run in edit mode against a throwaway copy.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from ifc_console.knowledge.records import Record


@dataclass(frozen=True)
class Recipe:
    slug: str
    title: str
    summary: str
    code: str
    tags: tuple[str, ...] = ()
    mode: str = "ask"  # ask = read-only, edit = mutates the model
    notes: str = ""
    verify: str | None = None  # read | edit | None


RECIPES: list[Recipe] = [
    # ------------------------------------------------------------------ read
    Recipe(
        slug="count-by-class",
        title="Count elements by IFC class",
        summary="How many of each class the model holds, biggest first.",
        tags=("count", "inventory", "overview"),
        verify="read",
        code="""
from collections import Counter
counts = Counter(e.is_a() for e in by_class("IfcElement"))
dict(counts.most_common())
""",
    ),
    Recipe(
        slug="elements-without-name",
        title="Find unnamed elements",
        summary="Elements whose Name is missing or blank, grouped by class.",
        tags=("quality", "naming", "audit"),
        verify="read",
        code="""
from collections import defaultdict
missing = defaultdict(list)
for e in by_class("IfcElement"):
    if not (e.Name or "").strip():
        missing[e.is_a()].append(e.GlobalId)
{k: {"count": len(v), "examples": v[:5]} for k, v in missing.items()}
""",
    ),
    Recipe(
        slug="property-value-histogram",
        title="Group elements by one property value",
        summary="Distribution of a property across a selection, including the elements that lack it.",
        tags=("property", "pset", "report"),
        verify="read",
        code="""
from collections import Counter
selector = "IfcWall"
pset_name, prop = "Pset_WallCommon", "FireRating"
values = Counter()
for e in query(selector):
    values[(psets(e).get(pset_name) or {}).get(prop, "(missing)")] += 1
dict(values)
""",
    ),
    Recipe(
        slug="find-property-anywhere",
        title="Find which property set carries a property",
        summary="Search every element for a property name when you do not know its pset.",
        tags=("property", "search", "pset"),
        verify="read",
        code="""
wanted = "FireRating"
found = {}
for e in by_class("IfcElement"):
    for pset_name, props in psets(e).items():
        if wanted in props:
            found.setdefault(f"{pset_name}.{wanted}", 0)
            found[f"{pset_name}.{wanted}"] += 1
found
""",
        notes="search_ifc_knowledge answers this from the schema without touching the model.",
    ),
    Recipe(
        slug="elements-per-storey",
        title="Count elements per storey",
        summary="Walks the containment relationships instead of guessing from names.",
        tags=("spatial", "storey", "count"),
        verify="read",
        code="""
from collections import Counter
per_storey = Counter()
for e in by_class("IfcElement"):
    holder = container(e)
    per_storey[holder.Name if holder else "(not contained)"] += 1
dict(per_storey)
""",
    ),
    Recipe(
        slug="list-psets-of-element",
        title="Show every property set on one element",
        summary="Property sets and quantity sets for a single GlobalId.",
        tags=("element", "pset", "qto"),
        verify="read",
        code="""
e = by_class("IfcWall")[0]
{"element": e.GlobalId, "name": e.Name, "psets": psets(e), "qtos": qtos(e)}
""",
    ),
    Recipe(
        slug="material-usage",
        title="Summarize material usage",
        summary="Which materials are assigned, and how many elements use each.",
        tags=("material", "report"),
        verify="read",
        code="""
from collections import Counter
used = Counter()
for e in by_class("IfcElement"):
    material = element_util.get_material(e)
    if material is None:
        used["(none)"] += 1
    else:
        used[getattr(material, "Name", None) or material.is_a()] += 1
dict(used)
""",
    ),
    Recipe(
        slug="unclassified-elements",
        title="Find elements with no classification reference",
        summary="Deliverables usually require a classification on every element.",
        tags=("classification", "quality", "audit"),
        verify="read",
        code="""
missing = []
for e in by_class("IfcElement"):
    refs = [
        rel for rel in getattr(e, "HasAssociations", []) or []
        if rel.is_a("IfcRelAssociatesClassification")
    ]
    if not refs:
        missing.append(e.GlobalId)
{"unclassified": len(missing), "examples": missing[:10]}
""",
    ),
    Recipe(
        slug="spaces-and-areas",
        title="List spaces with their areas",
        summary="Space names, numbers, and stored area quantities.",
        tags=("space", "area", "qto"),
        verify="read",
        code="""
rows = []
for space in by_class("IfcSpace"):
    quantities = qtos(space).get("Qto_SpaceBaseQuantities") or {}
    rows.append({
        "global_id": space.GlobalId,
        "name": space.Name,
        "number": getattr(space, "LongName", None),
        "area": quantities.get("NetFloorArea") or quantities.get("GrossFloorArea"),
    })
rows
""",
    ),
    Recipe(
        slug="type-occurrence-map",
        title="Map element types to their occurrences",
        summary="How many occurrences each type object has, and which types are unused.",
        tags=("type", "inventory"),
        verify="read",
        code="""
rows = []
for t in by_class("IfcTypeProduct"):
    occurrences = []
    for rel in getattr(t, "Types", []) or []:
        occurrences.extend(rel.RelatedObjects)
    rows.append({"type": t.Name, "class": t.is_a(), "occurrences": len(occurrences)})
sorted(rows, key=lambda r: -r["occurrences"])
""",
    ),
    Recipe(
        slug="openings-per-wall",
        title="Count openings in each wall",
        summary="Follows HasOpenings, which is how doors and windows cut a wall.",
        tags=("opening", "wall", "geometry"),
        verify="read",
        code="""
rows = []
for wall in by_class("IfcWall"):
    openings = getattr(wall, "HasOpenings", []) or []
    filled = sum(len(o.RelatedOpeningElement.HasFillings or []) for o in openings)
    rows.append({"wall": wall.Name, "openings": len(openings), "filled": filled})
rows
""",
    ),
    Recipe(
        slug="duplicate-names",
        title="Find duplicate element names",
        summary="Repeated names within a class usually mean a copy-paste mistake.",
        tags=("quality", "naming", "audit"),
        verify="read",
        code="""
from collections import defaultdict
seen = defaultdict(list)
for e in by_class("IfcElement"):
    if e.Name:
        seen[(e.is_a(), e.Name)].append(e.GlobalId)
{f"{cls} {name}": ids for (cls, name), ids in seen.items() if len(ids) > 1}
""",
    ),
    Recipe(
        slug="header-and-authoring-tool",
        title="Read the file header and authoring tool",
        summary="Who exported this file, with which application, and when.",
        tags=("header", "provenance", "triage"),
        verify="read",
        code="""
header = ifc.header
{
    "schema": ifc.schema,
    "description": list(header.file_description.description or []),
    "application": header.file_name.originating_system,
    "timestamp": header.file_name.time_stamp,
    "author": list(header.file_name.author or []),
}
""",
    ),
    Recipe(
        slug="elements-missing-a-property",
        title="List elements missing a required property",
        summary="The core of a delivery check, written as one selector plus one test.",
        tags=("quality", "ids", "property"),
        verify="read",
        code="""
selector, pset_name, prop = "IfcWall", "Pset_WallCommon", "FireRating"
missing = [
    e.GlobalId for e in query(selector)
    if not (psets(e).get(pset_name) or {}).get(prop)
]
{"checked": len(query(selector)), "missing": len(missing), "global_ids": missing[:20]}
""",
    ),
    Recipe(
        slug="spatial-orphans",
        title="Find elements outside the spatial structure",
        summary="Elements with no container never show up in a storey based report.",
        tags=("spatial", "quality", "audit"),
        verify="read",
        code="""
orphans = [e.GlobalId for e in by_class("IfcElement") if container(e) is None]
{"orphans": len(orphans), "examples": orphans[:10]}
""",
    ),
    Recipe(
        slug="units-and-precision",
        title="Report the project units",
        summary="Length, area, and volume units, which every quantity depends on.",
        tags=("units", "triage"),
        verify="read",
        code="""
project = by_class("IfcProject")[0]
{
    "length": unit_util.get_project_unit(ifc, "LENGTHUNIT"),
    "area": unit_util.get_project_unit(ifc, "AREAUNIT"),
    "volume": unit_util.get_project_unit(ifc, "VOLUMEUNIT"),
    "scale_to_si": unit_util.calculate_unit_scale(ifc),
    "project": project.Name,
}
""",
    ),
    # ------------------------------------------------------------------ edit
    Recipe(
        slug="set-property-on-selection",
        title="Set a property on every element of a selection",
        summary="Adds the property set when it is missing, then writes the value.",
        tags=("edit", "property", "pset"),
        mode="edit",
        verify="edit",
        code="""
selector, pset_name = "IfcWall", "Pset_WallCommon"
values = {"FireRating": "F30"}
changed = 0
for e in query(selector):
    pset = element_util.get_pset(e, pset_name, should_inherit=False)
    if pset is None:
        pset = ifc_api.pset.add_pset(ifc, product=e, name=pset_name)
    else:
        pset = ifc.by_id(pset["id"])
    ifc_api.pset.edit_pset(ifc, pset=pset, properties=values)
    changed += 1
{"changed": changed}
""",
        notes="get_pset returns a dict with an 'id' key; add_pset returns the entity itself.",
    ),
    Recipe(
        slug="rename-elements",
        title="Rename elements with a numbered pattern",
        summary="Deterministic renaming, ordered so a rerun gives the same result.",
        tags=("edit", "naming"),
        mode="edit",
        verify="edit",
        code="""
selector, pattern = "IfcWall", "WA-{index:03d}"
elements = sorted(query(selector), key=lambda e: e.GlobalId)
for index, e in enumerate(elements, start=1):
    ifc_api.attribute.edit_attributes(
        ifc, product=e, attributes={"Name": pattern.format(index=index)}
    )
{"renamed": len(elements)}
""",
    ),
    Recipe(
        slug="assign-classification",
        title="Assign a classification reference",
        summary="Creates the classification system once, then references it from elements.",
        tags=("edit", "classification"),
        mode="edit",
        verify="edit",
        code="""
system_name, code, label = "Uniclass 2015", "EF_25_10", "Walls"
system = next(
    (c for c in by_class("IfcClassification") if c.Name == system_name), None
)
if system is None:
    system = ifc_api.classification.add_classification(ifc, classification=system_name)
targets = query("IfcWall")
ifc_api.classification.add_reference(
    ifc, products=targets, identification=code, name=label, classification=system
)
{"classified": len(targets), "system": system_name}
""",
    ),
    Recipe(
        slug="assign-material",
        title="Assign a material to a selection",
        summary="Reuses an existing material with the same name instead of duplicating it.",
        tags=("edit", "material"),
        mode="edit",
        verify="edit",
        code="""
name, selector = "Concrete C30/37", "IfcWall"
material = next((m for m in by_class("IfcMaterial") if m.Name == name), None)
if material is None:
    material = ifc_api.material.add_material(ifc, name=name, category="concrete")
targets = query(selector)
for e in targets:
    ifc_api.material.assign_material(ifc, products=[e], material=material)
{"assigned": len(targets), "material": name}
""",
    ),
    Recipe(
        slug="copy-property-between-psets",
        title="Copy a property into another property set",
        summary="Migrates a custom property into the standard pset without losing the original.",
        tags=("edit", "property", "migration"),
        mode="edit",
        verify="edit",
        code="""
source_pset, source_prop = "Pset_WallCommon", "FireRating"
target_pset, target_prop = "Pset_ProjectQA", "FireRatingChecked"
moved = 0
for e in query("IfcWall"):
    value = (psets(e).get(source_pset) or {}).get(source_prop)
    if value is None:
        continue
    pset = element_util.get_pset(e, target_pset, should_inherit=False)
    pset = ifc.by_id(pset["id"]) if pset else ifc_api.pset.add_pset(
        ifc, product=e, name=target_pset
    )
    ifc_api.pset.edit_pset(ifc, pset=pset, properties={target_prop: str(value)})
    moved += 1
{"copied": moved}
""",
    ),
    Recipe(
        slug="fill-missing-names",
        title="Give unnamed elements a fallback name",
        summary="Names stay stable across runs because they derive from the GlobalId.",
        tags=("edit", "naming", "quality"),
        mode="edit",
        verify="edit",
        code="""
fixed = 0
for e in by_class("IfcElement"):
    if not (e.Name or "").strip():
        label = f"{e.is_a()[3:]}-{e.GlobalId[-6:]}"
        ifc_api.attribute.edit_attributes(ifc, product=e, attributes={"Name": label})
        fixed += 1
{"named": fixed}
""",
    ),
    Recipe(
        slug="remove-empty-psets",
        title="Remove property sets that hold no values",
        summary="Empty psets survive many exports and confuse downstream checks.",
        tags=("edit", "cleanup", "pset"),
        mode="edit",
        verify="edit",
        code="""
removed = 0
for e in by_class("IfcElement"):
    for name, props in list(psets(e).items()):
        values = {k: v for k, v in props.items() if k != "id"}
        if values:
            continue
        pset = ifc.by_id(props["id"])
        ifc_api.pset.remove_pset(ifc, product=e, pset=pset)
        removed += 1
{"removed": removed}
""",
    ),
    Recipe(
        slug="delete-selection",
        title="Delete every element of a selection",
        summary="Uses remove_product so relationships are cleaned up too.",
        tags=("edit", "delete"),
        mode="edit",
        verify="edit",
        code="""
selector = "IfcDoor, Name=Door-1"
targets = list(query(selector))
for e in targets:
    ifc_api.root.remove_product(ifc, product=e)
{"deleted": len(targets)}
""",
        notes="Deleting is irreversible in the file; save creates a backup first.",
    ),
    Recipe(
        slug="reclassify-elements",
        title="Change the IFC class of elements",
        summary="Turns proxies into the class they should have been exported as.",
        tags=("edit", "class", "migration"),
        mode="edit",
        verify="edit",
        code="""
targets = list(query("IfcWall, Name=Wall-3"))
for e in targets:
    ifc_api.root.reassign_class(ifc, product=e, ifc_class="IfcWall", predefined_type="PARTITIONING")
{"reassigned": len(targets)}
""",
    ),
]


def records() -> Iterator[Record]:
    for recipe in RECIPES:
        code = recipe.code.strip()
        body = [recipe.summary, "", "```python", code, "```"]
        if recipe.notes:
            body.append("\nNote: " + recipe.notes)
        body.append(
            "\nMode: "
            + ("needs edit mode" if recipe.mode == "edit" else "runs in ask mode")
        )
        yield Record(
            kind="recipe",
            key=f"recipe:{recipe.slug}",
            name=recipe.slug,
            summary=recipe.summary,
            body="\n".join(body),
            meta={
                "title": recipe.title,
                "tags": list(recipe.tags),
                "mode": recipe.mode,
                "code": code,
                "verified": bool(recipe.verify),
                "aliases": [recipe.title, *recipe.tags],
            },
        )
