# SDK examples

Run these commands from the repository root.

`quickstart_agent.py` opens a model, selects six read tools, and streams the
provider-neutral `Agent` loop:

```bash
uv run --all-packages python examples/sdk/quickstart_agent.py model.ifc --model MODEL_ID
```

Installed applications need the optional product first:

```bash
pip install ifc-console-agents
# Add PDF/project-document support when the agent uses it:
pip install "ifc-console-agents[documents]"
```

The example imports the deterministic `LocalRuntime` from `ifc_console` and
agent types from the canonical `ifc_console_agents` namespace.

`model_report.py` uses typed SDK results and prints stable JSON for scripts or
CI:

```bash
uv run python examples/sdk/model_report.py model.ifc
```

The browser viewer already ships in `ifc-console` and needs no LLM. Install
`ifc-console-agents`, start `ifc-console`, then run `/agent` for the optional
agent panel registered through `ifc_console.extensions`.
