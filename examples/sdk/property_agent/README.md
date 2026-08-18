# LangChain IFC property agent

This is a standalone application built on the IFC Console SDK. LangChain and
the OpenAI integration belong to this example's environment; neither is an
`ifc-console` dependency.

The agent resolves elements by name, GlobalId, IFC selector, or optional 3D
selection, inspects them, and creates a company-controlled thickness preview.
It cannot approve or commit its own proposal.

## Create the example environment

From this directory, let uv create `.venv` and install the example's own
dependencies plus the editable checkout of IFC Console:

```bash
uv sync
```

Add the optional IFC viewer only when this application needs it:

```bash
uv sync --extra viewer
```

The equivalent pip workflow from this directory is:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e "../../.."
.venv/Scripts/python -m pip install langchain langchain-openai
```

On macOS or Linux, replace `.venv/Scripts/python` with `.venv/bin/python`.
Install `"../../..[viewer]"` instead of `"../../.."` when the viewer is needed.

## Run it

Run one request:

```bash
uv run python app.py ../../../model.ifc \
  --model openai:YOUR_MODEL_ID \
  --prompt "Set Wall-1 thickness to 200 mm"
```

Open the viewer and allow click selection:

```bash
uv run --extra viewer python app.py ../../../model.ifc \
  --model openai:YOUR_MODEL_ID --viewer
```

Click one or more elements, then ask: `Set the selected elements to 200 mm`.
Add `--apply` to receive a terminal confirmation after every preview. Approval
and commit run only after that host-side confirmation. The verified transaction
commit durably replaces the source IFC and creates a backup.

## How the workflow is composed

The application is deliberately a visible sequence in `app.py`:

1. Create `LocalRuntime` with lifecycle settings.
2. Open the IFC model.
3. Set `ask` mode and session-only operational settings.
4. Optionally enable the viewer.
5. Create the company property-preview tool.
6. Select each IFC tool the agent may call by exact name.
7. Convert that small `Toolset` for LangChain and create the agent.
8. Keep approval, mode switching, and commit in normal host code.

The main extension point is `build_property_source()`. Replace its fixed
property rule with company policy, a catalog lookup, validation, or a
deterministic calculation. IFC execution and LangChain orchestration stay
separate.
