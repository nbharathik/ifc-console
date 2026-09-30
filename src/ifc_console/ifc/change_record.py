"""What one edit touched, read back from IfcOpenShell's own transaction log."""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

MAX_GUIDS = 2000
MAX_NAMES = 300
_NODE_BUDGET = 20_000
_TOP_CLASSES = 12

# Entities whose creation, deletion or edit cannot change what is drawn. The
# list is deliberately short: anything not named here counts as geometry, so an
# unknown class costs a redraw, never a stale screen.
_QUIET_EXACT = frozenset(
    {
        "IfcPropertySet",
        "IfcComplexProperty",
        "IfcElementQuantity",
        "IfcRelDefinesByProperties",
        "IfcRelAssociatesClassification",
        "IfcRelAssociatesMaterial",
        "IfcRelAssociatesDocument",
        "IfcRelAssociatesLibrary",
        "IfcRelAssociatesConstraint",
        "IfcRelAssignsToGroup",
        "IfcRelAggregates",
        "IfcRelNests",
        "IfcRelContainedInSpatialStructure",
        "IfcClassification",
        "IfcClassificationReference",
        "IfcDocumentReference",
        "IfcDocumentInformation",
        "IfcLibraryReference",
        "IfcLibraryInformation",
        "IfcMaterial",
        "IfcMaterialLayer",
        "IfcMaterialLayerSet",
        "IfcMaterialLayerSetUsage",
        "IfcMaterialConstituent",
        "IfcMaterialConstituentSet",
        "IfcMaterialList",
        "IfcOrganization",
        "IfcPerson",
        "IfcPersonAndOrganization",
        "IfcApplication",
        "IfcOwnerHistory",
        "IfcActorRole",
        "IfcPostalAddress",
        "IfcTelecomAddress",
    }
)
_QUIET_PREFIXES = ("IfcProperty", "IfcQuantity")

# Relationships that decide where an object sits in the tree.
_TREE_RELATIONS = frozenset(
    {"IfcRelAggregates", "IfcRelNests", "IfcRelContainedInSpatialStructure"}
)
_GEOMETRY_ATTRIBUTES = frozenset({"ObjectPlacement", "Representation"})
_LABEL_ATTRIBUTES = frozenset({"Name", "LongName", "Description", "ObjectType", "Tag"})


@dataclass
class ChangeSummary:
    ops: int = 0
    created: int = 0
    edited: int = 0
    removed: int = 0
    classes: dict[str, int] = field(default_factory=dict)
    guids: list[str] = field(default_factory=list)
    created_guids: list[str] = field(default_factory=list)
    removed_guids: list[str] = field(default_factory=list)
    geometry: bool = False
    tree: bool = False
    labels: bool = False
    truncated: bool = False
    # Names of the objects that were renamed, as they are now and as they were,
    # so a live view can relabel them without a rebuild.
    names: dict[str, str | None] = field(default_factory=dict)
    names_before: dict[str, str | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ops": self.ops,
            "created": self.created,
            "edited": self.edited,
            "removed": self.removed,
            "classes": dict(self.classes),
            "guids": list(self.guids),
            "created_guids": list(self.created_guids),
            "removed_guids": list(self.removed_guids),
            "geometry": self.geometry,
            "tree": self.tree,
            "labels": self.labels,
            "truncated": self.truncated,
        }


def _quiet(cls: str) -> bool:
    return cls in _QUIET_EXACT or cls.startswith(_QUIET_PREFIXES)


@lru_cache(maxsize=2048)
def _ancestry(schema: str, cls: str) -> frozenset[str]:
    """The class and every supertype above it, by name."""
    try:
        import ifcopenshell.ifcopenshell_wrapper as wrapper

        declaration = wrapper.schema_by_name(schema).declaration_by_name(cls)
    except Exception:
        return frozenset({cls})
    names: set[str] = set()
    while declaration is not None:
        names.add(declaration.name())
        declaration = declaration.supertype()
    return frozenset(names) or frozenset({cls})


def _entity_refs(value: Any) -> Iterator[Any]:
    if isinstance(value, (tuple, list)):
        for item in value:
            yield from _entity_refs(item)
    elif hasattr(value, "is_a") and hasattr(value, "id") and value.id():
        yield value


class _Owners:
    """Finds the objects a changed entity belongs to, within a fixed node budget."""

    def __init__(self, ifc: Any, limit: int) -> None:
        self.ifc = ifc
        self.limit = limit
        self.budget = _NODE_BUDGET
        self.found: dict[str, None] = {}
        self.truncated = False
        self._visited: set[int] = set()

    def add_guid(self, guid: str | None) -> None:
        if not guid or guid in self.found:
            return
        if len(self.found) >= self.limit:
            self.truncated = True
            return
        self.found[guid] = None

    def add_object(self, entity: Any) -> None:
        self.add_guid(getattr(entity, "GlobalId", None))

    def of(self, start: Any) -> None:
        queue: deque[Any] = deque([start])
        while queue:
            if self.budget <= 0:
                self.truncated = True
                return
            node = queue.popleft()
            node_id = node.id()
            if node_id in self._visited:
                continue
            self._visited.add(node_id)
            self.budget -= 1
            if node.is_a("IfcObjectDefinition"):
                self.add_object(node)
                continue
            if node.is_a("IfcRelationship"):
                for value in node.get_info(recursive=False, include_identifier=False).values():
                    for ref in _entity_refs(value):
                        if ref.is_a("IfcObjectDefinition"):
                            self.add_object(ref)
                continue
            queue.extend(self.ifc.get_inverse(node))


def verify_rollback(ifc: Any, transaction: Any, *, limit: int = 50_000) -> bool | None:
    """Whether a rolled-back transaction left the file as it found it.

    IfcOpenShell swallows some errors while rolling back, so the outcome is
    checked against the log itself. None means the log was too long to check.
    """
    operations = transaction.operations
    if len(operations) > limit:
        return None
    first: dict[int, str] = {}
    edits: set[tuple[int, int]] = set()
    try:
        for op in operations:
            action = op["action"]
            if action == "batch_delete":
                continue
            entity_id = op["value"]["id"] if action in ("create", "delete") else op["id"]
            first.setdefault(entity_id, action)
            if first[entity_id] == "create":
                continue  # must not exist; checked below
            if action == "delete" and first[entity_id] == "delete":
                entity = ifc.by_id(entity_id)
                if entity.is_a() != op["value"]["type"]:
                    return False
            elif action == "edit" and (entity_id, op["index"]) not in edits:
                edits.add((entity_id, op["index"]))
                element = ifc.by_id(entity_id)
                if transaction.serialise_value(element, element[op["index"]]) != op["old"]:
                    return False
        for entity_id, action in first.items():
            if action == "create" and _exists(ifc, entity_id):
                return False
    except Exception:
        return False
    return True


def _exists(ifc: Any, entity_id: int) -> bool:
    try:
        ifc.by_id(entity_id)
    except Exception:
        return False
    return True


def summarize(ifc: Any, transaction: Any, *, max_guids: int = MAX_GUIDS) -> ChangeSummary:
    """Read a finished transaction: counts, touched objects, and what to redraw."""
    summary = ChangeSummary(ops=len(transaction.operations))
    schema = getattr(ifc, "schema_identifier", None) or getattr(ifc, "schema", "IFC4")
    classes: Counter[str] = Counter()
    owners = _Owners(ifc, max_guids)
    created: dict[str, None] = {}
    removed: dict[str, None] = {}

    for op in transaction.operations:
        action = op["action"]
        if action == "batch_delete":
            continue
        if action in ("create", "delete"):
            value = op["value"]
            cls = str(value.get("type") or "")
            is_object = "IfcObjectDefinition" in _ancestry(schema, cls)
            classes[cls] += 1
            if action == "create":
                summary.created += 1
            else:
                summary.removed += 1
            if not _quiet(cls):
                summary.geometry = True
            if is_object or cls in _TREE_RELATIONS:
                summary.tree = True
            guid = value.get("GlobalId")
            if is_object and isinstance(guid, str):
                (created if action == "create" else removed)[guid] = None
                owners.add_guid(guid)
            continue

        summary.edited += 1
        try:
            element = ifc.by_id(op["id"])
        except Exception:
            continue  # created and removed within the same run
        cls = element.is_a()
        classes[cls] += 1
        if "IfcObjectDefinition" in _ancestry(schema, cls):
            try:
                attribute = element.attribute_name(op["index"])
            except Exception:
                attribute = ""
            if attribute in _GEOMETRY_ATTRIBUTES:
                summary.geometry = True
            elif attribute in _LABEL_ATTRIBUTES:
                summary.labels = True
                if attribute == "Name":
                    guid = getattr(element, "GlobalId", None)
                    if isinstance(guid, str):
                        old = op.get("old")
                        summary.names_before.setdefault(guid, old if isinstance(old, str) else None)
                        summary.names[guid] = element.Name
            owners.add_object(element)
            continue
        if not _quiet(cls):
            summary.geometry = True
        if cls in _TREE_RELATIONS:
            summary.tree = True
        owners.of(element)

    summary.classes = dict(classes.most_common(_TOP_CLASSES))
    summary.guids = list(owners.found)
    summary.created_guids = list(created)[:max_guids]
    summary.removed_guids = list(removed)[:max_guids]
    summary.truncated = (
        owners.truncated
        or len(created) > max_guids
        or len(removed) > max_guids
    )
    if len(summary.names) > MAX_NAMES:
        # too many to ship with the event; the view will rebuild its labels
        summary.names, summary.names_before = {}, {}
        summary.truncated = True
    return summary
