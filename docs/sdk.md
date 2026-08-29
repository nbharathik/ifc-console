# Python SDK

The framework-neutral deterministic SDK ships in `ifc-console` with MCP,
workflows, IFC operations, and the bundled browser viewer. The optional
`ifc-console-agents` distribution adds the provider-neutral agent SDK,
providers/chat, packs, testing/devkit helpers, and agent browser panel:

```bash
pip install ifc-console
pip install ifc-console-agents
```

| interface | use |
| --------- | --- |
| `LocalRuntime` | embedded core tools over a local model |
| `ConsoleRuntime` | tools connected to a running console |
| `Agent` | bounded, provider-neutral tool loop from `ifc-console-agents` |
| `Toolset` | scoped IFC, Python, and MCP operations |
| `FunctionToolSource` | trusted application functions as tools |
| `RuntimeSettings` | validated settings for an embedded session |
| `Workbench` | synchronous scripts, notebooks, and CI |
| `AsyncWorkbench` | the same API for async applications |

Start with `Workbench` for deterministic code and combine `LocalRuntime` with
the optional `Agent` only when a model must choose tools.

## Agent runtime

Keep model policy, settings, and tool selection visible in host code:

```python
from ifc_console import FunctionToolSource, LocalRuntime
from ifc_console_agents import Agent, ProviderModel

company = FunctionToolSource(namespace="company")

@company.tool()
async def property_rule() -> dict:
    return {"pset": "Company_ElementData", "property": "Thickness"}

async with await LocalRuntime.open() as runtime:
    await runtime.open_model("tower.ifc")
    runtime.set_mode("ask")
    runtime.settings.update({
        "exec.output_char_limit": 20_000,
        "files.allow_ai_save": False,
    })

    tools = await runtime.tools(
        "get_ifc_project_info",
        "search_elements",
        "get_element",
        "get_psets",
        "company__property_rule",
        sources=(company,),
    )
    agent = Agent(
        name="model-reviewer",
        model=ProviderModel(provider="local", model="company-model"),
        tools=tools,
        instructions="Use IFC tools for every factual model claim.",
    )
    result = await agent.run("Find walls without a fire rating")
    print(result.text)
```

`runtime.tools()` accepts exact names or globs. `runtime.toolset()` supports
profiles, tags, capabilities, and custom sources. Mode changes, credentials,
allowed paths, ChangeSet approval, and commit belong in host code.

`RuntimeSettings` changes only the current embedded session. Pass lifecycle
settings such as `server.port` to `LocalRuntime.open(settings=...)` because the
web surface needs them during construction.

Use `ConsoleRuntime` for the same tool-facing API over MCP when the model must
remain in a user's running console. That user retains mode and settings control.
See [Agent applications](agents.md) for limits, events, approvals, images,
middleware, thread stores, and specialist agents.

## LangChain and LangGraph

LangChain remains application-owned. Install it with the integration for your
model provider, then adapt an IFC Console toolset:

```python
from langchain.agents import create_agent
from ifc_console import LocalRuntime

async with await LocalRuntime.open("tower.ifc") as runtime:
    tools = await runtime.tools(
        "get_ifc_project_info", "search_elements", "get_element", "get_psets"
    )
    agent = create_agent(
        model="openai:YOUR_MODEL_ID",
        tools=tools.as_langchain_tools(),
        system_prompt="Use IFC tools before making model claims.",
    )
    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": "Inspect Wall-1"}]
    })
```

Use LangChain's async APIs so model sessions and tools stay on one event loop.
Structured IFC envelopes remain available as tool-message artifacts.

The standard `ifc-console-agents` installation includes checkpointed outer
workflows:

```python
from ifc_console_agents.integrations.langgraph import create_langgraph_workflow, graph_update

async def inspect(state):
    result = await reviewer.run(state["prompt"])
    return graph_update(final_text=result.text)

def configure(builder, start, end):
    builder.add_node("inspect", inspect)
    builder.add_edge(start, "inspect")
    builder.add_edge("inspect", end)

workflow = create_langgraph_workflow(configure, checkpointer=checkpointer)
async for event in workflow.stream("Inspect the walls", thread_id="review-1"):
    render(event)
```

The caller owns checkpointer lifecycle. Keep credentials, runtimes, IFC
handles, and image bytes outside graph state; store artifact and ChangeSet
references instead. `graph_approval_interrupt()` and `workflow.resume()` use
the same thread ID. Async approval interrupts require Python 3.11 or newer.

## Embed the web surface

`LocalRuntime.build_web_app()` returns the authenticated ASGI surface used by
the console. Core owns MCP, viewer routes, authentication, and the static
browser application. Installed products such as `ifc-console-agents` register
additional routes and a browser panel through `ifc_console.extensions`; the
panel assets are loaded lazily only when that extension is present and opened.

```python
runtime.enable_viewer()
surface = runtime.build_web_app(extra_routes=my_application_routes)

print(surface.viewer_url)
print(surface.browser_url("/my-chat"))
server = uvicorn.Server(uvicorn.Config(surface.app, host="127.0.0.1", port=8765))
await server.serve()
```

Your application owns server lifecycle, identity, provider credentials, and
custom UI.

## Workbench quick start

```python
from ifc_console import Workbench

with Workbench.open("tower.ifc") as wb:
    print(wb.info()["project"]["name"])
    walls = wb.query("IfcWall")
    report = wb.validate()
    print(len(walls), report["valid"])
```

The context manager closes model and worker resources.

## Common operations

| method | result |
| ------ | ------ |
| `orient()` | status, project summary, and shallow spatial tree |
| `info()` | schema, units, counts, and materials |
| `tree(depth=10)` | spatial containment tree |
| `search(term, limit=50)` | name, GlobalId, text, or selector matches |
| `query(selector, limit=50)` | selector result rows |
| `element(global_ids)` / `psets(global_ids)` | element details or property sets |
| `quantities(selector, by="storey")` | aggregated stored quantities |
| `validate()` / `validate_ids(path)` | schema or IDS results |
| `clashes(set_a, set_b)` | overlap or clearance results |
| `georeferencing()` | CRS and map conversion |
| `schema_docs(...)` | IFC entity, pset, or property docs |

Core knowledge methods include `search_knowledge()`, `knowledge_record()`, and
`api_docs()` over deterministic IFC schema/API references. Project document
and image retrieval belongs to `ifc-console-agents`, whose standard install
includes PDF text and rendered pages. Agent search hits retain document and
page provenance.

Geometry and measurement operations such as `get_element_geometry`,
`measure_elements`, `measure_distance`, and `get_measurement_recipe` are
available through `call()`. `get_viewer_screenshot` returns base64 images in
`data.images`.

## Modes and errors

`Workbench.open()` starts in `ask`. Set `mode="edit"` for writes:

```python
with Workbench.open("tower.ifc", mode="edit") as wb:
    wb.run_code(
        'project = ifc.by_type("IfcProject")[0]\n'
        'ifc_api.attribute.edit_attributes('
        'ifc, product=project, attributes={"Name": "Tower"})',
        "rename the project",
    )
    wb.save()
```

Convenience methods raise `IfcConsoleError` with a stable `code` and `hint`.
Low-level `call()` returns `{ok, data/error, meta}` instead.

```python
from ifc_console import IfcConsoleError

try:
    wb.query("IfcWall, ((broken")
except IfcConsoleError as exc:
    print(exc.code, exc.hint)
```

## Multiple models and tool bindings

One model is active and writable. Attached models are read-only:

```python
with Workbench.open("architecture.ifc") as wb:
    wb.attach("structure.ifc")
    wb.attach("mep.ifc")
    hits = wb.clashes(
        "IfcWall", "IfcDuctSegment", other_model="mep", tolerance=0.02
    )
```

`wb.tools(permitted_only=True)` returns provider-neutral JSON Schema
definitions. `wb.call(name, **arguments)` runs one operation. Definitions
include capabilities and current permission state.

Typed APIs include `query_result()`, `validation_result()`, `call_result()`, and
`operation_definitions()`. `wb.context` gives immutable workspace, model,
revision, and source-hash data; read it again after a write. The package ships a
`py.typed` marker and exports public contracts from `ifc_console`.

## Jobs and workflows

Long work runs in supervised workers and produces verified artifacts:

```python
with Workbench.open("tower.ifc") as wb:
    job = wb.submit_validation_job(ids_paths=("requirements.ids",))
    completed = wb.wait_job(job.job_id)
    for artifact in completed.artifacts:
        wb.export_artifact(artifact.artifact_id, artifact.name)
```

The SDK also supports validation and query batches, versioned YAML or JSON
workflows, watch, wait, cancel, safe resume, artifact pinning, and
reference-aware cleanup. Resume verifies source and artifact hashes. See
[Automation workflows](workflows.md).

## Structured changes

Property and classification edits use preview, host approval, commit, and
optional restore:

```python
with Workbench.open("tower.ifc") as wb:
    wall = wb.query("IfcWall")[0]
    preview = wb.preview_property_change(
        wall["global_id"],
        pset_name="Pset_WallCommon",
        property_name="FireRating",
        value="F60",
    )
    approval = wb.approve_change_set(
        preview.change_set_id, approved_by="bim-manager"
    )
    wb.set_mode("edit")
    wb.commit_change_set(
        preview.change_set_id, approval_id=approval.approval_id
    )
```

Preview does not change the model. Commit rechecks the revision, validates a
candidate, and creates a verified backup. Never expose approval, commit, or
restore as AI tools.

## Async and options

`AsyncWorkbench` uses the same method names as coroutines:

```python
from ifc_console import AsyncWorkbench

async with await AsyncWorkbench.create("tower.ifc") as wb:
    walls = await wb.query("IfcWall")
    report = await wb.validate()
```

Use `async with` or `await wb.aclose()` to finish supervised cleanup.

```python
Workbench.open(
    "tower.ifc",
    mode="ask",
    home=".ifc-console-ci",
    allowed_dirs=("models", "requirements"),
    settings={"sandbox.mode": "strict"},
    project_dir="path/to/project",
)
```

`project_dir` anchors deliberate project inputs such as `.ifc-console/`
settings, references, recipes, skills, and custom agents, and joins the allowed
paths. Private Agent threads remain under the user's IFC Console home, scoped
by a hash of this directory. It defaults to the working
directory. Settings here are session-only; change them later with
`wb.configure({...})` or `runtime.settings.get()`, `.set()`, and `.update()`.
