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

## Credentials and model data

If the normal environment variable is set, the console reads the key from its
own environment. Otherwise, you can enter a key in the panel. The browser sends
it directly to the running console and does not store a copy. The console keeps
it only in memory until `/chat off` or exit.

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

Turn `chat.tools` off when you want ordinary conversation without model tools.

## Panel controls

- Enter sends; Shift+Enter adds a line.
- Escape closes settings or stops a running answer.
- **Stop** cancels the current provider response.
- **New chat** clears the conversation in that browser tab.
- Reloading keeps the tab's conversation; closing the tab drops it.
- Answers show tool calls, token counts, latency, and copy controls.

Provider, model, and mode are always visible above the conversation.

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
