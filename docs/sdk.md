# Python SDK

Use the SDK to put IFC Console inside your own LLM agent, chat panel, product
feature, script, notebook, or CI job. IFC Console supplies model sessions,
settings, ask/edit policy, tools, transactions, MCP, and the optional viewer.
Your application chooses the LLM framework and orchestration.

Install the framework-neutral SDK by itself. Add the viewer only to applications
that display it:

```bash
pip install ifc-console
pip install "ifc-console[viewer]"
```

LangChain remains application-owned. LangGraph orchestration is available only
through the optional `graph` extra; a normal install stays framework-neutral.

| interface | use it for |
| --------- | ---------- |
| `LocalRuntime` | embedded agent tools over a local model |
| `ConsoleRuntime` | agent tools connected to a running console |
| `Agent` | a bounded provider-neutral tool loop |
| `Toolset` | scoped IFC, Python, and MCP tools |
| `FunctionToolSource` | trusted application functions exposed as tools |
| `RuntimeSettings` | validated, session-only operational settings |
| `Workbench` | synchronous scripts and CI |
| `AsyncWorkbench` | the same API inside async applications |

For an agent, start with `LocalRuntime`. For deterministic scripting, start
with `Workbench`. The runnable
[quickstart agent](https://github.com/nbharathik/ifc-console/blob/main/examples/sdk/quickstart_agent.py)
shows the whole agent path in one short file.

## The modular agent workflow

Keep construction as a visible sequence. This example opens an IFC, sets the
host-owned policy, changes operational settings, selects individual tools, and
then hands that `Toolset` to the bundled provider-neutral agent:

```python
from ifc_console import Agent, FunctionToolSource, LocalRuntime, ProviderModel

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

`runtime.tools()` accepts exact names or globs. `runtime.toolset()` remains
available for profile-, tag-, or capability-based selection. Mode changes,
settings, credentials, ChangeSet approval, and commit belong in host code and
must not be exposed to the model as tools.

`RuntimeSettings` changes the current embedded session without editing the user
settings file. Operational settings are read by later tool calls. Lifecycle
settings such as `server.port` must be passed to `LocalRuntime.open(settings=...)`
because they are needed while the web surface is created.

`ConsoleRuntime` provides the same tool-facing interface over MCP when the model
must remain in a user's running IFC Console. The remote console owner retains
its mode and settings controls.

## LangChain and LangGraph

An application that already uses LangChain can install it in its own project.
The lazy adapter keeps IFC execution and policy in IFC Console while LangChain
owns the model, graph, memory, middleware, and streaming:

```python
from langchain.agents import create_agent
from ifc_console import LocalRuntime

async with await LocalRuntime.open("tower.ifc") as runtime:
    toolset = await runtime.tools(
        "get_ifc_project_info",
        "search_elements",
        "get_element",
        "get_psets",
    )
    agent = create_agent(
        model="openai:YOUR_MODEL_ID",
        tools=toolset.as_langchain_tools(),
        system_prompt="Use IFC tools before making model claims.",
    )
    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": "Inspect Wall-1"}]
    })
```

Put `langchain` and the integration for your model provider in the agent
application's `pyproject.toml`; do not add them to IFC Console. Use LangChain's
async `ainvoke`/`astream` APIs so the model session and tools stay on one event
loop. Structured IFC envelopes are retained as tool-message artifacts.

For a checkpointed outer workflow without replacing the built-in agent loop,
install the graph extra:

```bash
pip install "ifc-console[graph]"
```

`create_langgraph_workflow()` receives a synchronous builder callback. Graph
nodes return `graph_update()` values, and the adapter emits the same
`AgentEvent` stream as the built-in agent:

```python
from ifc_console.integrations.langgraph import (
    create_langgraph_workflow,
    graph_update,
)

async def inspect(state):
    result = await reviewer.run(state["prompt"])
    return graph_update(final_text=result.text)

def configure(builder, start, end):
    builder.add_node("inspect", inspect)
    builder.add_edge(start, "inspect")
    builder.add_edge("inspect", end)

workflow = create_langgraph_workflow(configure, checkpointer=checkpointer)
async for event in workflow.stream("Inspect the selected walls", thread_id="review-1"):
    render(event)
```

The caller owns the checkpointer lifecycle. Use an in-memory saver only for
tests, an async SQLite saver for a local single-user application, and a hosted
database saver for multi-user deployments. Keep runtimes, credentials, IFC
handles, and inline image bytes outside checkpoint state; store artifact and
change-set references instead. Approval interrupts use
`graph_approval_interrupt()` and resume through `workflow.resume()` on the same
thread ID. LangGraph's async interrupt context requires Python 3.11 or newer;
other async graph workflows remain available on Python 3.10. Host policy and
revision checks still authorize the eventual tool call or commit.

## A focused property agent

The standalone [`property_agent` example](https://github.com/nbharathik/ifc-console/tree/main/examples/sdk/property_agent)
has its own `pyproject.toml` and virtual environment. It shows the intended
company-feature pattern:

1. `search_elements` resolves a name, partial name, GlobalId, or simple selector.
2. `get_viewer_selection` resolves what a user clicked in the 3D viewer.
3. Read tools inspect the targets and the model length unit.
4. A company Python tool fixes the allowed property set, property name, IFC value
   type, and missing-property policy in host code.
5. The LLM creates only a revision-bound preview.
6. Host code displays the diff and owns the durable transaction commit.

This is safer and easier to optimize than giving every specialist agent the
full IFC tool catalog or free-form code execution.

## Embed the viewer, MCP, and a custom panel

`LocalRuntime.build_web_app()` returns the authenticated ASGI surface used by
IFC Console. Add your own Starlette routes and serve it with Uvicorn:

```python
runtime.enable_viewer()  # do this before building tools that need selection
agent = await runtime.create_agent(...)
surface = runtime.build_web_app(extra_routes=my_chat_routes(agent))

print(surface.viewer_url)
print(surface.browser_url("/my-chat"))
server = uvicorn.Server(uvicorn.Config(surface.app, host="127.0.0.1", port=8765))
await server.serve()
```

The surface contains the same MCP endpoint, viewer routes, selection bridge,
and token gate as the console. Your application owns the server lifecycle,
identity, LLM credentials, and custom UI.

## Script quick start

```python
from ifc_console import Workbench

with Workbench.open("tower.ifc") as wb:
    print(wb.info()["project"]["name"])
    walls = wb.query("IfcWall")
    report = wb.validate()
    print(len(walls), report["valid"])
```

The context manager closes model and worker resources automatically.

## Common operations

| method | result |
| ------ | ------ |
| `orient()` | status, project summary, and shallow spatial tree |
| `info()` | schema, units, counts, and materials |
| `tree(depth=10)` | spatial containment tree |
| `search(term, limit=50)` | name, GlobalId, text, or simple-selector matches |
| `query(selector, limit=50)` | selector result rows |
| `element(global_ids)` | attributes, properties, type, and container |
| `psets(global_ids)` | property and quantity sets |
| `quantities(selector, by="storey")` | aggregated stored quantities |
| `validate()` / `validate_ids(path)` | schema or IDS results |
| `clashes(set_a, set_b)` | overlap or clearance results |
| `georeferencing()` | CRS and map conversion |
| `schema_docs(...)` | IFC entity, pset, or property documentation |

Search the offline reference with `search_knowledge()`,
`knowledge_record()`, and `api_docs()`. Index the project's own documents
with `ingest_docs(paths)` (markdown, text, PDF via the `[pdf]` extra), list
them with `project_documents()`, load indexed image pixels with
`project_reference_image(path)`, and search them with
`search_knowledge(..., corpus="project")`; hits carry document and page
provenance. The measurement tools, `get_element_geometry`,
`measure_elements`, `measure_distance`, and `get_measurement_recipe`, run
through `call()` and return file units and SI side by side.
`get_viewer_screenshot` returns its image as base64 in `data.images`, so SDK
callers and the bundled Agent can consume it like any other envelope.

## Ask and edit modes

`Workbench.open()` starts in `ask` mode. Reads work; mutations do not.

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

In a script, your code owns `set_mode()`. In an agent application, never expose
mode changes, allowed directories, or approval methods as model-callable tools.

## Errors

Convenience methods raise `IfcConsoleError` with the same code and hint an MCP
client receives:

```python
from ifc_console import IfcConsoleError

try:
    wb.query("IfcWall, ((broken")
except IfcConsoleError as exc:
    print(exc.code, exc.hint)
```

Low-level `call()` returns the normal `{ok, data/error, meta}` envelope instead.

## More than one model

```python
with Workbench.open("architecture.ifc") as wb:
    wb.attach("structure.ifc")
    wb.attach("mep.ifc")
    hits = wb.clashes(
        "IfcWall",
        "IfcDuctSegment",
        other_model="mep",
        tolerance=0.02,
    )
```

One model is active and writable. Attached models are read-only.

## Tool bindings

`tools()` returns provider-neutral JSON Schema definitions. `call()` runs one
operation by name.

```python
tools = wb.tools(permitted_only=True)
result = wb.call("query_elements", query="IfcDoor", limit=10)
```

Use `permitted_only=True` so an AI sees only operations allowed by the current
profile. Definitions also include required capabilities and current permission
state.

For typed results, use `query_result()`, `validation_result()`,
`call_result()`, and `operation_definitions()`. `wb.context` provides immutable
workspace, model, revision, and source-hash information. Read it again after a
write.

The package includes a `py.typed` marker. Public contracts are exported from
`ifc_console` for IDEs, Pyright, and mypy.

## Durable automation

Long work can run in restricted workers and produce checksum-verified artifacts:

```python
with Workbench.open("tower.ifc") as wb:
    job = wb.submit_validation_job(ids_paths=("requirements.ids",))
    completed = wb.wait_job(job.job_id)
    for artifact in completed.artifacts:
        wb.export_artifact(artifact.artifact_id, artifact.name)
```

The SDK also supports:

- validation and query batches across many IFC files;
- versioned JSON or YAML workflows;
- watch, wait, cancel, and safe resume;
- artifact pinning and reference-aware cleanup.

Resume verifies source hashes and previous artifacts before reusing work.
Validation jobs require a clean model because workers read the file from disk.
See [Automation workflows](workflows.md).

## Structured changes

Property and classification edits use a preview, approval, commit, and optional
restore flow:

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
        preview.change_set_id,
        approved_by="bim-manager",
    )
    wb.set_mode("edit")
    commit = wb.commit_change_set(
        preview.change_set_id,
        approval_id=approval.approval_id,
    )
```

Preview does not change the model. Commit rechecks the revision, validates a
candidate, and creates a verified backup. Approval, commit, and restore are
direct caller methods and must not be added to an AI tool list.

## Async

`AsyncWorkbench` uses the same method names as coroutines:

```python
from ifc_console import AsyncWorkbench

async with await AsyncWorkbench.create("tower.ifc") as wb:
    walls = await wb.query("IfcWall")
    report = await wb.validate()
```

Use `async with` or `await wb.aclose()` so supervised cleanup finishes before
the model closes.

## Agent applications

Use `LocalRuntime` or `ConsoleRuntime` when a model should choose tools. They
provide exact or profile-based selection, custom Python and MCP sources, lazy
framework projections, limits, middleware, threads, host-owned approvals, and
the embeddable viewer/web surface. See [Building agent applications](agents.md).

## Options

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

`project_dir` anchors the project folder convention: its `.ifc-console/`
directory supplies project settings, the ingested document index, built-in
agent references, and measurement recipes, and the directory joins the allowed
paths. It defaults to the working directory.

Settings passed here apply only to this workbench and do not edit the user file.
They can also be changed later with `wb.configure({...})`. In an async agent
application, use `runtime.settings.get()`, `.set()`, or `.update()`.
