# Development

How to work on ifc-console itself. To report a bug or a security issue, see
[Contributing and security](contributing.md).

## Setup

```bash
git clone https://github.com/nbharathik/ifc-console && cd ifc-console
uv sync --package ifc-console              # core runtime only
uv sync --all-packages --all-extras       # complete core + agents workspace
uv run --all-packages --all-extras ifc-console doctor
uv run --all-packages --all-extras pytest  # full suite; see the test summary for expected xfails
uv run --all-packages --all-extras ruff check src tests packages scripts
```

The uv workspace contains two active hatchling distributions:

```text
src/ifc_console/                                  ifc-console
packages/ifc-console-agents/src/ifc_console_agents/  ifc-console-agents
```

Both support Python 3.10 through 3.14. Core owns deterministic IFC behavior
and the viewer; agents depend on core and register through
`ifc_console.extensions`. Never add an import from core to
`ifc_console_agents`.

## Browser panel harness

The root npm scripts are dependency-free command shortcuts around the Python
dev harness; they do not add a second frontend build or require `npm install`.

```bash
npm run dev       # serve the generated IFC scenario and open one Agent workspace tab
npm run harness   # serve it without opening a browser; use one of the printed URLs
npm run check     # rebuild and exercise the real routes and agent streams headlessly
npm test          # panel logic, DOM contracts, and devkit unit tests
```

`npm test` reuses the synced environment without rewriting its console entry
point, so it remains safe to run while `npm run harness` is serving the viewer.

The viewer UI remains source-owned browser ESM. Its Agent boundary uses
AI SDK-compatible message and stream shapes, while the visual primitives use
native dialog, details, select, and switch controls. The panel itself consumes
that boundary: `chat_ai_sdk.js` owns request building, SSE framing, and the
proposal wire shape, so there is one implementation to keep correct rather
than a panel copy beside an embedder copy. Anything the panel attaches to
`/viewer` runs under `style-src 'self'`, so component sizing and
state travel as attributes and classes; a `style` attribute is dropped without
breaking the page, which makes it an expensive bug to notice.

Vercel AI SDK is a state and transport library, not a component theme.
Adopting literal AI Elements or shadcn React components would therefore be a
deliberate React, TypeScript, and Tailwind build migration rather than a
styling-only dependency.

The rehearsal provider uses no API key and makes no network requests. For a
custom model or port, call the underlying command directly, for example
`uv run --frozen --all-packages ifc-console dev --open none --port 8394 --file path/to/model.ifc`.

## Tests

```bash
uv run --all-packages --all-extras pytest tests/unit
uv run --all-packages --all-extras pytest tests/integration
uv run --all-packages --all-extras pytest tests/tui
```

Conventions worth knowing:

- Every test isolates `IFC_CONSOLE_HOME`, so nothing touches your real
  `~/.ifc-console`.
- The security suite asserts the on-disk model stays **byte-identical** under a
  battery of ask-mode bypass attempts. One known in-process escape is a strict
  xfail on purpose (see the honesty section of the [safety model](safety.md)).
- Fixtures regenerate deterministically:

```bash
uv run --all-packages --all-extras python tests/fixtures/make_fixtures.py
```

This produces minimal IFC4, IFC2X3, and IFC4X3 models whose walls carry real
geometry (the viewer and geometry tests rely on it), plus corrupt-file fixtures
for error paths.

## The vendored viewer

`src/ifc_console/viewer/static/vendor/` contains
three.js and web-ifc exactly as shipped on npm (one import specifier is rewritten
in OrbitControls.js so no import map is needed). `VENDORED.md` in that folder
records versions, licenses, hashes, and the upgrade procedure. Do not edit
`web-ifc-api.js` or the WASM: MPL-2.0 files ship unmodified. Verify the bundle
with `uv run --all-packages --all-extras python scripts/check_vendor_assets.py`.

## Style

- `ruff check` must stay clean (config in `pyproject.toml`; line length 100,
  isort, bugbear, pyupgrade, simplify).
- Every user-facing error carries a `hint` telling the reader (human or LLM)
  what to do next. Hints that mention the terminal name real console commands
  (`/mode`, `/viewer`, `/reload`), not keystrokes.
- Comments explain constraints and intent, not restatements of the code.
- The MCP tool list and the JSON envelope are the compatibility surface.
  Renaming a tool, narrowing an input schema, or changing envelope fields is a
  breaking change and needs a version bump.

## Docs

```bash
uv sync --extra docs
uv run --all-packages --all-extras mkdocs serve
uv run --all-packages --all-extras mkdocs build --strict
```

## CI and releases

Every push and pull request runs the suite across the supported operating
systems and Python versions. Packaging checks build both active wheels and
source archives, prove the viewer bundle is complete in core, reject agent
modules and panel assets from core, and reject `ifc_console` files from the
agent wheel.

Release budgets are deliberately small despite the vendored browser runtime:

| artifact | hard wheel limit |
| -------- | ---------------- |
| `ifc-console` | 2.5 MB |
| `ifc-console-agents` | 1.0 MB |
| both wheels together | 3.0 MB |

The browser also keeps startup work bounded: core viewer modules load normally,
while an installed agent panel's JavaScript and CSS load only when the panel is
opened. Size checks complement, rather than replace, the static-file allowlist,
vendor hashes, and dependency audits.

Configure GitHub Pages once under **Settings > Pages > Build and deployment**
with **Source** set to **GitHub Actions**. The docs and release workflows build
with read-only repository and Pages access, upload an official Pages artifact,
then hand that artifact to a minimal deployment job. Only that job has
`pages: write` and `id-token: write`. Both workflows deploy through the
`github-pages` environment and share one concurrency group. If you add
environment branch or tag rules, allow `main` and the intended `v*` release
tags.

Before a release,
`uv run --all-packages --all-extras python scripts/check_release.py --tag vX.Y.Z`
verifies
that the tag, both package versions, their compatibility range, and the
changelog agree. Releases are cut by the maintainer pushing that tag: CI
re-runs the tests, validates both wheels and source archives, publishes core
first and then its dependent agents package, and deploys these docs.

Configure trusted publishers for both the `ifc-console` and
`ifc-console-agents` PyPI projects with this repository, workflow
`release.yml`, and the protected GitHub environment `pypi`. The workflow
builds, inspects, and smoke-tests artifacts in a job without OIDC permission.
Its minimal publishing job can only retrieve those verified files and publish
them after the environment gate.

`ifc-console[viewer]` and the retired `ifc-console-viewer` project are retained
only as one-release compatibility no-ops/shims. They are not a third active
product and must not regain viewer assets or enter the normal publish order.

The checkout-only lockfile is deliberately excluded from the source archive.
Install that archive through the standard PEP 517 path with `pip` or `uv pip`;
the release workflow smoke-tests this path directly.
