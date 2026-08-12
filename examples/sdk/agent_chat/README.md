# Agent SDK reference chat

This example is a standalone company-style chat application built with
`LocalRuntime`, `Toolset`, `Agent`, `JsonThreadStore`, and `ProviderModel`. It is intentionally
separate from the product chat panel: copy it, replace its thread store and
approval handler, and add company Python or MCP tool sources.

From the repository root:

```bash
uv run python examples/sdk/agent_chat/app.py model.ifc \
  --provider openai --model YOUR_MODEL_ID
```

The same example is included in an installed wheel:

```bash
python -m ifc_console.examples.agent_chat model.ifc \
  --provider openai --model YOUR_MODEL_ID
```

The provider key comes from the same environment variables as IFC Console. For
an OpenAI-compatible local server:

```bash
uv run python examples/sdk/agent_chat/app.py model.ifc \
  --provider local --model local-model \
  --base-url http://localhost:8000/v1 --local-only
```

Add `--viewer` after installing `ifc-console[viewer]` to expose the existing 3D
viewer beside the reference application. The server binds only to loopback and
prints a tokenized URL. Provider credentials remain in Python and are never
sent to browser storage.

The example includes two company tools: a read-only submission profile and a
mock publishing action that requires an explicit browser approval. Replace
those functions with application services or add a namespaced `McpToolSource`.
