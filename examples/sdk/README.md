# SDK examples

Start with the terminal quickstart agent. It opens a model, selects six read
tools, and streams a provider-neutral agent in one short file:

```bash
uv run python examples/sdk/quickstart_agent.py model.ifc --model MODEL_ID
```

Run the model report from the repository root:

```bash
uv run python examples/sdk/model_report.py model.ifc
```

The script opens no server or browser. It uses typed validation results and
prints stable JSON that a CI job or another application can consume.

For a complete agent application with scoped tools, streaming, threads,
approvals, company functions, and an optional viewer, see
[`agent_chat/README.md`](agent_chat/README.md).

For a focused LangChain workflow that resolves elements by name, GlobalId,
selector, or viewer selection and previews a company-controlled thickness
property, see [`property_agent/README.md`](property_agent/README.md). It is a
standalone project with its own dependencies and virtual environment; LangChain
is not installed by IFC Console.
