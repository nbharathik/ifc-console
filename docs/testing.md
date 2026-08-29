# Testing the viewer, agent panel, and agents

Three layers, none of which needs an API key, a paid model, or a browser you
have to click through.

From a source checkout, install both workspace products and their test extras.
The root `package.json` then provides dependency-free shortcuts:

```bash
uv sync --all-packages --all-extras
```

```bash
npm run dev       # real viewer + Agent workspace in exactly one tab
npm run harness   # same server, print the URLs and open no tab
npm run check     # rebuild, run the offline HTTP/SSE checklist, then exit
npm test          # panel modules, markup contracts, and devkit unit tests
```

There is no npm install or second frontend server. The harness serves the real
core viewer plus the installed agent extension's panel assets with cache
revalidation, so a browser refresh picks up CSS and JavaScript edits. The agent
panel module and stylesheet are requested only when the extension contributes
that panel. `npm test` can run against the existing environment while the
harness stays up. `npm run check` resets only its disposable temp project, so
repeated checks start from the same server-side state.

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

The `dev` command and rehearsal provider come from `ifc-console-agents`. Core
viewer tests remain usable without an API key, model provider, or LLM.

**No browser tab is ever opened by `--check`.** The demo project lives under
your temp directory and its console home is isolated, so nothing touches your
real settings, keys, or recents.

Useful flags:

| Flag | Effect |
| --- | --- |
| `--fresh` | Delete and rebuild the default temporary demo project only |
| `--json` | Machine-readable results for CI |
| `--keep` | Keep serving after the checks so you can look at the panel |
| `--project DIR` | Use a specific directory instead of the temp one; cannot be combined with `--fresh` |
| `--file model.ifc` | Use your own IFC instead of the generated demo |
| `--port N` | Serve somewhere other than 8393 |

To look at the panel by hand, drop `--check`:

```bash
ifc-console dev --open agent
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

## 2. Panel module tests

The panel's pure logic lives in ES modules with no DOM dependency:

- `chat_markdown.js` - the renderer and its escaping
- `chat_flow.js` - the tool-to-stage map and the SSE run reducer
- `chat_history.js` - the local conversation archive and Markdown export
- `chat_sidebar.js` - assistant grouping and conversation bucketing
- `chat_workspace.js` - the agent workspace model

Run `npm test` from the repository root. Separate pytest wrappers enumerate the
core `tests/ui/*.test.mjs` files and the optional agents package's
`packages/ifc-console-agents/tests/ui/*.test.mjs` files explicitly, which works
across supported Node versions and shells.

The quoted glob works on Windows as well as macOS and Linux. No npm install,
browser, or dependencies are needed. `pytest` runs the same suite via
`tests/unit/test_ui_modules.py`, which skips when Node is absent and fails when
a pure panel module has no test.

## 3. `pytest`

The viewer and agent-panel asset tests are browser-less guards against markup
and script drifting apart: every `el("x")` must have a `data-role="x"`, every
handled action must have a button, and every button must be handled. The agent
tests also pin the security properties the panel promises, such as never
writing an API key to browser storage and never calling a provider directly,
and the two motion rules that cost real debugging time:

- entrance animations fill `forwards`, never `both`, because a `both` fill on
  an element that is still `display: none` keeps the keyframe's start state
  permanently;
- nothing animates layout width, because animating a grid item's width while
  its track resizes leaves the element stuck at its old size.

The suite can no longer open a browser: `tests/conftest.py` replaces
`webbrowser.open` with a recorder, so `/viewer` and `/agent` are exercised
without a tab appearing.

## When a link says the token is invalid

Opening `http://127.0.0.1:8383/viewer?panel=agents` with no `#t=` fragment makes the
browser fall back to a token remembered from an earlier console run. If that
console has stopped or restarted, the viewer now says the **link** has no valid
token, forgets the stale one so the next fresh link works, and points at
`/viewer` in the terminal for a new URL.
