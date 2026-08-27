# Chat panel

The optional browser chat panel can use the same operations as an MCP client.
Open it beside the 3D model when you want one browser workspace for questions,
tool calls, and visual results.

The panel is off by default. It is the only ifc-console feature that may contact
an external LLM provider.

## Open the panel

Install the viewer bundle first:

```bash
pip install "ifc-console[viewer]"
```

Then use:

```text
/chat          open chat beside the 3D viewer
/chat solo     open chat without the 3D canvas
/chat off      close chat and forget any in-memory key
```

In split view, press ++c++ to show or hide the panel and drag the divider to
resize it.

## Choose a provider

| provider | credential | notes |
| -------- | ---------- | ----- |
| OpenAI | `OPENAI_API_KEY` | choose a model available to the account |
| Anthropic | `ANTHROPIC_API_KEY` | native Claude tool use |
| OpenRouter | `OPENROUTER_API_KEY` | one endpoint for many model providers |
| Local | none by default | OpenAI-compatible vLLM, LM Studio, Ollama, or similar |

Choose the provider and model in **Agent workspace > Models**. Open the same
workspace from the right side of the chat header or from **Agent workspace**
at the bottom of the sidebar. The adjacent model control and the model pill in
the composer open **Models** directly. Use **Custom ID** when the model does
not appear in the fetched list.
`/chat anthropic` can set the provider from the terminal.

For a local provider, set its base URL, for example
`http://localhost:8000/v1`. Enable `chat.local_only=true` to reject any provider
URL outside the local machine.

## Agents in the panel

The sidebar lists every assistant. **General assistant** holds every capability
block and is where the panel starts; **Measurement**, **Documents**, and
**Model review** are the same machinery with a narrower block set and a sharper
prompt. **Plain chat** is the bare tool loop with no agent around it, listed
like any other so it is one click away. Python hosts can still register packs
explicitly; arbitrary extension discovery is not enabled. An agent keeps its
own conversation, starter prompts, and workflow: the measurement agent resolves
recipes, reads reference images, cites pages, and can prepare a safe ChangeSet
proposal; the documents agent answers from the ingested corpus.

All assistant configuration lives in one **Agent workspace**:

- **Agents** selects an assistant and shows its concise overview, suggested
  questions, safety limits, and standing instructions. **New assistant** and
  **Edit agent** open the compact setup flow in the same workspace.
- **Capabilities** explains each agent block and its tools. The assistant's
  pipeline lives in its own overview under **How this assistant works**,
  because which stages it can reach follows from the blocks it holds.
- **Tools** starts as a compact, filterable name list. The filter matches a
  tool's name and its description, so "pset" finds every tool that reads
  property sets. Open a row to inspect its full description, safety metadata,
  and argument schema; a row builds that detail the first time it is opened.
- **Content** is the shared project library. Upload manuals, specifications,
  drawings, photographs, and other supported references once, search them,
  then choose the standing files each assistant may use. Selection is bulk
  first: **Select shown** and **Clear shown** act on whatever the filter is
  showing, and shift-click extends from the last file you touched, so granting
  a folder of manuals is one gesture.
- **Models** contains provider, model, API key, tool use, and advanced model
  options. **App** contains local history, theme, and system health.

The assistant identity in the header remains a compact status readout. Agent
workspace opens as a bounded modal centred on the window, dismissed with its
close control, Escape, or a click outside it. Agent setup is the exception: an
unsaved draft is not thrown away by a stray click. Its one left rail replaces
nested top tabs.

An assistant's overview states its reach as a row of marks rather than a wall
of labels: tools, project content, viewer link, write policy, tool rounds, and
timeout, each naming itself on hover and to a screen reader.

Content access is per assistant and enforced by the server. **All content**
preserves the existing behavior; clearing it creates an explicit selected set,
which may also be empty. Adding a new library file does not silently grant it
to an assistant that uses a selected set. The agent's Files detail shows only
what that assistant can reach. PDF text extraction and visual page rendering
ship in the base package, and the library separates images from documents.

The paperclip in the composer is different: it adds a file only to the next
message without changing standing access or adding it to the shared library.
The camera button adds the current 3D view as the same kind of turn-only visual
evidence. The selection chip reports whether tools will receive the whole model
or the elements currently selected in the viewer.

Element references in answers are live. Any IFC GlobalId an assistant writes
renders as a chip: docked beside the viewer, clicking it selects and frames
that element in 3D; on the standalone chat page it copies the id. An answer
that names elements can also show them.

Agents start each run already oriented. The composed prompt carries a session
context section (the open model, the mode, and the project's saved skills), so
the first round is never spent listing what the host already knows, and every
panel message that is sent while elements are selected in the 3D viewer
carries those GlobalIds along, so "this wall" resolves without a
get_viewer_selection round. The tools stay available for anything richer.

Use `ifc-console agents files <paths>`, copy supported files into
`.ifc-console/agents/references/`, or use `/agent files` to list or refresh the
local references. `/agent` in the terminal picks an assistant, `/agent
measurement` opens one directly, and `/agent new` starts setup. When local
history is enabled, agent threads are stored as bounded, atomic records under
`.ifc-console/agents/threads/`, allowing the selected conversation to resume
after the console restarts. Switching provider, model, standing instructions,
or content access starts a compatible fresh context instead of loading history
created under different permissions.

## Credentials and model data

A key is found in this order: pasted in the panel, stored in the system
keyring (`ifc-console keys set openai`), or read from the provider's
environment variable. Keyring support ships with the base package. Select
**Save in the operating-system credential store** to store a pasted key
securely from the panel. Stored keys never enter browser or project storage;
`ifc-console keys list` and `keys delete` manage them. Without that explicit
choice, a pasted key is held in console memory only until `/chat off` or exit.

The browser never contacts the provider directly. ifc-console sends the
provider:

- your messages;
- the chat system instructions;
- tool results used to answer, which may contain IFC model data.

Use a local provider or keep the chat panel disabled for confidential models
that must not leave the machine.

## Tools and safety

With tools enabled, the assistant can query elements, inspect properties,
validate models, calculate quantities, search the knowledge index, and use any
other operation permitted by the current session.

Two independent controls sit in the composer, because they answer two
different questions:

| | **Approval** | **Auto** |
| --- | --- | --- |
| **Ask** | Read-only. Stops before running code or writing an artifact. | Read-only. Runs its tools and code without stopping. |
| **Edit** | May change the model in memory. Stops before every protected call. | May change the model in memory without stopping. |

Neither of them lets an assistant write the IFC file. Every change lives in
memory until **you** press **Save** in the composer, which appears only while
there is something unsaved. `/save` does the same from the message box.

Entering edit mode, and turning autonomy to auto, are each confirmed
explicitly.

While autonomy is **Approval**, a protected call pauses the run and puts a card
in the transcript naming the tool, the capabilities it needs, and the arguments
the model chose. **Approve** lets it through; **Deny** returns a refusal the
model can work around, and the run continues either way. Reading the model is
never held up for a decision: only generated code, artifact writes, and model
writes are.

The same controls apply as with MCP:

- `ask` mode blocks model changes;
- each tool call appears where it ran, between the sentence that led to it and
  the sentence that follows, and opens to show the arguments the model chose
  and what the console returned;
- a failed call opens itself and prints the console's own message and hint;
- every call is audited with provider and model metadata, never the key.

The measurement agent may create a revision-bound ChangeSet preview in
`IfcConsole_AI_Measurements`. The name permanently marks committed values as
AI-assisted. A preview changes neither the live model nor the IFC
file. Approval and commit remain explicit SDK/CLI host actions.

Turn `chat.tools` off when you want ordinary conversation without model tools.

## Panel controls

The sidebar toggle opens assistants and conversations. One **Agent workspace**
control in the header and one matching item at the bottom of the sidebar open
the same configuration surface. Closing it returns to the unchanged chat.

- Enter sends; Shift+Enter adds a line.
- Escape closes whatever is on top, then stops a running answer.
- **Stop** cancels the current provider response.
- **New chat** archives the current conversation when history is on and always
  starts with a new context.
- The sidebar reopens or deletes previous local chats, grouped by recency.
  A row's delete control arms on the first click and acts on the second, so a
  conversation or a custom assistant is never lost to one stray click beside
  the control that opens it. Escape, or clicking anything else, cancels it.
- **Export** downloads the selected conversation as Markdown.
- **Content > Add files** uploads and indexes shared project references.
- **+ > Attach a file** attaches content only to the next message without
  granting persistent access. The chip appears while the file is still being
  indexed and send waits for it, so a slow PDF cannot be sent half-attached.
- **New assistant** and **Edit agent** keep setup inside Agent workspace.
  Adaptive, evidence-first, and fast-scan strategies can be combined with
  explicit round and tool-call budgets under **Advanced run controls**.
- **Standing instructions**, under an agent's Instructions detail, are saved
  per assistant and augment, but cannot replace, its safety and approval rules.
- The bottom composer row selects the AI model, the IFC model when several
  are open, and Ask/Edit mode. Everything that belongs to one message sits
  behind the **+** control: attach a file, attach the current 3D view, mention
  project content, or frame the 3D selection.
- Type **@** in the message to name a project document. Accepting a suggestion
  both writes the name into the prompt and attaches the file to that message.
- The assistant can drive the 3D view as well as read it: isolate the elements
  it is talking about, put the camera on a named view, measure an element's
  size, and cast a clearance laser along each axis from a point or an element.
  The same three measurements are in **View tools > Measure**, so a number in
  an answer can be checked by hand without leaving the model.
  Every result it produces appears in the viewer's own measurement list, so the
  number in the answer is the number on screen.
- Type **/** at the start of a message for panel commands: `/agent`, `/model`,
  `/content`, `/tools`, `/pipeline`, `/new`, `/export`, `/ask`, `/edit`.
- Selecting elements in the 3D view adds them to the composer as a context
  chip beside any attached files. Click the chip's name to frame them again,
  or its close control to clear the selection. Nothing is shown when nothing
  is selected: the whole model is always available to the tools.
- **Keep conversation history locally** controls browser transcript storage and
  durable project-local agent context. Changing it starts a fresh conversation.
- **Delete all** in **App** asks for confirmation, then cancels any active
  panel run and removes every browser transcript and project-local assistant
  thread, including orphaned panel threads. Provider credentials and project
  reference files are not conversation history and are left alone.
- Answers show their tool calls in place, token counts, latency, and copy
  controls. A turn is marked by one icon rather than a repeated name and role
  word; the assistant's name is on the mark's tooltip and accessible name.

The active assistant stays in the header; model, IFC file, selection context,
and safety mode stay beside the composer. Everything else about the assistant
is in Agent workspace. While a run is working, one line in the message says
whether it is finding elements, reading documents, measuring, checking, or
preparing a proposal.

Browser history is scoped by an opaque project identifier, the open model, and
its fingerprint. Switching projects or models therefore starts a separate
conversation list. Changing the provider, AI model, base URL, tool access,
standing instructions, or standing content selection also forks a fresh
context instead of silently reusing messages created under the old
configuration.

## Python

The SDK exposes the same provider-neutral loop:

```python
from ifc_console import Workbench

with Workbench.open("tower.ifc") as wb:
    answer = wb.ask(
        "Which walls are missing a fire rating?",
        provider="anthropic",
        model="YOUR_MODEL_ID",
    )
    print(answer["text"])
    print(answer["tool_calls"])
```

Pass `on_event=print` to stream events. Without `api_key=`, the provider key
comes from the environment. See [Python SDK](sdk.md).

## Settings

| key | default | purpose |
| --- | ------- | ------- |
| `chat.enabled_default` | `false` | open chat at session start |
| `chat.provider` | `openai` | initial provider |
| `chat.model` | empty | initial model ID |
| `chat.base_url` | empty | provider URL override |
| `chat.tools` | `true` | expose permitted ifc-console tools |
| `chat.max_tool_rounds` | `8` | maximum tool rounds per answer |
| `chat.local_only` | `false` | allow only local provider URLs |
| `chat.timeout_s` | `300` | provider response timeout |

Use `ifc-console --chat` to enable the panel at startup.
