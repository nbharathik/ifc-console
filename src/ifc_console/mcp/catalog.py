"""Tool search over the live catalog, and the profiles that decide what is listed.

A client that lists every tool pays for every description on every session. The
lean profile lists a small core and lets the model search the rest: find_tools
ranks the live operations against plain words, and call_tool runs one by name.
Profiles only change what `tools/list` shows; every operation stays callable.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from ifc_console.core.operations import OperationDefinition

FULL = "full"
LEAN = "lean"
PROFILES = (FULL, LEAN)

# What a model needs before it knows what else exists. The two discovery tools
# are the way to everything else; execute_ifc_code and save_ifc_file stay listed
# by name so a client's approval prompts can see them.
LEAN_CORE = (
    "orient",
    "query_elements",
    "search_elements",
    "get_element",
    "compute_quantities",
    "get_schema_docs",
    "search_ifc_knowledge",
    "execute_ifc_code",
    "set_properties",
    "save_ifc_file",
    "get_viewer_selection",
    "highlight_elements",
    "find_tools",
    "call_tool",
)
DISCOVERY_TOOLS = frozenset({"find_tools", "call_tool"})


def select(tools: list[Any], profile: str) -> list[Any]:
    """The tools `tools/list` shows for a profile. Full hides the discovery pair,
    which only earns its place when the rest is not listed."""
    if profile == LEAN:
        order = {name: index for index, name in enumerate(LEAN_CORE)}
        return sorted((tool for tool in tools if tool.name in order), key=lambda t: order[t.name])
    return [tool for tool in tools if tool.name not in DISCOVERY_TOOLS]


def compact_input_schema(schema: Any) -> Any:
    """The argument schema as a model needs to read it.

    An optional argument says so by not being required, so a `null` alternative
    and a `null` default only repeat it. Validation still uses the full schema;
    this shortens what is listed.
    """
    if isinstance(schema, list):
        return [compact_input_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {key: compact_input_schema(value) for key, value in schema.items()}
    if out.get("default", 0) is None:
        del out["default"]
    options = out.get("anyOf")
    if isinstance(options, list):
        kept = [option for option in options if option != {"type": "null"}]
        if len(kept) == 1 and len(kept) != len(options):
            del out["anyOf"]
            out = {**kept[0], **out}
    return out


def _wire_annotations(annotations: Any) -> Any:
    """`destructiveHint` means nothing on a read-only tool, so it is not sent."""
    if annotations is not None and annotations.readOnlyHint is True:
        return annotations.model_copy(update={"destructiveHint": None})
    return annotations


def for_wire(tools: list[Any], profile: str) -> list[Any]:
    """What `tools/list` sends: the profile's tools with shortened schemas."""
    return [
        tool.model_copy(
            update={
                "inputSchema": compact_input_schema(tool.inputSchema),
                "annotations": _wire_annotations(tool.annotations),
            }
        )
        for tool in select(tools, profile)
    ]

# Words people use that the descriptions do not, keyed by operation.
_SYNONYMS: dict[str, str] = {
    "set_properties": "set assign write edit update change fill enrich property value rating",
    "measure_elements": "dimension size length width height thickness area volume",
    "measure_distance": "gap clearance between spacing",
    "measure_local_thickness": "wall thickness cross section through point",
    "measure_directional_extent": "extent span overall size along direction",
    "analyze_element_geometry": "shape profile parametric inspect dimensions",
    "compute_quantities": "takeoff quantity quantities count bill schedule totals",
    "detect_clashes": "collision interference overlap conflict intersect",
    "check_model_health": "quality problems defects orphans duplicates audit",
    "assess_model_quality": "score scorecard rating quality report",
    "audit_element_properties": "missing properties completeness expected",
    "validate_model": "schema errors valid express",
    "validate_ids": "ids specification requirements checker buildingsmart",
    "compare_models": "diff revisions changes versions compare",
    "export_csv": "spreadsheet excel table export rows",
    "export_measurement_report": "report markdown document write-up",
    "get_viewer_screenshot": "image picture screenshot render view capture",
    "control_viewer": "camera section view isolate focus measure orbit",
    "apply_color_theme": "color colour paint legend group theme",
    "get_georeferencing": "coordinates crs map location north geolocation",
    "query_spatial": "inside near within above below distance containment",
    "list_models": "resident open models files loaded",
    "open_ifc_file": "load open switch replace file model",
    "attach": "add second model companion file reference",
    "find_files": "search folder paths locate files",
    "get_psets": "properties property sets quantities pset",
    "get_spatial_structure": "storeys floors levels building tree hierarchy",
    "get_api_docs": "ifcopenshell api function signature usage",
    "list_project_documents": "pdf documents manuals catalogue drawings",
    "preview_property_change": "preview propose change set edit properties safely",
    "submit_validation_job": "background job long running validation",
}

_STOP = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "i",
        "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "with", "what",
    }
)  # fmt: skip
_WORD = re.compile(r"[a-z0-9]+")
_BRACKET_TAG = re.compile(r"^\s*\[[^\]]*\]\s*")
_K1 = 1.4
_B = 0.72


def tokens(text: str) -> list[str]:
    """Lowercase words, split on case and underscores, with plurals folded."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).replace("_", " ")
    words = []
    for word in _WORD.findall(spaced.lower()):
        if word in _STOP:
            continue
        if len(word) > 3 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.append(word)
    return words


def summary(description: str, limit: int = 170) -> str:
    """The first sentence of a description, without its [TAG] prefix."""
    text = _BRACKET_TAG.sub("", description).strip()
    match = re.search(r"(?<=[.!?])\s", text)
    first = text[: match.start()] if match else text
    if len(first) > limit:
        first = first[: limit - 1].rstrip() + "…"
    return first


def _type_of(schema: dict[str, Any]) -> str:
    if "type" in schema:
        kind = schema["type"]
        return "|".join(kind) if isinstance(kind, list) else str(kind)
    options = schema.get("anyOf") or schema.get("oneOf") or []
    names = [_type_of(option) for option in options if option.get("type") != "null"]
    return "|".join(dict.fromkeys(names)) or "any"


def compact_schema(schema: dict[str, Any], *, note_limit: int = 110) -> dict[str, Any]:
    """Arguments as name -> "type: note", plus which are required."""
    properties = schema.get("properties") or {}
    args: dict[str, str] = {}
    for name, spec in properties.items():
        note = str(spec.get("description") or "").strip()
        if len(note) > note_limit:
            note = note[: note_limit - 1].rstrip() + "…"
        kind = _type_of(spec)
        if "enum" in spec:
            kind = "|".join(str(item) for item in spec["enum"])
        args[name] = f"{kind}: {note}" if note else kind
    out: dict[str, Any] = {"args": args}
    if schema.get("required"):
        out["required"] = list(schema["required"])
    return out


@dataclass(frozen=True)
class Card:
    name: str
    summary: str
    read_only: bool
    destructive: bool
    schema: dict[str, Any]


class ToolIndex:
    """BM25 over each operation's name, description, arguments and synonyms."""

    def __init__(self, definitions: list[OperationDefinition]) -> None:
        self.cards: dict[str, Card] = {}
        self._terms: dict[str, Counter[str]] = {}
        self._length: dict[str, int] = {}
        for definition in definitions:
            name = definition.name
            self.cards[name] = Card(
                name=name,
                summary=summary(definition.description),
                read_only=definition.annotations.readOnlyHint is True,
                destructive=definition.annotations.destructiveHint is True,
                schema=definition.input_schema,
            )
            properties = definition.input_schema.get("properties") or {}
            fields = [
                (tokens(name), 4),
                (tokens(_SYNONYMS.get(name, "")), 3),
                (tokens(definition.description), 1),
                (tokens(" ".join(properties)), 2),
                (
                    tokens(" ".join(str(spec.get("description") or "") for spec in properties.values())),
                    1,
                ),
            ]
            counts: Counter[str] = Counter()
            for words, weight in fields:
                for word in words:
                    counts[word] += weight
            self._terms[name] = counts
            self._length[name] = sum(counts.values())
        total = max(len(self._terms), 1)
        self._average = sum(self._length.values()) / total
        document_frequency: Counter[str] = Counter()
        for counts in self._terms.values():
            document_frequency.update(counts.keys())
        self._idf = {
            word: math.log(1 + (total - freq + 0.5) / (freq + 0.5))
            for word, freq in document_frequency.items()
        }

    def search(self, query: str, *, limit: int = 6, exclude: frozenset[str] = frozenset()) -> list[Card]:
        wanted = tokens(query)
        if not wanted:
            return []
        scored: list[tuple[float, str]] = []
        for name, counts in self._terms.items():
            if name in exclude:
                continue
            score = 0.0
            norm = 1 - _B + _B * self._length[name] / (self._average or 1)
            for word in wanted:
                tf = counts.get(word, 0)
                if not tf:
                    # a prefix hit ("measur" for "measure") counts for less
                    tf = 0.5 * max(
                        (count for term, count in counts.items() if term.startswith(word) and len(word) > 3),
                        default=0,
                    )
                if tf:
                    score += self._idf.get(word, 0.5) * tf * (_K1 + 1) / (tf + _K1 * norm)
            if score > 0:
                scored.append((score, name))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [self.cards[name] for _, name in scored[:limit]]
