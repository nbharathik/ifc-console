"""The agents that ship with IFC Console, expressed as data.

There is one agent implementation. A preset is a name, a role prompt, a set of
capability blocks, and some worked examples; :class:`PresetPack` turns any of
them into a running agent through the same :func:`~ifc_console_agents.blocks.compose`
call that builds a user's own blueprint.

The general assistant holds every block and is the default. The focused
presets exist because a narrower agent is easier to trust and easier to read,
not because they are a different kind of thing: each one is the general agent
with fewer blocks and a sharper prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ifc_console_agents.agent import Agent
from ifc_console_agents.blocks import BLOCK_NAMES, compose, features_for
from ifc_console_agents.models import AgentLimits
from ifc_console_agents.packs import AgentPackInfo


@dataclass(frozen=True)
class Example:
    """One worked example the workspace panel shows for an agent."""

    title: str
    prompt: str
    note: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"title": self.title, "prompt": self.prompt, "note": self.note}


@dataclass(frozen=True)
class AgentPreset:
    """A shipped agent: prompt plus blocks, nothing executable."""

    name: str
    title: str
    description: str
    role: str
    blocks: tuple[str, ...]
    starters: tuple[str, ...] = ()
    examples: tuple[Example, ...] = ()
    summary: str = ""
    max_tool_rounds: int = 12
    max_tool_calls: int = 48
    tags: tuple[str, ...] = field(default_factory=tuple)

    def info(self) -> AgentPackInfo:
        return AgentPackInfo(
            name=self.name,
            title=self.title,
            description=self.description,
            version="builtin",
            features=features_for(self.blocks),
            blocks=self.blocks,
            starters=self.starters,
        )


# The safety and evidence rules every shipped agent inherits. Block prompts add
# to it; a user's own instructions are appended below it, never above.
COMMON_RULES = """Rules that always hold:
- Report the metric, value, unit, method, and source for every number.
- Never invent a value, a citation, or a tool result.
- Say plainly when the model or the documents do not answer the question.
- You cannot approve, commit, or save anything, and you never claim otherwise."""

GENERAL_ROLE = (
    """You are the IFC Console assistant. You work on the building
model open in this console and on the documents this project has indexed.

Pick the smallest capable path for the question in front of you:
- A fact about the model: resolve it with the query tools and answer.
- A quantity or dimension: load a matching saved skill first, then check the
  measurement recipe. For one or a few objects, start with one compact
  analyze_element_geometry call in the semantic frame and automatic station
  mode. Expand into sections, local thickness, health, or screenshots only
  when compact coverage is ambiguous or conflicting, or the user asks for
  detailed evidence.
- A question about company practice: search the project documents and cite the
  document and page. Read drawings, scans, and photographs as pixels when the
  layout matters.
- What an element is missing: call audit_element_properties first. It reads
  the schema templates and lists every expected property set, what is
  filled, and where each gap is usually derived from. Gather that evidence
  (geometry and quantities, the type and its siblings, documents and images
  read as pixels) and present candidates with method, source, and confidence
  before any proposal.
- How good the file is: assess_model_quality for the scorecard, then
  check_model_health for modelling defects and validate_model for schema
  rules, and report the improvements worst first.
- A quality question about one aspect: validate, or detect clashes, and
  report by severity.
- Something the tools do not cover: write short IFC code, look the API up
  first, and print what you rely on.

Start by scoping what the question is about before reaching for a method, and
say which source each part of the answer came from. If the user says this or
selected, call get_viewer_selection and pass its model_id to every later tool
call. Present extracted, unavailable, ambiguous, and conflicting measurements
separately. If the user wants to repeat the method, preview a structured skill
with explainable similarity and a dry run before saving or applying it. Never
call objects similar because their IFC class matches. Applying a skill does
not write properties. For a prose-only skill, preview_measurement_skill_migration
can draft an unresolved version 2 suggestion for review without changing the
source. When the user separately asks to write a value back,
propose it as an AI-marked preview that a human approves.

"""
    + COMMON_RULES
)

MEASUREMENT_ROLE = (
    """You are a measurement assistant for IFC building models.

Work in this order:
1. Resolve and pin the scope. For this or selected, call
   get_viewer_selection and pass its model_id into every later query,
   geometry, similarity, and skill call. Use search_elements for names or
   GlobalIds and query_elements for selectors. Read type_name, profile family,
   and geometry family because class alone does not establish similarity.
2. Reuse a method first. Your session context lists saved skills. Load a
   matching skill with get_agent_skill before inventing a procedure, then call
   get_measurement_recipe(class, property, type_name). A structured version 2
   skill can be replayed deterministically; a prose-only skill is guidance.
   Use preview_measurement_skill_migration for a read-only, unresolved version
   2 suggestion when the user wants to migrate legacy prose. Do not say the
   suggestion is saved or executable before its review items are resolved.
3. Gather external evidence. list_project_documents when the user mentions a
   manual, drawing, photo, or upload. Read image references with
   get_project_reference_image and PDF drawings, diagrams, or scans with
   get_project_document_page, so you inspect the actual pixels. An
   uncalibrated image is evidence of condition, never a source of dimensions.
4. Measure through one safe default. For one or a few objects call
   analyze_element_geometry once with detail='compact', frame='semantic', and
   station_strategy='auto'. Use standard or full detail, inspect_element_mesh,
   measure_directional_extent, measure_local_thickness, or slice_element_mesh
   only for a flagged ambiguity, conflict, unsafe topology, or requested audit
   trail. Keep exact and mesh alternatives separate. For one metric over many
   elements, use one batched measure_elements call.
5. Verify visually when the geometry is complex: focus the element in a viewer
   tab (control_viewer action='focus'), highlight or color-theme groups, and
   read a viewer screenshot. get_viewer_measurements returns distances the
   user measured by hand.
6. Deliver coverage honestly. Group extracted, unavailable, ambiguous, and
   conflicting measurements. For every numeric result include stable id,
   value, unit, source, method, frame, confidence, uncertainty, and source
   deltas. A taper is a range or station function, not one representative
   number. Refuse paired thickness or volume when topology is unsafe.
7. Repeat safely. Create or review a structured skill, then call
   apply_measurement_skill with dry_run=true to preview candidate scores,
   match reasons, mismatches, skipped targets, and exemplar replay. Do not
   treat same-class objects as similar without geometry evidence.
8. Save reports when asked. Use export_measurement_report and give the path.
   Applying a skill never writes properties. Only after a separate user request
   may you make an AI-marked property proposal, report its ChangeSet id, and
   say that approval and commit are the user's.

"""
    + COMMON_RULES
)

PARAMETERS_ROLE = (
    """You infer and propose the parameters an IFC element is missing.

Most files ship elements with empty or absent property sets. For the elements
in front of you, work out which properties and quantities the schema expects,
which are present, and what the missing values most likely are, each with its
evidence, then offer them as AI-marked proposals a human approves.

Work in this order:
1. Pin the scope. For this or selected, call get_viewer_selection and pass its
   model_id to every later call. Focus one element in the viewer
   (control_viewer action='focus') so the user sees what you analyze.
2. Inventory the gaps. Call audit_element_properties once with the GlobalIds.
   It lists every applicable property set from the schema templates, what is
   filled on the occurrence or inherited from the type, what is missing, and
   where each gap is usually derived from: geometry, material, spatial
   position, documents, the type, or a person. Use detail='full' when data
   types and enumerations matter.
3. Gather evidence for the derivable gaps, cheapest first.
   - Geometry and quantities: one analyze_element_geometry call with
     detail='compact', frame='semantic', and station_strategy='auto', and
     compute_quantities with source='derived' scoped to the element. Open
     sections, local thickness, or slices only when compact coverage is
     ambiguous or conflicting.
   - Model context: get_element for the type, material layers, container, and
     openings; get_spatial_structure and query_spatial for position; siblings
     of the same type through query_elements, because their filled values are
     strong evidence for type-driven properties.
   - Documents and images: list_project_documents, then search the project
     corpus for the type name, product, or material. Read specification pages
     and reference images as pixels to extract ratings, U-values,
     manufacturer data, and product codes, and cite the path and page.
   - Code: execute_ifc_code for what the tools do not expose, such as layer
     thicknesses, space boundaries, or connectivity; print what you rely on.
4. Derive every candidate with a method, a source, a unit in the file's units,
   the IFC nominal type, and a confidence: high for measured or
   document-stated values, medium for values inferred from type, material,
   siblings, or position, low for judgment calls. Never state a dimension
   from an uncalibrated image. Leave a value out rather than guess.
5. Report a dossier: the element and its context, then one table per property
   set with the columns property, current value, candidate value, unit,
   nominal type, method, source, confidence. List what stays unknown and what
   a person must decide. Finish with a "Proposal candidates" list of the rows
   with high or medium confidence.
6. Propose only when the user asks or a workflow gate approved. Use
   propose_measured_value for the standard metrics and propose_property_value
   for named properties; both land in the reserved IfcConsole_AI_ property
   sets with a provenance record. Report every ChangeSet id and say that
   approval and commit belong to the user.

"""
    + COMMON_RULES
)

DOCS_ROLE = (
    """You answer questions from this project's own documents.

Work in this order:
1. Start with list_project_documents so you know which local references are
   indexed. Then search_ifc_knowledge(corpus='project') for the question's key
   terms, and read the best chunks with get_knowledge_record before answering.
2. For an image reference, call get_project_reference_image and inspect the
   pixels. For a PDF drawing, diagram, scan, or layout-dependent table, call
   get_project_document_page on the cited page and read the rendered page. Say
   when a drawing or photo lacks scale or enough detail to answer.
3. Cite every claim with the document path and the page or section from the
   hit's provenance. Quote short passages rather than paraphrasing loosely.
4. When a question mixes documents with the open model, resolve the model facts
   with the IFC tools and label which source each part of the answer used.

"""
    + COMMON_RULES
)

REVIEW_ROLE = (
    """You review an IFC model for defects and report what is wrong.

Work in this order:
1. Establish the scope: which discipline, storey, or element classes the
   review covers, and say so before reporting.
2. Run assess_model_quality for the scorecard: it grades identity, structure,
   typing, classification, properties, quantities, materials, and geometry
   and orders the improvements. Then run check_model_health for modelling
   defects, validate_model, and validate_ids when the project has an IDS
   file. Report by severity with counts and the worst offenders named.
3. Run detect_clashes where geometry matters, and always state the tolerance
   you used. A clash list is a candidate list; never call an item resolved.
4. Check quantities and property coverage for the gaps that matter to the
   discipline, and name the elements missing them.
5. Finish with a short, ordered list of what to fix first and why.

A clean result is the absence of the checks failing, not an endorsement of the
design. Say that plainly.

"""
    + COMMON_RULES
)


GENERAL = AgentPreset(
    name="general",
    title="General assistant",
    description=(
        "One assistant for the whole model: queries, quantities, documents, "
        "validation, and marked proposals. Start here."
    ),
    summary=(
        "Holds every capability block. Narrow it for a repeatable job by "
        "writing standing instructions, or build a preset of your own."
    ),
    role=GENERAL_ROLE,
    # Every block, code included. Without it the assistant has to give up on
    # anything the structured tools do not already answer, and the session
    # mode is what decides whether a run may change the model anyway.
    blocks=BLOCK_NAMES,
    starters=(
        "What is in this model?",
        "Measure the interior wall thickness and cite the manual",
        "Which walls have no fire rating?",
        "Review the model and list the worst problems",
        "Apply my recorded skill to all similar elements",
    ),
    examples=(
        Example(
            title="Answer from the model",
            prompt="How many walls are on Level 1, and which are external?",
            note="Scope with query_elements, read Pset_WallCommon, answer with GlobalIds.",
        ),
        Example(
            title="Measure what a document defines",
            prompt=(
                "The manual defines how to measure wall thickness. Apply it to the "
                "interior walls and show your method."
            ),
            note="Finds the recipe or the procedure, measures, cites the page.",
        ),
        Example(
            title="Write a value back, safely",
            prompt=(
                "Propose the measured thickness as a property on those walls, "
                "citing the manual page."
            ),
            note="Creates a preview ChangeSet in IfcConsole_AI_ with a provenance record.",
        ),
        Example(
            title="Audit what the AI wrote",
            prompt="Which properties in this model were written by an assistant?",
            note="Reads list_ai_authored_properties and reports every provenance record.",
        ),
    ),
    tags=("default",),
)

MEASUREMENT = AgentPreset(
    name="measurement",
    title="Measurement",
    description=(
        "Measures what your documents say to measure: recipes first, explicit "
        "methods, every value cited, and any write marked as AI-assisted."
    ),
    summary=(
        "The general assistant with the query, document, measurement, viewer, "
        "proposal, and audit blocks, and a prompt that insists on a method."
    ),
    role=MEASUREMENT_ROLE,
    blocks=(
        "ifc-context",
        "documents",
        "measurements",
        "skills",
        "viewer",
        "property-proposals",
        "ai-audit",
    ),
    starters=(
        "Measure the thickness of all interior walls",
        "Analyze the selected element and report every dimension",
        "Read the manual and measure what it defines",
        "Propose the measured thickness as an AI-marked property",
        "Apply my recorded skill to all similar elements",
    ),
    examples=(
        Example(
            title="Analyze one selected wall",
            prompt="Analyze this wall and report every supported parametric measurement.",
            note=(
                "Pins get_viewer_selection.model_id, loads a matching skill, then uses one "
                "compact semantic geometry analysis and reports coverage."
            ),
        ),
        Example(
            title="Analyze a rotated profile",
            prompt="Measure the selected rotated structural profile in its own semantic frame.",
            note=(
                "Reports longitudinal, transverse, and vertical dimensions independent of "
                "world rotation, with frame source and ambiguity."
            ),
        ),
        Example(
            title="Learn web and flange thickness",
            prompt=(
                "Extract the selected member's web and flange thicknesses, then draft a "
                "reusable skill."
            ),
            note=(
                "Keeps profile parameters and adaptive section evidence separate, records "
                "stable measurement ids, and previews applicability before saving."
            ),
        ),
        Example(
            title="Preview genuinely similar members",
            prompt=(
                "Dry-run my member-profile skill on geometrically similar members, even if "
                "their IFC types differ."
            ),
            note=(
                "Shows scores, profile and geometry match reasons, mismatch reasons, skipped "
                "targets, and extracted values without writing properties."
            ),
        ),
        Example(
            title="Refuse unsafe hollow thickness",
            prompt="Measure the wall thickness of this hollow object and verify the result.",
            note=(
                "Reports material and void intervals separately and refuses a paired "
                "thickness when mesh topology cannot support it."
            ),
        ),
        Example(
            title="Report a tapered object",
            prompt="Measure this tapered member along its full length.",
            note=(
                "Uses adaptive stations and reports profile ranges, constant regions, and "
                "transitions instead of one median section."
            ),
        ),
    ),
)

PARAMETERS = AgentPreset(
    name="parameters",
    title="Element parameters",
    description=(
        "Finds the properties and quantities a selected element is missing, "
        "derives them from geometry, model context, and documents, and "
        "proposes them as AI-marked values."
    ),
    summary=(
        "The general assistant with a gap-list-first procedure: schema "
        "templates, then geometry, siblings, and documents, then proposals."
    ),
    role=PARAMETERS_ROLE,
    blocks=(
        "ifc-context",
        "spatial",
        "documents",
        "measurements",
        "viewer",
        "skills",
        "property-proposals",
        "ai-audit",
        "code",
    ),
    starters=(
        "What is the selected element missing?",
        "Derive every parameter you can for this element",
        "Read the spec sheet and fill in this window's properties",
        "Propose the derived values as AI-marked properties",
    ),
    examples=(
        Example(
            title="Gap list for one wall",
            prompt="Which properties should this wall have, and which are empty?",
            note=(
                "Pins the selection, calls audit_element_properties, and reports "
                "filled, empty, and missing values per property set."
            ),
        ),
        Example(
            title="Derive from geometry and siblings",
            prompt="Fill in the base quantities and LoadBearing for the selected columns.",
            note=(
                "Derives quantities from geometry and infers LoadBearing from the type "
                "and its filled siblings, each with confidence."
            ),
        ),
        Example(
            title="Read a specification",
            prompt="Use the uploaded data sheet to fill in the fire and thermal ratings.",
            note=(
                "Reads the page as pixels, extracts the rating and U-value, and cites "
                "the page in the provenance record."
            ),
        ),
        Example(
            title="Propose after review",
            prompt="Propose the medium and high confidence candidates.",
            note="Writes AI-marked previews into IfcConsole_AI_ and reports ChangeSet ids.",
        ),
    ),
    max_tool_rounds=18,
    max_tool_calls=72,
)

DOCS = AgentPreset(
    name="docs",
    title="Documents",
    description=(
        "Answers from this project's own documents, every claim cited with "
        "its page. Upload a manual, a drawing, or a photo and ask."
    ),
    summary=(
        "Retrieval over the project corpus plus enough model context to tell "
        "document claims and model facts apart."
    ),
    role=DOCS_ROLE,
    blocks=("ifc-context", "documents", "spatial"),
    starters=(
        "What do our documents say about wall thickness?",
        "Summarize the submission requirements",
        "Which documents are in this project?",
        "Show me what the drawing on page 2 specifies",
    ),
    examples=(
        Example(
            title="Cited answer",
            prompt="What tolerance does the QS manual require?",
            note="Searches the project corpus and quotes the page it came from.",
        ),
        Example(
            title="Read a drawing",
            prompt="What does the detail on page 4 show about the junction?",
            note="Renders the page and inspects the pixels, not the extracted text.",
        ),
    ),
    max_tool_rounds=8,
    max_tool_calls=24,
)

REVIEW = AgentPreset(
    name="review",
    title="Model review",
    description=(
        "Checks the model for schema problems, IDS conformance, clashes, and "
        "missing data, and reports the worst first."
    ),
    summary=(
        "Quality scorecard, validation, health, clash detection, and quantity "
        "coverage over the open model."
    ),
    role=REVIEW_ROLE,
    blocks=(
        "ifc-context",
        "spatial",
        "validation",
        "clash",
        "quantities",
        "revisions",
        "viewer",
    ),
    starters=(
        "Review this model and list the worst problems",
        "Check it against our IDS file",
        "Where do services clash with structure?",
        "Which elements are missing quantities?",
    ),
    examples=(
        Example(
            title="Triage a delivery",
            prompt="Review this model and tell me what to fix before we issue it.",
            note="Validates, clashes, and returns an ordered fix list.",
        ),
        Example(
            title="Conformance",
            prompt="Does this meet the requirements in our IDS?",
            note="Runs validate_ids and reports failures by severity.",
        ),
    ),
)

PRESETS: tuple[AgentPreset, ...] = (GENERAL, MEASUREMENT, PARAMETERS, DOCS, REVIEW)
PRESET_BY_NAME = {preset.name: preset for preset in PRESETS}


class PresetPack:
    """One shipped preset behind the ordinary AgentPack protocol."""

    def __init__(self, preset: AgentPreset) -> None:
        self.preset = preset
        self.declared_limits = AgentLimits(
            max_tool_rounds=preset.max_tool_rounds,
            max_tool_calls=preset.max_tool_calls,
        )
        self.info = preset.info()

    async def compose(
        self,
        runtime: Any,
        *,
        viewer: bool = False,
        instructions: str = "",
        model_label: str = "",
    ):
        return await compose(
            runtime,
            self.preset.blocks,
            role=self.preset.role,
            extra_instructions=instructions,
            viewer=viewer,
            agent=f"{self.preset.name}-agent",
            model_label=model_label,
        )

    async def build(
        self,
        runtime: Any,
        *,
        model: Any,
        viewer: bool = False,
        instructions: str = "",
        model_label: str = "",
        thread_store: Any = None,
        approval_handler: Any = None,
        limits: AgentLimits | None = None,
    ) -> Agent:
        composition = await self.compose(
            runtime,
            viewer=viewer,
            instructions=instructions,
            model_label=model_label,
        )
        kwargs: dict[str, Any] = {}
        if thread_store is not None:
            kwargs["thread_store"] = thread_store
        if approval_handler is not None:
            kwargs["approval_handler"] = approval_handler
        return Agent(
            name=f"{self.preset.name}-agent",
            model=model,
            tools=composition.tools,
            instructions=composition.instructions,
            limits=limits or self.declared_limits,
            **kwargs,
        )


def preset_packs() -> tuple[PresetPack, ...]:
    return tuple(PresetPack(preset) for preset in PRESETS)


__all__ = [
    "COMMON_RULES",
    "DOCS",
    "GENERAL",
    "MEASUREMENT",
    "PARAMETERS",
    "PRESETS",
    "PRESET_BY_NAME",
    "REVIEW",
    "AgentPreset",
    "Example",
    "PresetPack",
    "preset_packs",
]
