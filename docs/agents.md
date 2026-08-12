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
tools = await runtime.toolset(permitted_only=True)

review_tools = (
    tools
    .include(tags={"read", "analysis", "preview"})
    .exclude("open_ifc_file", "save_ifc_file", "execute_ifc_code")
)

result = await review_tools.call(
    "query_elements",
    {"query": "IfcWall"},
)
```

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

Implement `AgentModel` for another provider library and `ThreadStore` for your
database. `JsonThreadStore(path)` is suitable for small local applications;
protect its directory because transcripts can contain model data.

`agent.stream()` emits typed events for text, reasoning, tool calls, approvals,
usage, completion, and failure. Every event carries run and thread IDs.

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

`examples/sdk/agent_chat` demonstrates streaming, durable browser threads,
tool-call history, approvals, company functions, MCP, and the optional viewer:

```bash
python -m ifc_console.examples.agent_chat model.ifc \
  --provider openai --model YOUR_MODEL_ID
```

It is a reference, not a production identity system. Replace its local thread
store, browser approvals, and loopback-only hosting before deployment.
