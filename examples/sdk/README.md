# SDK examples

Run the model report from the repository root:

```bash
uv run python examples/sdk/model_report.py model.ifc
```

The script opens no server or browser. It uses typed validation results and
prints stable JSON that a CI job or another application can consume.
