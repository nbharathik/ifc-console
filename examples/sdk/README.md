# SDK examples

Run these commands from the repository root.

`quickstart_agent.py` opens a model, selects six read tools, and streams the
provider-neutral `Agent` loop:

```bash
uv run python examples/sdk/quickstart_agent.py model.ifc --model MODEL_ID
```

The agent loop ships in the base install (`pip install ifc-console`); the
`[agents]` extra only adds ACP engines, keyring storage, and PDF support.

The example imports the deterministic `LocalRuntime` from `ifc_console` and
agent types from the `ifc_console.agents` namespace.

`model_report.py` uses typed SDK results and prints stable JSON for scripts or
CI:

```bash
uv run python examples/sdk/model_report.py model.ifc
```

The browser viewer ships in `ifc-console` and needs no LLM. Start
`ifc-console`, then run `/agent` for the Agent panel.
