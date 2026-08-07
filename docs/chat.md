# Chat panel

A chat panel docked beside the 3D view that can drive the ifc-console tools.
It exists so you can try the whole thing without wiring up an MCP client, and
so you can see the model while you ask about it: what the assistant selects,
highlights or hides shows up in the view next to it.

It is **off by default** and it is the only part of ifc-console that talks to
the internet.

```
/chat          the 3D view with the chat panel beside it
/chat solo     the panel on its own page, no 3D view
/chat off      turn it off (any key held for this run is dropped)
```

`/chat` turns the viewer on as well. In the viewer the dock has its own
button in the toolbar, `C` toggles it, and the divider between them drags.

It ships with the viewer extra:

```bash
uv tool install "ifc-console[viewer]"
```

## Providers

| provider | what you need | notes |
| -------- | ------------- | ----- |
| OpenAI | `OPENAI_API_KEY` | models are listed from your account |
| Claude (Anthropic) | `ANTHROPIC_API_KEY` | native tool use |
| OpenRouter | `OPENROUTER_API_KEY` | one key, most models |
| Local (vLLM, LM Studio, Ollama) | nothing | any OpenAI-compatible server; nothing leaves your machine |

Pick a provider in the panel's settings and it lists the models that key can
reach; choose one from the list, or "Custom id..." for a name it does not
know. `/chat anthropic` sets the provider from the terminal.

The key comes from the environment variable when one is set. If not, you can
paste one into the panel: it goes straight to the running console, which holds
it in memory for that session, and the browser keeps no copy of it at all.
**A key is never written to disk, by ifc-console or by the page.** `/chat off`
and quitting both drop it, and the panel then asks again.

For a local server, choose the local provider and set the base URL, for
example `http://localhost:8000/v1` for vLLM or `http://localhost:1234/v1` for
LM Studio. With `chat.local_only true` in settings, ifc-console refuses any
provider URL that is not on this machine, which is the setting to use if
client models must never leave the building.

## What the assistant can do

Everything an MCP client can, through exactly the same tool functions:
queries, property sets, quantities, validation, clash detection, the knowledge
index, and (only in edit mode) changes to the model. Each tool call appears as
a chip under the answer with its result, so you can see what the assistant
actually did rather than trusting the prose.

The rules are unchanged:

- **The session mode still gates everything.** In `ask` mode any mutation
  fails with `ASK_MODE_BLOCKED`, whatever the model was asked to do.
- **You own the mode.** The panel cannot change it; `/mode edit` in your
  terminal can.
- **Every call is audited**, chat calls included, with the provider and model
  recorded and the key never written.

Turn tools off in the panel's settings if you just want a plain chat.

## Where the network goes

The browser never calls a provider. It posts to ifc-console on loopback, and
ifc-console makes the provider call, which is what keeps the key out of the
page and the content security policy tight.

What reaches the provider: your prompts, the system prompt, and whatever the
tools returned, which is data from your model file. If a model is
confidential, use a local provider (or do not use the panel). Element names
and property values are attacker-controllable text; the standing rule that
model text is data, never instructions, is in the system prompt, and tool
responses that look like instructions are flagged (see
[Safety model](safety.md)).

## From Python

The same loop is one SDK call, and it is provider neutral:

```python
from ifc_console import Workbench

with Workbench.open("tower.ifc") as wb:
    answer = wb.ask(
        "Which walls are missing a fire rating?",
        provider="anthropic",         # or openai, openrouter, local
        model="claude-sonnet-5",
    )
    print(answer["text"])
    print(answer["tool_calls"])       # what it actually ran
```

`wb.ask(..., on_event=print)` streams the events as they arrive. Without
`api_key=`, the key comes from the environment. See the [SDK](sdk.md).

## Settings

| key | default | meaning |
| --- | ------- | ------- |
| `chat.enabled_default` | `false` | start every session with the panel on |
| `chat.provider` | `openai` | provider the panel opens with |
| `chat.model` | empty | model the panel opens with |
| `chat.base_url` | empty | override the provider's URL (a local server) |
| `chat.tools` | `true` | lend the model the ifc-console tools |
| `chat.max_tool_rounds` | `8` | how many tool rounds one answer may take |
| `chat.local_only` | `false` | refuse any provider that is not on this machine |
| `chat.timeout_s` | `300` | how long to wait on the provider, first token included |

`ifc-console --chat` (with `--no-tui` or the console) starts with it on.

## The panel

The panel is chrome of the viewer: same title bar, same colours, same theme.
Under the title a single line names the provider and model you are on and the
session mode, so you always know what the answers are about and whether edits
are possible; click it to change either. Settings live in a dialog over the
panel, not in a strip that squeezes the conversation.

Picking a model: choose a provider and the panel lists the models that key can
reach; the refresh button reloads them, and "Custom id..." takes any name the
list does not have. The model and base URL are remembered per provider, so
switching to Claude and back does not lose the local server you set up. The
key is the one thing the browser keeps no copy of.

Under each answer you get the tool calls it made with their results, the token
counts, time to first token, and tokens per second. Answers copy whole or by
code block, and a failed answer offers a retry rather than making you retype
the question.

The conversation follows the tab: reloading keeps it, closing the tab drops
it, and "New chat" clears it. Stop ends the answer immediately, on the
provider as well as in the page.

## Keyboard

In the split view, **C** toggles the chat dock and the divider is draggable;
opening the chat folds the properties panel away so the 3D view keeps its
room. In the panel, Enter sends, Shift+Enter adds a line, and Escape closes
the settings dialog or stops a running answer.
