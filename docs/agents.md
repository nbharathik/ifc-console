# Agent applications

The agent runtime is part of `ifc-console`. Use it when an LLM should choose
IFC and application tools. Use [`Workbench`](sdk.md) for deterministic scripts
and CI.

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

`runtime.create_agent(...)` builds the toolset and agent together.
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
from ifc_console import AgentImage

result = await agent.run(
    "Inspect this detail",
    images=[AgentImage.from_file("detail.png")],
)
```

Image tool results become native vision input automatically. Keep thread-store
limits small because image-bearing transcripts grow quickly.

`ifc_console.testing` provides `ScriptedAgentModel`, `RecordingThreadStore`,
`text_round`, `tool_call_round`, `ok_envelope`, and `error_envelope` for tests
without a provider key.

## Host approvals

Write, commit, restore, subprocess, and destructive operations require host
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
database. `JsonThreadStore(path)` suits small local applications. The optional
`ifc-console[graph]` extra adds the LangGraph adapter described in the
[SDK guide](sdk.md#langchain-and-langgraph).

## Built-in agents

Four presets ship in `ifc_console.agents.presets`:

| preset | purpose |
| ------ | ------- |
| `general` | full query, document, measurement, review, proposal, and code surface |
| `measurement` | recipe-driven measurement with cited evidence |
| `docs` | answers from project references with page citations |
| `review` | schema, IDS, clash, quantity, and data-quality review |

They use the same public `Agent`, `Toolset`, and provider contracts as custom
applications. `examples/sdk/quickstart_agent.py` is the smallest runnable
example. For the browser panel, install `ifc-console[viewer]`, start the
console, and run `/agent`.

## Project workspace

Project-local agent data is inspectable and versionable:

```text
.ifc-console/
  agents/
    references/       shared documents and images
    content-access.json
    custom/           custom agent blueprints
    skills/           reusable markdown procedures
    threads/          optional local conversations
  knowledge/          retrieval index and source manifest
  recipes/            reviewed measurement recipes
```

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

## Capability blocks

Built-in and custom agents are composed from reviewed blocks. Blocks narrow
the tool surface but never widen session policy.

| block | adds |
| ----- | ---- |
| `ifc-context` | elements, types, properties, and schema docs |
| `spatial` | hierarchy and georeferencing |
| `documents` | project search, images, and rendered PDF pages |
| `measurements` | recipes, geometry, distances, and reports |
| `quantities` | takeoff and CSV artifacts |
| `validation` | schema and IDS checks |
| `clash` | intersection and clearance checks |
| `viewer` | selection, screenshots, highlights, and viewport control |
| `skills` | reusable project procedures |
| `property-proposals` | marked, preview-only property changes |
| `ai-audit` | AI-authored value inventory |
| `code` | generated IfcOpenShell code for uncovered cases |

Compose the same blocks in Python:

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
```

In the browser use **Agent workspace > Agents**, or use `/agent new` in the
terminal. Blueprints choose only reviewed blocks, instructions, limits, and
starter prompts. They cannot load code, name arbitrary operations, approve a
ChangeSet, or change runtime policy.

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
`ifc_console.agents.provenance.read_ai_properties` return values with
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
