# Contributing and security

## Contributing

Setup, tests, and style live on the [Development](development.md) page. The short
version: `uv sync --extra dev`, then `uv run pytest` and
`uv run ruff check src tests` must both be green.

- **Bugs and ideas.** Open a
  [GitHub issue](https://github.com/nbharathik/ifc-console/issues). For bugs,
  include `ifc-console doctor` output. Never upload confidential models.
- **Scope.** ifc-console is deliberately small: a safe MCP bridge to IfcOpenShell
  with a console and an optional viewer. Anything that widens the
  `execute_ifc_code` attack surface, weakens the mode model, or adds network
  calls needs a strong case.
- **Compatibility.** The MCP tool names, their input schemas, and the response
  envelope are the public API. Changing them needs a version bump.

## Security

### Reporting a vulnerability

Report privately through GitHub Security Advisories ("Report a vulnerability" on
the repository's Security tab). Please do not open a public issue.

### Threat model, honestly

ifc-console runs LLM-written Python (`execute_ifc_code`) inside its own process. The
ask/edit mode gate, AST classification, runtime guards, and allowed-directory
checks stop **accidents and default behavior**, not a determined adversary:
in-process CPython sandboxing can always be escaped by creative enough code. One
known escape ships as a documented xfail test.

So "guards can be bypassed by adversarial code" is an acknowledged limitation,
not a new vulnerability. These are the interesting reports:

- writing to disk, or mutating the on-disk model, from `ask` mode
- reaching the network or the OS from a guarded run
- reading files outside the allowed directories
- bypassing the bearer token, or reaching the server off loopback
- the viewer gaining any mutation capability

### Staying safe

Keep the default `ask` mode for untrusted prompts and models. Rotate a leaked
token with `ifc-console token rotate`. Treat `edit` mode plus untrusted prompts like
running a script a stranger sent you.
