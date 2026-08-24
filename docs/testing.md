# Testing the panel and the agents

Three layers, none of which needs an API key, a paid model, or a browser you
have to click through.

## 1. `ifc-console dev --check`

```bash
ifc-console dev --check
```

Builds a throwaway demo project, boots the real HTTP server on port 8393, and
walks every panel feature once through the actual routes: the token gate, the
viewer and chat shells, provider and model discovery, the agent list, the
capability blocks, reference indexing, a document upload, creating a custom
agent, and one full streaming run per agent including the vision path and an
AI-marked proposal. It prints a table and exits non-zero on failure.

**No browser tab is ever opened by `--check`.** The demo project lives under
your temp directory and its console home is isolated, so nothing touches your
real settings, keys, or recents.

Useful flags:

| Flag | Effect |
| --- | --- |
| `--fresh` | Delete and rebuild the demo project first |
| `--json` | Machine-readable results for CI |
| `--keep` | Keep serving after the checks so you can look at the panel |
| `--project DIR` | Use a specific directory instead of the temp one |
| `--file model.ifc` | Use your own IFC instead of the generated demo |
| `--port N` | Serve somewhere other than 8393 |

To look at the panel by hand, drop `--check`:

```bash
ifc-console dev --open chat
```

That opens **exactly one** tab, and only because you asked. Without a terminal
(piped output, CI) it opens none and just prints the URLs.

### The rehearsal provider

`dev` registers an offline provider called **rehearsal** that speaks the same
normalized event vocabulary as OpenAI and Anthropic. It walks the real
multi-round tool loop: scope the model, read the documents, apply a recipe and
measure, then propose a marked value. It contacts nothing, costs nothing, and
is registered only under `dev` or `IFC_CONSOLE_DEV=1`, so a normal run cannot
reach it.

## 2. `node --test tests/ui`

The panel's pure logic lives in ES modules with no DOM dependency:

- `chat_markdown.js` - the renderer and its escaping
- `chat_flow.js` - the tool-to-stage map and the SSE run reducer
- `chat_history.js` - the local conversation archive and Markdown export
- `chat_sidebar.js` - assistant grouping and conversation bucketing
- `chat_workspace.js` - the agent workspace model

```bash
node --test tests/ui/*.test.mjs
```

No npm install, no browser, no dependencies. `pytest` runs the same suite via
`tests/unit/test_ui_modules.py`, which skips when Node is absent and fails when
a pure panel module has no test.

## 3. `pytest`

`tests/unit/test_viewer_assets.py` is the browser-less guard against markup and
script drifting apart: every `el("x")` must have a `data-role="x"`, every
handled action must have a button, and every button must be handled. It also
pins the security properties the panel promises, such as never writing an API
key to browser storage and never calling a provider directly, and the two
motion rules that cost real debugging time:

- entrance animations fill `forwards`, never `both`, because a `both` fill on
  an element that is still `display: none` keeps the keyframe's start state
  permanently;
- nothing animates layout width, because animating a grid item's width while
  its track resizes leaves the element stuck at its old size.

The suite can no longer open a browser: `tests/conftest.py` replaces
`webbrowser.open` with a recorder for every test, so `/viewer`, `/chat`, and
`/agent` are exercised without a tab appearing.

## When a link says the token is invalid

Opening `http://127.0.0.1:8383/viewer?chat=1` with no `#t=` fragment makes the
browser fall back to a token remembered from an earlier console run. If that
console has stopped or restarted, the viewer now says the **link** has no valid
token, forgets the stale one so the next fresh link works, and points at
`/viewer` in the terminal for a new URL.
