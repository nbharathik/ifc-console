# Building agent applications

Use these APIs when an LLM should choose IFC and company tools. For ordinary
scripts or CI, use [`Workbench`](sdk.md) instead.

```text
runtime -> scoped toolset -> Agent -> typed events
              |                |
       IFC + company tools   host approvals
```

Your application owns identity, credentials, business policy, and production
persistence. ifc-console supplies model sessions, tools, capability checks,
limits, approvals, and audit records.

## Choose a runtime

`LocalRuntime` opens a model in the application:

```python
from ifc_console import LocalRuntime

runtime = await LocalRuntime.open("architecture.ifc", mode="ask")
async with runtime:
    print((await runtime.workspace.info())["project"])
```

`ConsoleRuntime` connects to the model selected in a running console:

```python
from ifc_console import ConsoleRuntime

runtime = await ConsoleRuntime.connect_http(
    "http://127.0.0.1:8383/mcp",
    token=session_token,
)
```

Remote runtimes have no mode setter. The user who owns the console owns its
`ask`/`edit` switch.

## Scope the tools

Build the smallest toolset the agent needs:

```python
tools = await runtime.tools(
    "get_ifc_project_info",
    "search_elements",
    "get_element",
    "get_psets",
)

result = await tools.call(
    "search_elements",
    {"term": "Wall-1"},
)
```

For metadata-based selection, start with the complete permitted catalog and
filter it:

```python
catalog = await runtime.toolset(permitted_only=True)

review_tools = (
    catalog
    .include(tags={"read", "analysis", "preview"})
    .exclude("open_ifc_file", "save_ifc_file", "execute_ifc_code")
)
```

For common cases, start with an allowlisted profile:

```python
from ifc_console import IfcToolProfile

inspect_tools = await runtime.toolset(profile=IfcToolProfile.INSPECT)
property_tools = await runtime.toolset(profile=IfcToolProfile.PROPERTY_EDIT)
```

`inspect` omits free-form code, file/workspace changes, jobs, and model writes.
`property-edit` adds structured property previews but still omits commit and
save. `full` preserves the complete permitted surface. Profiles are a usability
allowlist; runtime capabilities and host approvals remain the security boundary.

`include()` and `exclude()` accept names, globs, tags, and capabilities. Tool
definitions include source, tags, capabilities, current permission, and whether
host approval is required.

## Add application tools

Use `FunctionToolSource` for trusted Python functions used only by this app:

```python
from ifc_console import FunctionToolSource

company = FunctionToolSource(namespace="company")

@company.tool(tags={"validation"})
async def submission_requirements(discipline: str) -> dict:
    """Return company requirements for one discipline."""
    return await requirements_service.for_discipline(discipline)

tools = await runtime.toolset(company)
# Exposed as company__submission_requirements.
```

Use `McpToolSource` to add an existing MCP server:

```python
from ifc_console import McpToolSource

async with await McpToolSource.connect_stdio(
    "company-mcp",
    ["serve"],
    namespace="erp",
) as erp:
    tools = await runtime.toolset(erp)
```

Namespacing prevents collisions. Use an [operation plugin](plugins.md) only
when the tool should appear automatically in every ifc-console interface.

## Run an agent

```python
from ifc_console import Agent, AgentLimits, InMemoryThreadStore, ProviderModel

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

The same construction is available as `await runtime.create_agent(...)`, which
builds the selected toolset and agent in one call.

`tools.describe()` renders a prompt-ready summary of the selected tools, so
system prompts list the surface from code instead of by hand. Rounds where
every requested call is read-only execute concurrently; set
`AgentLimits(parallel_read_only=False)` to serialize them again.

## Structured answers

Applications want data out of agents, not prose. Pass a Pydantic model and
the run ends in a validated instance, with one retry that feeds the
validation error back to the model:

```python
from pydantic import BaseModel

class Finding(BaseModel):
    global_id: str
    problem: str

result = await agent.run("Review the walls", response_model=Finding)
print(result.data.global_id)
```

The schema is appended to the prompt; a final answer that still does not
validate raises `AgentRunError`.

## Test without an LLM key

`ifc_console.testing` ships the fakes the internal suite uses:
`ScriptedAgentModel` plays back provider rounds (`text_round`,
`tool_call_round` build them), `RecordingThreadStore` remembers saves, and
`ok_envelope`/`error_envelope` shape tool results. A complete agent test
needs a model file and no network; `tests/unit/test_builtin_agents.py` shows
the pattern end to end.

## Use LangChain/LangGraph directly

Install LangChain and its model-provider package in the agent application's own
environment. IFC Console does not install or depend on LangChain.

```python
from langchain.agents import create_agent

tools = await runtime.tools(
    "get_ifc_project_info",
    "search_elements",
    "get_element",
    "company__submission_requirements",
    sources=(company,),
)
agent = create_agent(
    model="openai:YOUR_MODEL_ID",
    tools=tools.as_langchain_tools(),
    system_prompt="Use tools for IFC facts.",
)
result = await agent.ainvoke({
    "messages": [{"role": "user", "content": "Review Wall-1"}]
})
```

This direct adapter is for a custom in-process agent. Use `ConsoleRuntime` when
you want the same API backed by a user's existing console, or use a normal
LangChain MCP client when protocol isolation and independent server lifecycle
are more important than in-process customization.

Implement `AgentModel` for another provider library and `ThreadStore` for your
database. `JsonThreadStore(path)` is suitable for small local applications;
protect its directory because transcripts can contain model data.

`agent.stream()` emits typed events for text, reasoning, tool calls, approvals,
usage, completion, and failure. Every event carries run and thread IDs.

## The event contract

| event | fires | fields set beyond run and thread ids |
| ----- | ----- | ------------------------------------ |
| `run_started` | once, first | |
| `text_delta` | per streamed answer fragment | `text` |
| `reasoning_delta` | per streamed reasoning fragment, providers that expose it | `text` |
| `tool_call_started` | before each tool executes, always paired with a finish | `tool_call_id`, `tool_name`, `arguments` |
| `approval_requested` | when a protected tool waits on the host | the started fields plus `approval` |
| `approval_resolved` | when the host decides | `tool_call_id`, `tool_name`, `decision` |
| `tool_call_finished` | after each tool, budget and parse failures included | the started fields plus `result` |
| `usage` | after each model round that reports tokens | `usage` |
| `run_completed` | once, on success, last | `run_result` |
| `run_failed` | once, on timeout, budget exhaustion, or provider failure, last | `text` |

Exactly one of `run_completed` or `run_failed` ends every run. In a round
where every requested call is read-only, all started events precede the
finished events because the calls execute concurrently.

## Vision

`Agent.run(..., images=[AgentImage.from_file("detail.png")])` attaches images
to the prompt; providers that support vision receive them as native image
content. Tool results that carry images (such as `get_viewer_screenshot`
through the SDK) are split automatically: the transcript keeps a count where
the base64 was, and the pixels follow the round as vision input. Image-bearing
threads grow quickly; size `JsonThreadStore` limits accordingly.

## Protect actions

Write, commit, restore, subprocess, and destructive MCP tools require host
approval. The default policy denies them, and the model cannot approve itself.

```python
from ifc_console import ApprovalDecision, CallbackApprovalHandler

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

Approval is an extra boundary. Normal mode, capability, path, revision, and
ChangeSet checks still apply. Keep mode changes, ChangeSet approval, commit,
restore, allowed directories, and credentials in host code.

## Middleware and specialists

Middleware wraps each tool call for tracing, quotas, caching, or logging:

```python
async def trace_tool(call, call_next):
    with tracer.start_as_current_span(call.name):
        return await call_next(call)

agent = Agent(..., middleware=(trace_tool,))
```

`AgentToolSource` can expose a bounded specialist agent as one namespaced tool
of a supervisor. Use deterministic [workflows](workflows.md) for repeatable
validation/query graphs and delegation only when a model decision is needed.

## Reference application

`examples/sdk/quickstart_agent.py` is the smallest runnable agent: it opens a
model, selects six read tools, and streams the bundled `Agent` in a terminal.

The complete browser chat is part of IFC Console itself. PDF ingestion and PDF
page vision ship in the base package; install the viewer extra for the browser
panel, start the Console in a project folder, and open the agent picker:

```bash
pip install "ifc-console[viewer]"
ifc-console
# then type /agent
```

The main panel demonstrates the same public `Agent`, `Toolset`,
`ProviderModel`, typed event, and image contracts without maintaining a second
web-chat implementation under `examples/`.

`examples/sdk/property_agent` is a separate LangChain project with its own
`pyproject.toml` and virtual environment. It resolves elements by name,
GlobalId, selector, or viewer selection; inspects the unit; previews thickness;
then requires host-side approval before the durable transaction commit.

## The shipped agents

Four presets ship in `ifc_console.agents.presets`. They are data, not code: a
role prompt, a set of capability blocks, and some worked examples. The same
`compose()` call builds all of them and any agent a user creates, so a custom
agent is never a second-class citizen.

| Agent | Blocks | For |
| --- | --- | --- |
| `general` | every block, `code` included | Start here. Queries, quantities, documents, validation, generated code, and marked proposals in one place. |
| `measurement` | context, documents, measurements, viewer, proposals, audit | Recipe-driven measurement with a stated method and a cited source. |
| `docs` | context, documents, spatial | Answers from the project corpus, every claim cited with its page. |
| `review` | context, spatial, validation, clash, quantities, viewer | Schema, IDS, clashes, and missing data, worst first. |

The focused presets exist because a narrower agent is easier to trust and to
read, not because they are a different kind of thing: each is the general
assistant with fewer blocks and a sharper prompt. To make your own, write
standing instructions for one of them, or build a preset of your own from the
blocks below.

The project reference directory is:

```text
project/
  .ifc-console/
    agents/references/    local manuals, drawings, and images
    agents/content-access.json  standing access for built-in agents
    agents/custom/        your own agents, as inspectable JSON
    agents/skills/        saved measurement procedures, one markdown file each
    knowledge/            generated retrieval index and source manifest
    recipes/              reviewed measurement recipes
```

Add shared files in **Agent workspace > Content**, with
`ifc-console agents files <paths>`, or by copying them into
`agents/references/`. The Content view lets you search the library and grant
files in bulk: **Select shown** and **Clear shown** act on the filtered set,
and shift-click extends a range. Upload once, then select which files each
assistant may use as standing project context. A built-in agent's selection is stored in
`content-access.json`; a custom agent keeps the same selection in its blueprint
as `content_paths`.

No saved selection means access to all project content, which preserves the
behavior of existing projects. A saved empty selection means no standing file
access. The panel enforces the selection around document listing, retrieval
search, record reads, reference images, and rendered PDF pages. Changing it
starts a compatible new agent context, so a tighter selection cannot resume a
thread created with broader access.

`list_project_documents` lets an agent inspect its permitted evidence ledger;
`get_project_reference_image` provides image pixels as native vision input;
`get_project_document_page` renders a PDF page for native vision, so drawings,
scans, and layout-dependent tables are not reduced to extracted text. A file
attached from the composer is message context, not standing agent access. It is
stored below the hidden `references/.turns/` area and is available only to the
message that attaches it, without silently changing the agent's saved
selection or appearing in the shared library. Typing `@` in the composer
mentions a file the agent already has standing access to and attaches it to
that message, so the model is told what to read as well as being permitted
to read it.

## The agent workspace

`GET /api/agents/workspace?agent=<name>` returns one payload describing an
agent exactly as it would run right now: its role prompt, its blocks and which
are available, every tool it holds with its full description, input schema,
required capabilities, source, and pipeline stage, the stages it can reach,
its worked examples, what it may write, its limits, and the project files it
can see. Its `content` object carries the
shared library, the effective access mode, and an `allowed` flag on each file;
the compatibility `files` list contains only accessible files. The payload is
assembled from the same composition the agent runs with, so it cannot drift: a
tool missing from the workspace is a tool the agent does not have.

`agent=` with no name describes plain chat: the console's whole tool surface
behind the stateless loop, with no blocks and no server-side thread.

The browser has one **Agent workspace**, opened from the right side of the chat
header or **Agent workspace** at the bottom of the sidebar. A single left rail
contains **Agents**, **Pipeline**, **Capabilities**, **Tools**, **Content**,
**Models**, and **App**. Capability, workflow, and tool rows expand only when
their details are needed. Agent setup opens inside this same workspace. There
are no separate inspector, settings, and builder panels to coordinate. The
workspace is a bounded modal centred on the window, closed with its own
control, Escape, or a click outside it.

The content endpoints use the same project-local store and enforcement:

| endpoint | purpose |
| --- | --- |
| `GET /api/agents/content` | List the shared project content library. |
| `GET /api/agents/content?agent=<name>` | Add the selected agent's access mode and per-file `allowed` state. |
| `POST /api/agents/content/upload?name=<file>` | Add and index a shared workspace file without granting it to a selected-only agent. |
| `POST /api/agents/content/access` | Save `{agent, mode, paths}` where mode is `all` or `selected`. |

## Capability blocks

Every agent in this project, built-in or custom, is assembled from the same
list in `ifc_console.agents.blocks`. One `compose()` call decides an agent's
tool surface and safety preamble, so a custom agent is never a second-class
citizen and no agent can widen policy by construction.

| Block | What it adds |
| --- | --- |
| `ifc-context` | Elements, types, property sets, schema docs |
| `spatial` | Site, building, storey, space hierarchy and georeferencing |
| `documents` | Project corpus search, reference images, rendered PDF pages |
| `measurements` | Recipes, geometry extents, distances, quantities |
| `quantities` | Aggregated takeoff and CSV artifacts |
| `validation` | Schema checks and IDS conformance |
| `clash` | Intersection and near-touch detection |
| `viewer` | Selection, hand measurements, highlight, theme, screenshots |
| `property-proposals` | AI-marked, preview-only property and measurement writes |
| `ai-audit` | Inventory of every AI-authored value already in the model |
| `code` | Generated ifcopenshell code for what the tools do not cover |

Composition degrades instead of failing: a viewer block with no viewer, or a
validation block with no IDS engine, drops out and the agent is told in its
prompt what it cannot do, so it never promises a capability it lacks.

The `code` block is what answers a question the structured tools do not cover,
such as measuring a wall by walking its layer set. It is not a way around the
session gate: in ask mode a run is classified before it executes and anything
that would change the model is refused, while a read-only run goes to a
separate worker process against a copy of the file. In edit mode a mutating run
executes against the live in-memory model under the capability and audit
guards. Neither can write the IFC file: only the person at the console can.

```python
from ifc_console.agents.blocks import compose

composition = await compose(
    runtime,
    ["ifc-context", "documents", "measurements", "property-proposals"],
    role="You are a facade compliance assistant.",
    extra_instructions=company_procedure,
    viewer=False,
    agent="facade-agent",
)
agent = Agent(name="facade", model=model, tools=composition.tools,
              instructions=composition.instructions)
```

## Custom agents from blocks

Open **Agent workspace > Agents**, then choose **New assistant** or **Edit
agent**. `/agent new` remains available in the terminal. The compact setup flow
keeps the essential profile, capabilities, and instructions together. Choose
the smallest set of blocks, write the company procedure, and save. When needed,
expand **Advanced run controls** to select an adaptive, evidence-first, or
fast-scan strategy, set explicit tool-round and tool-call budgets, and add
starter prompts. The footer summarizes the resulting reach before saving. The
resulting agent appears beside Documents and Measurement in `/agent`, in
`ifc-console agents list`, and in the panel sidebar, where it can also be
deleted. Its standing content selection is edited in the workspace's Content
view and persisted with the blueprint.

Blueprints cannot name arbitrary operations, load code, approve ChangeSets, or
change runtime policy. The selected blocks expand to a fixed allowlist at build
time; viewer tools disappear when no viewer surface is available. This makes a
blueprint useful as reusable project configuration without turning it into a
plugin or a second security boundary.

## Marking what the model wrote

An agent may propose values, never commit them, and everything it proposes is
identifiable in the file afterwards. Two preview-only tools exist:

- `measure__propose_measured_value` for the standard metrics, into
  `IfcConsole_AI_Measurements`
- `measure__propose_property_value` for a property your own instructions or a
  document define, into `IfcConsole_AI_Properties`

The property set names are fixed in host code. Alongside every value the agent
writes an `AI_Provenance` record into `IfcConsole_AI_Provenance`:

```json
{"v": 1, "ai_generated": true, "agent": "measurement-agent",
 "property": "IfcConsole_AI_Measurements.MeasuredThickness",
 "method": "geometry_extent (local_y)", "model": "anthropic/claude-sonnet-5",
 "source": "QS-Manual.pdf p12", "unit": "mm", "confidence": "medium",
 "change_set": "sha256:...", "written_at": "2026-08-23T10:00:00+00:00",
 "tool": "ifc-console"}
```

Because every AI-authored property set starts with `IfcConsole_AI_`, the whole
AI-assisted layer is separable from the authored model by prefix match.
`list_ai_authored_properties` returns that inventory for review, and
`ifc_console.agents.provenance.read_ai_properties` does the same in Python.

## Measurement recipes

Recipes pin the method and citation as reviewable YAML under
`.ifc-console/recipes/`:

```yaml
applies_to: {class: IfcWall, type_name: "Basic Wall: Interior*"}
property: thickness
method: layer_sum
params: {exclude_layers: ["*Finish*", "*Render*"]}
unit: mm
tolerance: 2
source: {document: "QS-Manual.pdf", page: 12}
notes: structural layers only, per section 4.2
```

`get_measurement_recipe` resolves the most specific match (type before class)
and returns ready-to-use `measure_elements` arguments. Recipes remain
host-authored data: agents may read them but never write them.

## Skills

Skills complement recipes from the other side: where a recipe pins one
property's method as host-authored YAML, a skill records a whole worked
procedure as markdown that agents may both read and, with approval, write.
They live one file per skill under `.ifc-console/agents/skills/`:

```markdown
---
name: sheet-pile-profile
description: Measure a sheet pile's b, h, t_f, t_w and length
applies_to: IfcMember sheet piles
---

## When to use
The element is a thin-walled pile or profile member.

## Steps
1. get_viewer_selection, then control_viewer action='focus' on the element.
2. analyze_element_geometry with its GlobalId; read `dimensions` and the
   thickness pair (upper is t_f, lower is t_w on Larssen-style piles).
3. Cross-check against the type's catalogue page if one is indexed.
4. export_measurement_report when the user wants the result to keep.
```

The flow is deliberate. Any agent holding the skills block gets the saved
skills indexed straight into its system prompt at composition (name,
description, and applicability, along with the open model and mode), so
discovering them costs no tool round; `list_agent_skills` refreshes the list
mid-conversation, `get_agent_skill` loads the one that matches, and
`save_agent_skill` records a new one, asking the user for approval before
writing. Skills are procedures, not facts: agents are instructed to adapt ids
and selectors and never to copy session values out of one. The panel shows
them under **Agent workspace > Skills**, and the files diff cleanly in
version control.

Skills do not have to be written in the console. Draft one anywhere (any LLM,
any editor), then bring it in with **Import .md skills** in the Skills tab,
`POST /api/agents/skills/import?name=<file>.md`, or by copying the file into
`agents/skills/`. The importer reads the front matter when present, derives
the name and description from the file otherwise, and never overwrites an
existing skill (a taken name gets a numeric suffix).
