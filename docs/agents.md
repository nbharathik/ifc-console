# Agent applications

The agent runtime is the optional `ifc-console-agents` product. Use it when an
LLM should choose IFC and application tools; use [`Workbench`](sdk.md) from
core for deterministic scripts and CI. The package depends on a compatible
`ifc-console` and registers chat routes, agent operations, state, and its
browser panel through `ifc_console.extensions`.

```bash
pip install ifc-console-agents
```

`ifc-console-agents` also owns built-in and custom packs, provider chat,
PDF text and page rendering, LangGraph checkpoints, devkit/rehearsal tools,
and provider-free testing helpers. The core viewer and its MCP controls
continue to work when this package is absent.

```text
runtime -> scoped Toolset -> Agent -> typed events
              |               |
       IFC + app tools     host approvals
```

Your application owns identity, credentials, business policy, and persistence.
IFC Console supplies model sessions, operation schemas, limits, capability
checks, approvals, and audit records.

## Choose a runtime

`LocalRuntime` embeds a model:

```python
from ifc_console import LocalRuntime

async with await LocalRuntime.open("architecture.ifc", mode="ask") as runtime:
    print((await runtime.workspace.info())["project"])
```

`ConsoleRuntime` uses the model selected in a running console:

```python
from ifc_console import ConsoleRuntime

runtime = await ConsoleRuntime.connect_http(
    "http://127.0.0.1:8383/mcp",
    token=session_token,
)
```

Remote runtimes have no mode setter. The console owner controls `ask` and
`edit`.

## Scope the tools

Select exact operations whenever possible:

```python
tools = await runtime.tools(
    "get_ifc_project_info",
    "search_elements",
    "get_element",
    "get_psets",
)
result = await tools.call("search_elements", {"term": "Wall-1"})
```

For metadata-based selection, filter the permitted catalog:

```python
catalog = await runtime.toolset(permitted_only=True)
review_tools = (
    catalog
    .include(tags={"read", "analysis", "preview"})
    .exclude("open_ifc_file", "save_ifc_file", "execute_ifc_code")
)
```

`include()` and `exclude()` accept names, globs, tags, and capabilities. Common
allowlists are also available:

```python
from ifc_console import IfcToolProfile

inspect_tools = await runtime.toolset(profile=IfcToolProfile.INSPECT)
property_tools = await runtime.toolset(profile=IfcToolProfile.PROPERTY_EDIT)
```

Profiles improve usability. Runtime policy and host approval remain the
security boundary.

## Add application tools

Expose trusted Python functions with `FunctionToolSource`:

```python
from ifc_console import FunctionToolSource

company = FunctionToolSource(namespace="company")

@company.tool(tags={"validation"})
async def submission_requirements(discipline: str) -> dict:
    return await requirements_service.for_discipline(discipline)

tools = await runtime.toolset(company)
# Tool name: company__submission_requirements
```

Add another MCP server with `McpToolSource`:

```python
from ifc_console import McpToolSource

async with await McpToolSource.connect_stdio(
    "company-mcp", ["serve"], namespace="erp"
) as erp:
    tools = await runtime.toolset(erp)
```

Use an [operation plugin](plugins.md) only when an operation should appear in
every IFC Console interface.

## Run an agent

```python
from ifc_console_agents import Agent, AgentLimits, InMemoryThreadStore, ProviderModel

agent = Agent(
    name="submission-reviewer",
    model=ProviderModel(
        provider="local",
        model="company-model",
        base_url="http://localhost:8000/v1",
        local_only=True,
    ),
    tools=review_tools,
    instructions="Use IFC tools for every factual model claim.",
    thread_store=InMemoryThreadStore(),
    limits=AgentLimits(max_tool_rounds=10, max_tool_calls=40),
)

result = await agent.run("Review the submission", thread_id="review-42")
print(result.text)
```

Build the toolset with a core runtime and pass it to `Agent`, as above.
`tools.describe()` renders a prompt-ready catalog. Read-only calls in the same
round run concurrently unless `parallel_read_only=False`.

For validated output, pass a Pydantic model:

```python
from pydantic import BaseModel

class Finding(BaseModel):
    global_id: str
    problem: str

result = await agent.run("Review the walls", response_model=Finding)
print(result.data.global_id)
```

One failed validation is returned to the model for correction. A second failure
raises `AgentRunError`.

## Events, images, and tests

`agent.stream()` emits `run_started`, text and reasoning deltas, tool start and
finish events, approval events, usage, and exactly one final `run_completed` or
`run_failed`. Every event carries run and thread IDs.

Attach images with `AgentImage.from_file()`:

```python
from ifc_console_agents import AgentImage

result = await agent.run(
    "Inspect this detail",
    images=[AgentImage.from_file("detail.png")],
)
```

Image tool results become native vision input automatically. Keep thread-store
limits small because image-bearing transcripts grow quickly.

`ifc_console_agents.testing` provides `ScriptedAgentModel`, `RecordingThreadStore`,
`text_round`, `tool_call_round`, `ok_envelope`, and `error_envelope` for tests
without a provider key.

## Host approvals

Write, commit, restore, subprocess, and destructive operations require host
approval. The default policy denies them, and the model cannot approve itself.

```python
from ifc_console_agents import ApprovalDecision, CallbackApprovalHandler

async def approve(request):
    allowed = await policy_service.authorize(
        principal=current_user,
        action=request.tool_name,
        arguments=request.arguments,
    )
    return ApprovalDecision(
        approved=allowed,
        decided_by=current_user.id,
        reason="company policy",
    )

handler = CallbackApprovalHandler(approve)
```

Pass `approval_handler=handler` to `Agent`, `agent.run()`, or the runtime's
agent factory.

Keep credentials, mode changes, allowed paths, ChangeSet approval, commit, and
restore in host code. Approval does not bypass capability, revision, or path
checks.

Middleware can wrap calls for tracing, quotas, or caching. `AgentToolSource`
can expose a bounded specialist as one namespaced supervisor tool. Prefer
deterministic [workflows](workflows.md) when no model decision is required.

## Framework integrations

IFC Console does not install LangChain. Applications may project a toolset into
their own LangChain agent:

```python
from langchain.agents import create_agent

agent = create_agent(
    model="openai:YOUR_MODEL_ID",
    tools=tools.as_langchain_tools(),
    system_prompt="Use tools for IFC facts.",
)
```

Implement `AgentModel` for another provider library and `ThreadStore` for your
database. `JsonThreadStore(path)` suits small local applications. The LangGraph
adapter ships with `ifc-console-agents` and is described in the
[SDK guide](sdk.md#langchain-and-langgraph).

## Built-in agents

Five presets ship in `ifc_console_agents.presets`:

| preset | purpose |
| ------ | ------- |
| `general` | full query, document, measurement, review, proposal, and code surface |
| `measurement` | skill-first parametric measurement with cited, conflict-aware evidence |
| `parameters` | gap list for a selected element, values derived from geometry, context, and documents, AI-marked proposals |
| `docs` | answers from project references with page citations |
| `review` | quality scorecard, schema, IDS, health, clash, and quantity review |

They use the same public `Agent`, `Toolset`, and provider contracts as custom
applications. `examples/sdk/quickstart_agent.py` is the smallest runnable
example. Install `ifc-console-agents`, start the console, and run `/agent` for
the lazily loaded browser panel.

## Project workspace

Project-local agent data is inspectable and versionable:

```text
.ifc-console/
  agents/
    references/       shared documents and images
    content-access.json
    custom/           custom agent blueprints
    skills/           reusable markdown procedures
  knowledge/          retrieval index and source manifest
  recipes/            reviewed measurement recipes
```

Private conversation threads are not project files. The panel stores them per
project under `~/.ifc-console/agents/projects/<project-hash>/threads/` (or the
configured `IFC_CONSOLE_HOME`) so prompts and tool output cannot be committed
with a repository. Older project-local panel threads are migrated on startup.

Add references in **Agent workspace > Content**, with
`ifc-console agents files <paths>`, or by copying supported files into
`agents/references/`. Access may be `all` or an explicit selected set, including
an empty set. The server enforces it for search, document reads, reference
images, and rendered PDF pages.

A composer upload is turn-only context and does not change standing access.
Typing `@` attaches a permitted project file to one message. Changing provider,
model, instructions, or content access starts a compatible new context.

The corresponding local API is small:

| endpoint | purpose |
| -------- | ------- |
| `GET /api/agents/workspace?agent=<name>` | resolved prompt, tools, limits, files, and pipeline |
| `GET /api/agents/content?agent=<name>` | library and per-file access state |
| `POST /api/agents/content/upload?name=<file>` | add and index a shared reference |
| `POST /api/agents/content/access` | save `{agent, mode, paths}` |
| `POST /api/agents/geometry/review` | bounded, read-only analysis of one model-scoped GlobalId |
| `POST /api/agents/skills/dry-run` | read-only applicability and extraction preview for one v2 skill |

The two review endpoints require `model`, so a selected GlobalId stays pinned
to the IFC file that supplied it. The geometry endpoint fixes the frame to
`semantic` and automatic station discovery; the workspace requests standard
detail so it can review representative sections and independent alternatives.
The skill endpoint always forces `dry_run=true`, even if a caller sends false.
Neither endpoint proposes or writes properties.

## Capability blocks

Built-in and custom agents are composed from reviewed blocks. Blocks narrow
the tool surface but never widen session policy.

| block | adds |
| ----- | ---- |
| `ifc-context` | elements, types, properties, schema docs, and the property gap audit |
| `spatial` | hierarchy and georeferencing |
| `documents` | project search, images, and rendered PDF pages |
| `measurements` | recipes, geometry, distances, and reports |
| `quantities` | takeoff and CSV artifacts |
| `validation` | schema and IDS checks, model health, and the quality scorecard |
| `clash` | intersection and clearance checks |
| `revisions` | attached models and revision diffs |
| `viewer` | selection, screenshots, highlights, and viewport control |
| `skills` | reusable project procedures |
| `property-proposals` | marked, preview-only property changes |
| `ai-audit` | AI-authored value inventory |
| `code` | generated IfcOpenShell code for uncovered cases |

Compose the same blocks in Python:

```python
from ifc_console_agents.blocks import compose

composition = await compose(
    runtime,
    ["ifc-context", "documents", "measurements", "property-proposals"],
    role="You are a facade compliance assistant.",
    extra_instructions=company_procedure,
    viewer=False,
    agent="facade-agent",
)
```

In the browser use **Agent workspace > Agents**, or use `/agent new` in the
terminal. Blueprints choose only reviewed blocks, instructions, limits, and
starter prompts. They cannot load code, name arbitrary operations, approve a
ChangeSet, or change runtime policy.

## Selected-object geometry behavior

The built-in `general` and `measurement` agents use the same deterministic
path for geometry questions:

1. For "this" or "selected", read `get_viewer_selection` and pass its
   `model_id` to every following query, geometry, and skill call.
2. Load a matching saved skill before inventing a method, then check the
   measurement recipe when one applies.
3. Start with one `analyze_element_geometry` call using compact detail, the
   semantic frame, and automatic stations.
4. Open detailed sections, local thickness, mesh health, or screenshots only
   for flagged ambiguity, conflict, invalid topology, or requested evidence.
5. Present extracted, unavailable, ambiguous, and conflicting measurements as
   separate groups, retaining alternatives and source deltas.
6. For repetition, review a structured skill and dry-run its explainable
   similarity match before applying it.
7. Keep property proposals as a later, separately confirmed action.

Semantic ids keep one meaning across IFC classes. World rotation does not turn
profile width into length. Variable profiles are reported as ranges and
station domains. Hollow or non-manifold geometry can be refused when safe
material interval pairing or volume cannot be established.

## Element parameter inference

Most delivered files carry elements whose property sets are absent or half
empty. The `parameters` preset, and the `general` agent when asked what an
element is missing, follow one procedure:

1. Pin the viewer selection and its `model_id`.
2. Call `audit_element_properties`. It reads the schema's property set
   templates for the class and predefined type and returns, per applicable
   property and quantity set, what is filled on the occurrence or inherited
   from the type, what is empty or absent, and where each gap is usually
   derived from: geometry, material, spatial position, documents, the type,
   or a person.
3. Gather evidence cheapest first: one compact geometry analysis and derived
   quantities, the type and its filled siblings, the spatial position, then
   the project documents and reference images read as pixels, then generated
   code for anything the tools do not expose.
4. Present every candidate with unit, IFC nominal type, method, source, and
   confidence. Dimensions are never taken from an uncalibrated image, and a
   value that cannot be justified is left out.
5. Propose only on request or behind a workflow gate. Proposals land in the
   reserved `IfcConsole_AI_` property sets with a provenance record, so the
   AI-assisted layer stays separable from authored data.

The `element-parameters` workflow packages this as a click on a selected
object: an analysis stage that cannot reach a proposal tool, a human gate
showing the dossier, and a proposal stage that writes only the reviewed
candidates. `assess_model_quality` answers the whole-model question the same
way: a deterministic scorecard the `review` agent explains and prioritises.

## AI-marked proposals

Agents may create previews, never commits. Values go only into
`IfcConsole_AI_Measurements` or `IfcConsole_AI_Properties`. Each value and its
per-property record in `IfcConsole_AI_Provenance` form one ChangeSet, so they
are approved and committed atomically.

The preview-only tools are:

- `measure__propose_measured_value` for standard metrics;
- `measure__propose_property_value` for named values defined by instructions or evidence.

Provenance records include the agent, target property, model, method, source,
unit, confidence, timestamp, and proposal ID. `list_ai_authored_properties` and
`ifc_console_agents.provenance.read_ai_properties` returns values with
`provenance_by_property`. The `IfcConsole_AI_` prefix keeps the complete
AI-assisted layer identifiable.

## Measurement recipes

Recipes are host-authored YAML in `.ifc-console/recipes/`. They pin a property
method and citation:

```yaml
applies_to: {class: IfcWall, type_name: "Basic Wall: Interior*"}
property: thickness
method: layer_sum
params: {exclude_layers: ["*Finish*", "*Render*"]}
unit: mm
tolerance: 2
source: {document: "QS-Manual.pdf", page: 12}
```

`get_measurement_recipe` returns the most specific type, predefined-type, or
class match. Agents may read recipes but cannot write them.

### Skills

Skills are reviewable markdown procedures in `.ifc-console/agents/skills/`.
Agents receive their names and applicability at composition time, load full
instructions with `get_agent_skill`, and may write with `save_agent_skill`
only after host approval. Import a skill from **Agent workspace > Skills**,
`POST /api/agents/skills/import?name=<file>.md`, or by copying the file into
the skills directory. Existing names are never overwritten during import.

A version 2 parametric measurement skill adds these front-matter fields:

```yaml
kind: parametric_measurement
schema_version: 2
```

Its Markdown body contains exactly one fenced `measurement-spec` JSON object.
The validated object records applicability, stable output ids, preferred and
fallback sources, semantic frame, confidence and tolerance requirements,
exemplar revision and signatures, and verification behavior. For example:

```measurement-spec
{
  "schema_version": 2,
  "kind": "parametric_measurement",
  "applicability": {
    "ifc_classes": ["IfcMember"],
    "profile_families": ["i_shape"],
    "geometry_families": ["constant_profile_extrusion"],
    "hard_requirements": ["constant_or_piecewise_profile"],
    "similarity_threshold": 0.85
  },
  "measurements": [
    {
      "output": "profile.web_thickness",
      "rule_type": "object_measurement",
      "preferred_sources": ["profile_parameter", "mesh_section"],
      "fallbacks": ["adaptive_section.thickness_modes"],
      "frame": "semantic",
      "minimum_confidence": "medium",
      "tolerance": {"absolute_si": 0.001, "relative": 0.02},
      "unresolved": false,
      "intent": {"viewer_kind": "distance", "viewer_index": 0}
    }
  ],
  "verification": {"cross_check": "second_source_when_available"},
  "outputs": ["profile.web_thickness"]
}
```

The executable block is authoritative for deterministic replay, but it is not
a source of facts about the current model. `list_agent_skills` and the
workspace expose `kind`, `schema_version`, `structured`, `executable`, and
`spec_status`. Status is `valid`, `review_required`, `invalid`, or `none` for
a prose-only skill. An unresolved output remains visible for naming and cannot
execute by accident.

#### Recording a skill from the viewer

Measure an element in the 3D viewer, then use the composer plus menu
(**Save measurements as a skill**) or **Agent workspace > Skills > Record
from viewer**. The console reads the viewer's measurement list, runs
`analyze_element_geometry` on every referenced element, up to the explicit
25-element endpoint limit, and pins the analysis to the measurement tab's
`model_id`, fingerprint, and revision. It stores semantic or local direction,
snap kinds, anchor relationship, stable measurement matches, tolerance, and
the exemplar geometry signature. World coordinates are evidence for intent,
not replay coordinates.

Distance between two objects becomes a relationship intent rather than one
object's dimension. Area, path, angle, clearance, and element-size records use
distinct rule types. Relationship intents retain explicit `from` and `to`
object roles, their GlobalIds, anchor indexes, and bounded local-point or reach
evidence when available. A match uses semantic direction, feature relationship,
and scale-aware value tolerance. Ambiguous matches stay unresolved for human
review instead of being guessed. The same recording is available to scripts
as `POST /api/agents/skills/record` with
`{"name", "notes", "overwrite"}`. The response includes structured metadata,
unresolved intent indexes, analysis status, and the pinned model revision.

In **Agent workspace > Skills**, **Analyze selection** renders grouped version
2 measurements and coverage. The read-only review also shows the semantic
frame vectors and provenance, adaptive profile regions, a representative
section-station slider, exact IFC versus measured evidence badges, and
expandable source alternatives with deltas, tolerance, uncertainty, and
conflict reasons. These are workspace diagrams from the analysis payload, not
objects injected into the Three.js scene. An executable skill offers **Review
dry run on selection**, which renders applicability score, match reasons,
extracted values, skipped targets, and ambiguous outputs. **Prepare a separate
property proposal** appears only as a follow-up action after a complete current
dry-run with extracted values. It starts a proposal request; it does not change
or commit the IFC.

`apply_measurement_skill` requires exactly one of `selector` or `global_ids`,
is paged and bounded, and defaults to `dry_run=true`. It returns a row for
every accepted or rejected target. Type, profile and geometry family,
representation compatibility, topology, normalized proportions, and intrinsic
signature contribute to the score. Same class alone does not pass.

Existing prose-only skills continue to load and guide an agent. They cannot use
deterministic replay. `preview_measurement_skill_migration(name)` returns a
read-only version 2 suggestion with inferred classes and outputs, unresolved
measurement intents, canonical content, and explicit review items. The result
is not executable and leaves the source unchanged. Review it, resolve every
item, then save under a new name unless overwrite was explicitly approved.
Reading, listing, importing, or previewing migration of an old skill never
rewrites it.
