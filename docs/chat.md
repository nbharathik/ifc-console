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

Choose the provider and model from panel settings. Use **Custom ID** when the
model does not appear in the fetched list. `/chat anthropic` can set the
provider from the terminal.

For a local provider, set its base URL, for example
`http://localhost:8000/v1`. Enable `chat.local_only=true` to reject any provider
URL outside the local machine.

## Agents in the panel

A selector beside the panel title offers plain **Chat** or one of the
agents. **Measurement** and **Documents** ship with ifc-console and are
there out of the box. The block button or `/agent new` builds project-local
agents from reviewed capability blocks. Python hosts can still register packs
explicitly; arbitrary extension discovery is not enabled. An agent keeps its
own conversation, starter prompts, and workflow: the
measurement agent resolves recipes, reads reference images, cites pages, and
can prepare a safe ChangeSet proposal; the documents agent answers from the
ingested corpus.

Both agents show the same project reference ledger. Use **Add files**, run
`ifc-console agents files <paths>`, or copy supported files directly into
`.ifc-console/agents/references/`. Documents are indexed for retrieval; images
are stored locally and can ride directly with the next model message. PDF text
extraction and visual page rendering ship in the base package. The ledger shows
whether each file is indexed. `/agent` in the terminal picks one with the arrow
keys; `/agent measurement` opens that agent directly, `/agent new` opens the
builder, and `/agent files` lists or refreshes the
local references. When local history is enabled, agent threads are stored as
bounded, atomic records under `.ifc-console/agents/threads/`, allowing the
selected conversation to resume after the console restarts. Switching provider
or model starts a compatible agent runtime while preserving the selected local
conversation.

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

The same controls apply as with MCP:

- `ask` mode blocks model changes;
- only the terminal user can switch to `edit`;
- each tool call and result appears below the answer;
- every call is audited with provider and model metadata, never the key.

The measurement agent may create a revision-bound ChangeSet preview in
`IfcConsole_AI_Measurements`. The name permanently marks committed values as
AI-assisted. A preview changes neither the live model nor the IFC
file. Approval and commit remain explicit SDK/CLI host actions.

Turn `chat.tools` off when you want ordinary conversation without model tools.

## Panel controls

- Enter sends; Shift+Enter adds a line.
- Escape closes settings or stops a running answer.
- **Stop** cancels the current provider response.
- **New chat** archives the current conversation locally and starts another.
- **Conversation history** reopens or deletes previous local chats.
- **Export** downloads the selected conversation as Markdown.
- **Add files** indexes references and attaches them to the next agent turn.
- The block button opens the custom-agent builder.
- **Additional instructions** are saved per assistant and augment, but cannot
  replace, its built-in safety and approval rules.
- **Keep conversation history locally** controls browser transcript storage and
  durable project-local agent context. Clearing history removes both.
- Answers show tool calls, token counts, latency, and copy controls.

Provider, model, IFC file, evidence count, capability blocks, and safety mode
are visible in the context map above the conversation. Live workflow markers
show whether an agent is gathering evidence, using tools, verifying results, or
preparing a proposal.

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
