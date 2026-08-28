# SDK examples

Run these commands from the repository root.

`quickstart_agent.py` opens a model, selects six read tools, and streams the
provider-neutral `Agent` loop:

```bash
uv run python examples/sdk/quickstart_agent.py model.ifc --model MODEL_ID
```

`model_report.py` uses typed SDK results and prints stable JSON for scripts or
CI:

```bash
uv run python examples/sdk/model_report.py model.ifc
```

The full agent runtime ships in `ifc-console`. For its browser interface,
install `ifc-console[viewer]`, start `ifc-console`, then run `/agent`.
