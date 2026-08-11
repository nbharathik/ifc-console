# Contributing and security

## Contributing

Setup, tests, and style live on the [Development](development.md) page. The short
version: `uv sync --extra dev`, then `uv run pytest` and
`uv run ruff check src tests` must both be green.

- **Bugs and ideas.** Open a
  [GitHub issue](https://github.com/nbharathik/ifc-console/issues). For bugs,
  include `ifc-console doctor` output. Never upload confidential models.
- **Scope.** ifc-console is deliberately small: a safe MCP bridge to IfcOpenShell
  with a console and a built-in viewer. Anything that widens the
  `execute_ifc_code` attack surface, weakens the mode model, or adds network
  calls needs a strong case.
- **Compatibility.** The MCP tool names, their input schemas, and the response
  envelope are the public API. Changing them needs a version bump.

## Security

### Reporting a vulnerability

Report privately through GitHub Security Advisories ("Report a vulnerability" on
the repository's Security tab). Please do not open a public issue.

### Threat model, honestly

Eligible read-only generated Python uses a separate restricted process with no
network or subprocess access, no inherited credential environment, blocks for
common credential stores, and a read allowlist for model directories. An
arbitrarily named secret inside an allowed root remains readable. The default
auto mode reports and uses guarded in-process fallback when isolation is not
available; strict mode refuses it. Mutating code always runs in-process after
the user explicitly selects edit mode, where namespace guards reduce accidents
but are not a secure boundary against adversarial Python. One documented xfail
records that in-process limitation.

An escape from the restricted read-only process is a security issue. A bypass
of only the edit-mode in-process namespace guards is an acknowledged limitation
unless it also crosses another boundary. Reports of particular interest are:

- writing to disk, or mutating the on-disk model, from `ask` mode
- reaching the network or the OS from a guarded run
- reading files outside the allowed directories
- bypassing the bearer token, or reaching the server off loopback
- the viewer gaining any mutation capability

### Staying safe

Keep the default `ask` mode for untrusted prompts and models. Rotate a leaked
token with `ifc-console token rotate`. Treat `edit` mode plus untrusted prompts like
running a script a stranger sent you.
