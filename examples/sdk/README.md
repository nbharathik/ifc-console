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

For the complete browser agent experience, install `ifc-console[viewer]`, run
IFC Console, and open `/agent`. The production panel now lives in the main
package instead of being duplicated as an SDK example. Its implementation uses
the same public `Agent`, `Toolset`, `ProviderModel`, and runtime APIs shown by
the terminal quickstart.

For a focused LangChain workflow that resolves elements by name, GlobalId,
selector, or viewer selection and previews a company-controlled thickness
property, see [`property_agent/README.md`](property_agent/README.md). It is a
standalone project with its own dependencies and virtual environment; LangChain
is not installed by IFC Console.
