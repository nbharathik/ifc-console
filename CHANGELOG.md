# Changelog

Notable user-facing changes to ifc-console are recorded here. The project uses
semantic versioning for its MCP schema, documented Python SDK, versioned plugin
API, and stable command-line exit codes.

## [0.1.4] - 2026-08-10

### Added

- A public synchronous and asynchronous Python SDK with typed lifecycle
  records, direct operation calls, and headless workflow support.
- A versioned, allowlisted plugin API that exposes one operation consistently
  through the SDK, MCP, and browser assistant.
- Durable validation and streaming-query jobs, resumable multi-file batches,
  and version 1 JSON or YAML workflow graphs.
- Structured property and classification previews with explicit approval,
  verified commit, automatic rollback, and guarded restore.
- An offline IFC knowledge index for schema entities, property sets,
  IfcOpenShell APIs, and tested recipes.
- Multi-model workspaces, content-addressed artifacts, retention controls, and
  integrity-chained audit verification.
- A responsive optional viewer and browser assistant with visible prompt-route
  state and local-only provider controls.

### Changed

- The operation surface now has 36 core operations and 4 viewer operations,
  all backed by one typed registry and capability policy.
- Read-only generated code runs in a restricted subprocess by default.
- Clash mode `sampled` replaces the misleading `exact` name; `exact` remains a
  compatibility alias and reports the sampled method honestly.
- Viewer assets moved to the optional `ifc-console-viewer` distribution, used
  through the `ifc-console[viewer]` extra.
- Tokens are omitted from generated client snippets unless explicitly
  requested.
- Viewer navigation, panels, splitters, dialogs, loading states, and chat are
  responsive and keyboard accessible, with reduced-motion, forced-color, and
  coarse-pointer support.
- Release automation tests every supported Python version on Linux, Windows,
  and macOS, audits dependencies and source, verifies exact archive contents,
  and publishes only a previously verified artifact through minimal OIDC jobs.

### Fixed

- Python 3.10 imports no longer depend on `enum.StrEnum` from Python 3.11.
- Synchronous and asynchronous SDK signatures, error normalization, settings
  overrides, and repeated shutdown behavior now agree.
- Viewer and chat state recover from corrupt or unavailable browser storage,
  and chat panel imports cannot race or overwrite newer UI state.
- Every CLI port flag rejects zero, privileged, negative, and out-of-range
  values before server or client configuration is created.

### Security

- Provider URLs, redirects, loopback-only routing, proxy behavior, and secret
  redaction are validated consistently for model discovery and completion.
- Project settings cannot enable edit mode or plugins, and plugin loading is
  disabled until a user-owned exact allowlist enables a trusted package.
- Plugin registration is atomic, output is schema-checked and size-bounded,
  reserved metadata cannot be spoofed, and lifecycle failures are redacted.
- The stdio bridge proves the loopback server identity with a fresh nonce and
  HMAC before sending a bearer token, refuses redirects, and bounds responses.
- HTTP Host, Origin, authentication, body, chat stream, WebSocket, screenshot,
  and search inputs are validated and bounded.
- Sandbox requests, code, output, stderr, and protocol frames are bounded;
  native escape surfaces and nested credential-store paths are denied.
- Model saves and transaction commits verify source revisions, candidate IFC
  files, backups, checksums, replacement journals, and directory durability.
- Vendored browser assets have pinned SHA-256 digests checked by CI.

### Compatibility notes

- Python 3.10 through 3.14 are supported.
- The viewer is now optional: install `ifc-console[viewer]` for the local 3D
  interface.
- Plugin API version 1 is an in-process trusted-code contract, not a sandbox.

## [0.1.2] - 2026-07-29

- Improved package metadata and the installation and update path.

## [0.1.1] - 2026-07-29

- Added analysis tools, viewer improvements, background preload, and expanded
  IFC file handling.

## [0.1.0] - 2026-07-17

- First public release with the standalone MCP server, ask/edit safety switch,
  Textual console, core IFC query and edit tools, and local 3D viewer.

[0.1.4]: https://github.com/nbharathik/ifc-console/compare/v0.1.2...v0.1.4
[0.1.2]: https://github.com/nbharathik/ifc-console/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/nbharathik/ifc-console/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/nbharathik/ifc-console/releases/tag/v0.1.0
