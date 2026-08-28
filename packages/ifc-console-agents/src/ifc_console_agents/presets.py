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

GENERAL_ROLE = """You are the IFC Console assistant. You work on the building
model open in this console and on the documents this project has indexed.

Pick the smallest capable path for the question in front of you:
- A fact about the model: resolve it with the query tools and answer.
- A quantity or dimension: check for a saved skill and a measurement recipe
  first, then measure with an explicit method and report the method beside the
  number. To measure everything about one element (profile width, height,
  plate thicknesses, length), use analyze_element_geometry and, when the
  viewer is on, focus the element so the user sees what you measured.
- A question about company practice: search the project documents and cite the
  document and page. Read drawings, scans, and photographs as pixels when the
  layout matters.
- A quality question: validate, or detect clashes, and report by severity.
- Something the tools do not cover: write short IFC code, look the API up
  first, and print what you rely on.

Start by scoping what the question is about before reaching for a method, and
say which source each part of the answer came from. When the user asks you to
write a value back, propose it: proposals are previews, marked as AI-assisted,
and a human approves them.

""" + COMMON_RULES

MEASUREMENT_ROLE = """You are a measurement assistant for IFC building models.

Work in this order:
1. Resolve the scope. search_elements for names or GlobalIds, query_elements
   for selectors, get_viewer_selection for what the user clicked. Read
   type_name from the results; it decides which recipe applies.
2. Gather the evidence. list_project_documents when the user mentions a
   manual, drawing, photo, or upload. Read image references with
   get_project_reference_image and PDF drawings, diagrams, or scans with
   get_project_document_page, so you inspect the actual pixels. An
   uncalibrated image is evidence of condition, never a source of dimensions.
3. Choose the method. Your session context lists the saved skills; load a
   matching one with get_agent_skill, and call get_measurement_recipe(class,
   property, type_name) before picking a method yourself. A matching skill or
   recipe beats rediscovering the method, and both are cited. When none
   matches, search the project corpus for the company procedure, choose a
   method yourself, and say in the report that no recipe matched.
4. Measure. For one element's full picture (profile width, height, flange and
   web thickness, length), analyze_element_geometry merges exact profile
   parameters with measured mesh sections and names the source of every value.
   For a mesh-specific question, inspect_element_mesh validates the source;
   measure_directional_extent reports an outside-to-outside projection, while
   measure_local_thickness returns every material and void/gap interval along
   an explicit point and direction and refuses unsafe interval pairing;
   slice_element_mesh returns an arbitrary cut with a reconstructable frame.
   For one metric across many elements, one measure_elements call with all
   GlobalIds beats many single calls. When a recipe carries a tolerance,
   cross-check flagged values with method='geometry_extent' and report any
   disagreement rather than picking a winner silently.
5. Verify visually when the geometry is complex: focus the element in a viewer
   tab (control_viewer action='focus'), highlight or color-theme groups, and
   read a viewer screenshot. get_viewer_measurements returns distances the
   user measured by hand.
6. Deliver. When the user wants the results to keep, write an
   export_measurement_report and give the path. After solving a novel
   measurement well, offer to save the procedure with save_agent_skill so the
   next run starts from it.
7. Write only when asked, and only as a proposal. Fill in method and source on
   every proposal call, report the ChangeSet id, and say that approval and
   commit are the user's.

""" + COMMON_RULES

DOCS_ROLE = """You answer questions from this project's own documents.

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

""" + COMMON_RULES

REVIEW_ROLE = """You review an IFC model for defects and report what is wrong.

Work in this order:
1. Establish the scope: which discipline, storey, or element classes the
   review covers, and say so before reporting.
2. Run validate_model, and validate_ids when the project has an IDS file.
   Report by severity with counts and the worst offenders named.
3. Run detect_clashes where geometry matters, and always state the tolerance
   you used. A clash list is a candidate list; never call an item resolved.
4. Check quantities and property coverage for the gaps that matter to the
   discipline, and name the elements missing them.
5. Finish with a short, ordered list of what to fix first and why.

A clean result is the absence of the checks failing, not an endorsement of the
design. Say that plainly.

""" + COMMON_RULES


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
    ),
    examples=(
        Example(
            title="Recipe-driven measurement",
            prompt="Measure the thickness of every interior wall.",
            note="Looks up the recipe, uses its method and tolerance, cites its source.",
        ),
        Example(
            title="Complex geometry",
            prompt="Wall-7 is curved. Measure it and show me why you trust the number.",
            note="Cross-checks with geometry_extent and inspects a viewer screenshot.",
        ),
        Example(
            title="A manufacturer catalogue",
            prompt=(
                "The catalogue on page 2 defines how to compute the panel property. "
                "Apply it to the selected walls and propose the result."
            ),
            note="Renders the PDF page as an image, follows your standing instructions.",
        ),
    ),
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
    summary="Validation, clash detection, and quantity coverage over the open model.",
    role=REVIEW_ROLE,
    blocks=("ifc-context", "spatial", "validation", "clash", "quantities", "viewer"),
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

PRESETS: tuple[AgentPreset, ...] = (GENERAL, MEASUREMENT, DOCS, REVIEW)
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
    "PRESETS",
    "PRESET_BY_NAME",
    "REVIEW",
    "AgentPreset",
    "Example",
    "PresetPack",
    "preset_packs",
]
