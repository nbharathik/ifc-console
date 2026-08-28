# Browser chat

The chat runtime, providers, built-in/custom packs, and Agent browser panel ship
in the optional `ifc-console-agents` distribution. It registers through
`ifc_console.extensions`; core does not import it directly. The panel is off by
default and is the only IFC Console feature that may contact an external LLM
provider. The bundled IFC viewer remains fully usable without this package or
an LLM.

The extension contributes its routes and panel declaration at startup. Its
browser JavaScript and CSS are loaded lazily only when the installed panel is
opened.

## Open the panel

```bash
pip install ifc-console-agents

# Existing uv tool installation:
uv tool install --with ifc-console-agents ifc-console
ifc-console
```

Then use:

| command | action |
| ------- | ------ |
| `/chat` | open chat beside the 3D viewer |
| `/chat solo` | open chat without the canvas |
| `/chat <provider>` | open chat and select a provider |
| `/chat off` | close chat and forget any in-memory key |
| `/agent` | open the General assistant |
| `/agent list` | list every built-in and custom assistant |

stdio has no browser surface. Use the interactive console or `--no-tui`.

## Providers and models

| provider | default credential | notes |
| -------- | ------------------ | ----- |
| OpenAI | `OPENAI_API_KEY` | models available to the account |
| Anthropic | `ANTHROPIC_API_KEY` | native Claude tool use |
| OpenRouter | `OPENROUTER_API_KEY` | models from several providers |
| Local | none | OpenAI-compatible local servers |

Choose the provider and model in **Agent workspace > Models**. Use **Custom
ID** for a model not returned by the provider. For a local server, set its base
URL, such as `http://localhost:8000/v1`, and enable `chat.local_only=true` to
reject non-local provider URLs.

Tool calling and image input each support **Auto-detect**, **Supported**, and
**Not supported**. Mark unsupported features explicitly for text-only or
non-tool models. The console then omits incompatible schemas or image blocks
and keeps ordinary text chat available.

## Assistants and content

The panel includes:

| assistant | purpose |
| --------- | ------- |
| General | full IFC, document, measurement, review, proposal, and code surface |
| Measurement | cited, recipe-driven measurements |
| Documents | answers from indexed project references; PDFs require `[documents]` |
| Model review | schema, IDS, clashes, quantities, and model health |
| Plain chat | the permitted tool loop without a preset prompt |

Each assistant has its own conversation, instructions, limits, capability
blocks, and standing content access. Create or edit one in **Agent workspace >
Agents** or with `/agent new`. Custom assistants choose reviewed blocks; they
cannot widen policy or approve their own changes.

Use **Content** to add manuals, drawings, photographs, and other supported
references. Access is either all project content or an explicit selected set.
The server enforces it for retrieval, records, images, and rendered PDF pages.

Composer attachments are different: the paperclip and camera add evidence only
to the next message. Typing `@` attaches a permitted project file to that
message. Selecting elements in the viewer adds model-scoped GlobalIds, so the
assistant can resolve phrases such as "this wall" without another selection
round.

Project references live under `.ifc-console/agents/references/`. Add them from
the panel, with `ifc-console agents files <paths>`, or by copying supported
files there. See [Agent applications](agents.md#project-workspace).

## Credentials and privacy

A provider key is resolved in this order:

1. key pasted into the panel;
2. operating-system keyring entry from `ifc-console keys set <provider>`;
3. provider environment variable.

A pasted key stays in memory unless you explicitly save it to the system
credential store. Stored and environment keys are never exposed to browser
JavaScript. `ifc-console keys list` and `ifc-console keys delete <provider>`
manage saved entries.

The browser talks only to the local console. The console sends the selected
provider your messages, system instructions, image inputs, and tool results,
which may contain IFC or project data. Use a local provider or leave chat off
when that data must not leave the machine.

## Tools and safety

Tool access follows the current session policy. Two controls are independent:

| mode | Approval | Auto |
| ---- | -------- | ---- |
| Ask | read-only; pauses before protected calls | read-only; runs permitted calls without pausing |
| Edit | may change memory; pauses before protected calls | may change memory without pausing |

Entering Edit or Auto requires confirmation. Approval cards show the operation,
capabilities, and arguments. Denial returns a tool result the model may handle.

Neither control lets the assistant write the IFC file. Changes remain in
memory until you press **Save** or run `/save`. AI-generated property proposals
remain revision-bound ChangeSet previews until host code approves and commits
them. Values and per-property provenance use the reserved `IfcConsole_AI_`
namespace.

Every operation appears in the transcript with its arguments, result, and
error hint. Audit records include provider and model metadata, never the key.
Turn `chat.tools` off for conversation without IFC tools.

## Main controls

- Enter sends; Shift+Enter adds a line; Stop cancels the current response.
- New chat starts a fresh context. Export downloads the conversation as Markdown.
- GlobalIds in answers select and frame the corresponding viewer element.
- `/agent`, `/model`, `/content`, `/tools`, `/pipeline`, `/new`, `/export`,
  `/ask`, and `/edit` are available at the start of a message.
- `control_viewer` lets assistants focus, select, isolate, section, measure,
  and capture the same viewport the user sees.
- Local history stores bounded browser transcripts and agent threads under the
  project. Switching model, provider, instructions, or content access starts a
  compatible fresh context.
- **Delete all** removes conversation history, but keeps credentials and project
  references.

For viewer navigation and measurement controls, see [3D viewer](viewer.md).

## Python

The optional SDK exposes the same provider-neutral loop. Agent types use the
canonical `ifc_console_agents` namespace; deterministic runtimes remain in
`ifc_console`:

```python
from ifc_console import LocalRuntime
from ifc_console_agents import Agent, ProviderModel

async with await LocalRuntime.open("tower.ifc") as runtime:
    tools = await runtime.tools("query_elements", "get_element", "get_psets")
    agent = Agent(
        name="fire-review",
        model=ProviderModel(provider="anthropic", model="YOUR_MODEL_ID"),
        tools=tools,
        instructions="Use IFC tools for every factual model claim.",
    )
    answer = await agent.run("Which walls are missing a fire rating?")
    print(answer.text)
```

Use `agent.stream()` to consume typed events. Provider keys come from the
environment or configured credential source. See [Python SDK](sdk.md).

## Settings

| key | default | purpose |
| --- | ------- | ------- |
| `chat.enabled_default` | `false` | open chat at session start |
| `chat.provider` | `openai` | initial provider |
| `chat.model` | empty | initial model ID |
| `chat.base_url` | empty | provider URL override |
| `chat.tools` | `true` | expose permitted tools |
| `chat.max_tool_rounds` | `8` | maximum tool rounds per answer |
| `chat.local_only` | `false` | allow only local provider URLs |
| `chat.timeout_s` | `300` | provider response timeout |

Use `ifc-console --chat` to open the panel at startup.
