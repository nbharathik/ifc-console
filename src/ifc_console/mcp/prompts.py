"""Curated MCP prompts: expert BIM workflows as one-click slash commands.

Each prompt expands to a guided sequence over the existing tools. They are
deliberately concise: the value is the workflow order and the selector
know-how, not prose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ifc_console.ifc.query import SYNTAX_HELP
from ifc_console.mcp.compat import MCPServer

if TYPE_CHECKING:
    from ifc_console.app import AppCore

# Everything below is verified against the pinned ifcopenshell grammar by
# tests/integration/test_resources_prompts.py. The facets past the basic
# cheat sheet answer most spatial, classification and system questions in
# one line, and are the difference between a selector and hand-written code.
SELECTOR_EXTRAS = """
Quoting, the mistake that costs the most rounds:
  A bare value may not contain a space or a dot. Quote it, or use a regex.
  IfcWall, Name="Basic Wall:Interior"      quoted value
  IfcElement, query:z>"2.5"                quoted number

Set algebra:
  IfcWall + IfcDoor                        union of two groups
  IfcElement, !IfcWall                     everything except walls
  1fTRrSB3LEdQg16mMfXa_p                   one element, by bare GlobalId

Comparisons: = != > >= < <= *= (contains) !*= (does not contain).
Specials: NULL, TRUE, FALSE. Commas AND facets together, except bare
classes, which union.

Facets the basic sheet omits:
  IfcElement, classification=/EF_25/       classification code or name
  IfcElement, classification=NULL          everything unclassified
  IfcElement, location="Level 1"           any container above it
  IfcElement, parent="Wall-01"             its direct parent only
  IfcElement, group="Fire Compartment 3"   IfcGroup, IfcZone or IfcSystem
  IfcWall, Name*=Corridor                  substring match
  IfcWall, /Pset_.*Common/.FireRating=F30  regex on the property set name
  IfcWall, Qto_WallBaseQuantities.NetVolume>0   numeric quantity comparison

Key paths, for anything the facets do not name. A dotted key MUST be quoted
or the filter silently matches nothing:
  IfcElement, query:"storey.Name"="Level 2"
  IfcElement, query:"space.Name"="Office 101"
  IfcElement, query:"system.Name"="SUP-01"
  IfcElement, query:"type.Name"=NULL       occurrences with no type
  IfcElement, query:class=IfcWall
  IfcElement, query:predefined_type=LOADBEARING
  IfcElement, query:z>2                    origin height in metres
  IfcElement, query:easting>0              also northing, elevation
"""

SELECTOR_GUIDE = SYNTAX_HELP + "\n" + SELECTOR_EXTRAS


def register(mcp: MCPServer, core: AppCore) -> None:
    @mcp.prompt(description="Audit the loaded model end to end and report what is wrong.")
    def model_audit() -> str:
        return (
            "Audit the loaded IFC model end to end:\n"
            "1. orient: confirm what is loaded and how it is structured.\n"
            "2. validate_model: schema issues, grouped by class and severity.\n"
            "3. query_elements to probe common gaps: elements with no Name "
            "(`IfcElement, Name=NULL`), walls without FireRating.\n"
            "4. compute_quantities(selector='IfcElement', aggregate_by='class') "
            "for a quantity sanity check.\n"
            "5. Finish with a short verdict: what is solid, what needs fixing, "
            "the worst offenders with their GlobalIds. If the viewer is "
            "connected, apply_color_theme the offenders so the user sees them."
        )

    @mcp.prompt(description="Produce a quantity takeoff table for a selector-defined set.")
    def qto_report(selector: str = "IfcElement", aggregate_by: str = "storey") -> str:
        return (
            f"Produce a quantity takeoff for `{selector}` aggregated by "
            f"{aggregate_by}:\n"
            f"1. compute_quantities(selector='{selector}', "
            f"aggregate_by='{aggregate_by}').\n"
            "2. Render the result as a readable table with units and totals; "
            "note elements that carry no stored quantities.\n"
            "3. Offer export_csv to save the underlying rows if the user wants "
            "the data."
        )

    @mcp.prompt(description="Explain one element in plain language.")
    def explain_element(global_id: str) -> str:
        return (
            f"Explain element {global_id} for a non-expert:\n"
            f"1. get_element(global_ids=['{global_id}'], include=['attributes', "
            "'psets', 'qtos', 'materials', 'type', 'container']).\n"
            "2. Say what it is, where it sits (storey and container chain), its "
            "key properties in plain words, and anything odd (missing psets, "
            "no material, unnamed). If the viewer is connected, "
            "highlight_elements it."
        )

    @mcp.prompt(description="Find elements that carry no classification reference.")
    def find_unclassified() -> str:
        return (
            "Find unclassified elements:\n"
            "1. query_elements(query='IfcElement, classification=NULL', "
            "limit=100). The selector grammar answers this natively; do not "
            "write code for it.\n"
            "2. query_elements(query='IfcElement, classification=/./', limit=1) "
            "for the other side of the count, so the report says how much of "
            "the model is classified rather than only what is missing.\n"
            "3. Group the result by class, report counts, and list the first "
            "few GlobalIds per class. Color them by class with "
            "apply_color_theme when the viewer is connected."
        )

    @mcp.prompt(description="Check the model against a buildingSMART IDS file and report.")
    def validate_against_ids(ids_path: str) -> str:
        return (
            f"Check the model against the IDS file at {ids_path}:\n"
            f"1. validate_ids(ids_path='{ids_path}').\n"
            "2. Summarize per specification: pass/fail, how many applicable "
            "elements failed, and why (the reasons are in the report).\n"
            "3. List failing GlobalIds and, if the viewer is connected, "
            "apply_color_theme pass (green group) versus fail (orange group) "
            "so the user sees the result in 3D."
        )

    @mcp.prompt(description="The selector syntax cheat sheet for query_elements.")
    def selector_help() -> str:
        return (
            "Selector syntax. Every tool that takes a set of elements speaks "
            "it: query_elements (`query`), measure_elements, compute_quantities, "
            "get_element_geometry, analyze_element_geometry, export_csv "
            "(`selector`), detect_clashes and measure_distance (`set_a`, "
            "`set_b`). Those argument names are interchangeable, so `selector`, "
            "`query` and `term` are all accepted wherever one of them is.\n\n"
            + SELECTOR_GUIDE
            + "\nAnswer the user's next element-finding question by composing "
            "one of these selectors and running query_elements."
        )
