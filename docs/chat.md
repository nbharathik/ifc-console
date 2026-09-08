# Agent workspace

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

Then use the single Agent launcher:

| command | action |
| ------- | ------ |
| `/agent` | open General beside the shared 3D viewer |
| `/agent <name>` | open a named assistant |
| `/agent list` | list every built-in and custom assistant |
| `/agent new` | build a custom assistant |
| `/agent off` | disable the workspace and forget any in-memory key |

There is no separate `/chat` command. Conversation is part of the Agent
workspace. `/viewer` always opens the viewer alone; `/agent` attaches this
panel to the same viewer component and therefore has the same measurements,
sections, selections, camera controls, and screenshots.

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
references: press **Add files** or drop them onto the page. Files are listed
by collection; a collection folds, is granted to the assistant with one
checkbox, and is deleted or turned into a general skill from its row. Access
is either all content or an explicit selected set. The server enforces it for
retrieval, records, images, and rendered PDF pages.

Composer attachments are different: the paperclip and camera add evidence only
to the next message.

Three keys open the same list, narrowing as you type:

| key | offers |
| --- | ------ |
| `@` | workflows, the 3D selection, saved views, permitted project files |
| `#` | saved skills |
| `/` | panel commands, and everything above |

Picking a workflow attaches it to the conversation, exactly as choosing it from
the workflows panel does. Picking a file both names it in the message and grants
it to that message. Selecting elements in the viewer adds model-scoped
GlobalIds, so the assistant can resolve phrases such as "this wall" without
another selection round; a GlobalId picked through several meshes is listed
once.

While the session is in edit mode a bar above the conversation states how many
changes are waiting, which file a save would write, and offers **Save** and
**Download**. Edit mode works in a copy, so neither touches the file you opened.

Reference content lives under the console home, never in the project folder:
the library `~/.ifc-console/agents/references/` serves every project and is
the default for **Add files**, and `~/.ifc-console/agents/projects/<hash>/references/`
holds files added for one project. A subfolder is a collection, shown as a
group in the Content tab. Supported files are markdown, text, PDF, images,
and `.jsonl` or `.csv` tables, which are indexed row by row for search and
for `lookup_table_rows`. A PDF is indexed per page, its table-like lines
become rows too (a name, positional values `v0, v1, ...`, and the printed
header), and a digest record lists every page with its title and table
rows. Add files from the panel, with
`ifc-console agents files <paths> [--collection name]`, or by copying files
into those folders and pressing the refresh control (shift-click rebuilds the
index after an update). The Content page also searches the index the way an
agent does (**Test a search**), reads any file in place (**View**), and can
write a collection's general skill (**Make general skill**) from the files and
tables it holds. See
[Agent applications](agents.md#project-workspace).

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
which may contain IFC or project data. Use a local provider or leave the Agent workspace off
when that data must not leave the machine.

## Tools and safety

Tool access follows the current session policy. Two controls are independent:

| mode | Approval | Auto |
| ---- | -------- | ---- |
| Ask | read-only; pauses before protected calls | read-only; runs permitted calls without pausing |
| Edit | may change memory; pauses before protected calls | may change memory without pausing |

Entering Edit or Auto requires confirmation. A protected call waits as one
short row with Deny, Approve, and an **always** toggle that keeps the answer
for the rest of the conversation; open the row to see the operation,
capabilities, and arguments. Once the call has run, the decision is a small
mark on its tool card. Denial returns a tool result the model may handle.

Neither control lets the assistant write the file you opened. Edit mode copies
that file aside first, so the assistant can save its work into the copy and you
still have the original; changes stay in memory until you press **Save**, run
`/save`, or the assistant writes the copy. AI-generated property proposals
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
- Local history stores bounded browser transcripts and keeps server-side Agent
  threads in the user's private IFC Console home, scoped by a hash of the
  project path. Switching model, provider, instructions, or content access
  starts a compatible fresh context.
- **Delete all** removes conversation history, but keeps credentials and project
  references.

For viewer navigation and measurement controls, see [3D viewer](viewer.md).

## Workflows in the composer

Type `/` to open the command list. Saved workflows come first, then the
panel commands, then the saved skills of the active assistant. Choosing a
workflow attaches it to the conversation as a chip above the composer:

- Hover the chip, focus it, or press its name to read exactly what it adds.
  Before the first turn that is the workflow's system prompt, stages, and
  scope; afterwards it is the instructions the console actually sent.
- Send reads **Run** while the workflow waits to start. An empty composer
  sends the workflow's own task; anything typed is the prompt for this run.
- The chip stays pinned. Every later turn names the workflow again, so the
  console keeps one thread with the workflow's prompt in place. Removing the
  chip starts a fresh conversation without it.
- The empty state lists the first workflows as one-press starters, and the
  plus menu's **Run a workflow** opens the same list.

See [Agent workflows](agent-workflows.md) for what a workflow is and how the
chat path differs from a staged run.

## Memory

A memory pill under the composer reads what this page holds, what the console
process holds, and what the machine has left. It turns amber when any of
them is high and red when the machine is close to running out. Pressing it
releases what can be rebuilt: the viewer's parsed-model cache and idle parser
worker, and the full tool output kept on older turns, which keep their
previews. While a run is live the panel samples every few seconds and applies
the same relief on its own, at most once a minute, so a long run on a large
model does not push the machine into swapping.

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
| `chat.enabled_default` | `false` | enable the Agent workspace at session start |
| `chat.provider` | `openai` | initial provider |
| `chat.model` | empty | initial model ID |
| `chat.base_url` | empty | provider URL override |
| `chat.tools` | `true` | expose permitted tools |
| `chat.max_tool_rounds` | `8` | maximum tool rounds per answer |
| `chat.local_only` | `false` | allow only local provider URLs |
| `chat.timeout_s` | `300` | provider response timeout |

Use `ifc-console --agent` to open the workspace at startup.
