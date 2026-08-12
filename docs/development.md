# Development

How to work on ifc-console itself. To report a bug or a security issue, see
[Contributing and security](contributing.md).

## Setup

```bash
git clone https://github.com/nbharathik/ifc-console && cd ifc-console
uv sync --extra dev
uv run ifc-console doctor
uv run pytest          # full suite; see the test summary for expected xfails
uv run ruff check src tests packages scripts
```

Standard src-layout package (`src/ifc_console/`), hatchling build, uv-managed.
Python 3.10 through 3.14.

## Tests

```bash
uv run pytest tests/unit           # pure logic: classifier, guards, envelope, hub
uv run pytest tests/integration    # in-memory MCP client + HTTP/WS TestClient
uv run pytest tests/tui            # Textual Pilot: console, commands, completion
```

Conventions worth knowing:

- Every test isolates `IFC_CONSOLE_HOME`, so nothing touches your real
  `~/.ifc-console`.
- The security suite asserts the on-disk model stays **byte-identical** under a
  battery of ask-mode bypass attempts. One known in-process escape is a strict
  xfail on purpose (see the honesty section of the [safety model](safety.md)).
- Fixtures regenerate deterministically:

```bash
uv run python tests/fixtures/make_fixtures.py
```

This produces minimal IFC4, IFC2X3, and IFC4X3 models whose walls carry real
geometry (the viewer and geometry tests rely on it), plus corrupt-file fixtures
for error paths.

## The vendored viewer

`packages/ifc-console-viewer/src/ifc_console_viewer/static/vendor/` contains
three.js and web-ifc exactly as shipped on npm (one import specifier is rewritten
in OrbitControls.js so no import map is needed). `VENDORED.md` in that folder
records versions, licenses, hashes, and the upgrade procedure. Do not edit
`web-ifc-api.js` or the WASM: MPL-2.0 files ship unmodified. Verify the bundle
with `uv run python scripts/check_vendor_assets.py`.

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
uv run mkdocs serve    # live preview at 127.0.0.1:8000
uv run mkdocs build --strict    # static site into site/
```

## CI and releases

Every push and pull request runs the suite on three operating systems and five
Python versions, plus builds both wheels and proves the core wheel contains no
viewer assets while the companion wheel contains the complete reviewed bundle.

Configure GitHub Pages once under **Settings > Pages > Build and deployment**
with **Source** set to **GitHub Actions**. The docs and release workflows build
with read-only repository and Pages access, upload an official Pages artifact,
then hand that artifact to a minimal deployment job. Only that job has
`pages: write` and `id-token: write`. Both workflows deploy through the
`github-pages` environment and share one concurrency group. If you add
environment branch or tag rules, allow `main` and the intended `v*` release
tags.

Before a release, `uv run python scripts/check_release.py --tag vX.Y.Z` verifies
that the tag, both package versions, their compatibility range, and the
changelog agree. Releases are cut by the maintainer pushing that tag: CI
re-runs the tests, validates both wheels and source archives, publishes the
viewer first and then the core package, and deploys these docs.

Configure trusted publishers for both the `ifc-console` and
`ifc-console-viewer` PyPI projects with this repository, workflow
`release.yml`, and the protected GitHub environment `pypi`. The workflow
builds, inspects, and smoke-tests artifacts in a job without OIDC permission.
Its minimal publishing job can only retrieve those verified files and publish
them after the environment gate.

The checkout-only lockfile is deliberately excluded from the source archive.
Install that archive through the standard PEP 517 path with `pip` or `uv pip`;
the release workflow smoke-tests this path directly.
