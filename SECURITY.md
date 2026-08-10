# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately via GitHub Security
Advisories ("Report a vulnerability" on the repository's Security tab).
Do not open a public issue. You should receive a response within a week.

## Scope and threat model, honestly

ifc-console runs LLM-written Python (`execute_ifc_code`). Eligible non-mutating
runs use a separate sandbox process with no network, no subprocesses, no
inherited credentials in its environment, a memory cap, and read access limited
to the model directories; enforcement there sits on CPython audit hooks. Common
credential stores are denied inside those roots, but an arbitrarily named
secret file in an allowed model directory remains readable. The default
`sandbox.mode=auto` reports and uses in-process guards if a run is ineligible or
the worker is unavailable; `strict` refuses that fallback. Mutating runs always
execute in the server process behind namespace guards, because the edit has to
land in the live model, and getting there already required the user to switch
the session to `edit` mode in their own terminal.

The layered controls (session modes, AST classification, runtime guards,
allowed-directory checks) stop **accidents and default behavior**. In-process
CPython guards can always be escaped by sufficiently creative code; one known
escape is documented as an xfail test in the suite.

Reports that in-process guards can be bypassed by adversarial code are
therefore acknowledged limitations rather than new vulnerabilities, unless they
also work from `ask` mode or from inside the sandbox. Especially interesting
reports:

- writing to disk, or mutating the on-disk model, from `ask` mode
- reaching the network, a subprocess, or the OS from inside the sandbox
- reading files outside the allowed directories
- bypassing the bearer token on the HTTP surface, or reaching the server from a
  non-loopback origin
- the viewer gaining any mutation capability
- a payload inside an IFC file that makes an assistant act against the user

## Indirect prompt injection

Element names, descriptions, property values, and file headers come from
whoever authored the model, so they are attacker-controlled text that an
assistant will read. Two mitigations ship by default:

1. The server instructions tell the assistant that model text is data, never
   instructions, and that mode changes only ever happen in the user's terminal.
2. Tool responses carry a `meta.injection_warning` when returned text reads
   like instructions to an assistant, with the excerpts that triggered it.

Neither is a guarantee. Treat `edit` mode plus a model from an untrusted source
the way you would treat running a script someone emailed you.

## The chat panel and the network

ifc-console makes no outbound network calls except from the optional chat
panel, which is off by default. When it is on, the console (not the browser)
calls the LLM provider you configured, so your prompts and whatever the tools
read from the model reach that provider.

- Keys come from an environment variable or from the panel. They are held in
  memory for the session and dropped on `/chat off`; nothing is written to
  disk, logged, or put in a URL, and provider error bodies are redacted.
- Provider URLs reject embedded credentials and non-HTTP schemes. Redirects
  cannot change scheme, host, or port, so authorization headers stay on the
  configured origin.
- `chat.local_only true` refuses any provider URL that is not on this machine.
- Chat tool calls go through the same functions, the same ask/edit gate, and
  the same audit log as an MCP client. A chat session cannot mutate a model in
  `ask` mode.
- Reports worth sending: a key reaching disk or a log, the panel reaching a
  provider the user did not configure, or a chat tool call bypassing the mode
  gate.

## Hardening tips for users

- Keep the default `ask` mode for untrusted prompts and models.
- `/sandbox strict` refuses a generated read-only run when the sandbox is
  unavailable, instead of falling back to in-process guards.
- The HTTP server binds to 127.0.0.1. MCP and session APIs require a bearer
  token. Browser shells and static assets expose no session data, the viewer
  WebSocket authenticates its first frame, and the identity route returns only
  a nonce-bound proof. Rotate the token with `ifc-console token rotate` if it
  leaks.
- Before the stdio bridge attaches that token, it sends a fresh random nonce
  to the configured loopback port and requires a domain-separated HMAC proof
  bound to the identity route and port. A foreign listener cannot collect the
  token merely by occupying the configured port, and bridge responses are
  size-limited. This is not isolation from malicious code running as the same
  OS user, which can read the persistent token file or inspect peer processes.
- Project settings cannot change the server port, authentication behavior,
  allowed directories, sandbox policy, session mode, or plugin allowlist.
- Keep secrets outside model directories. The sandbox blocks common stores and
  `.env` variants, but cannot infer that an arbitrary file contains a secret.
- Every session writes an audit log; `/audit` shows it live and
  `ifc-console sessions show <id>` reads it afterwards.
- The knowledge index is built from the ifcopenshell package already installed
  on your machine. It performs no network access.

## Python plugins

Operation plugins are trusted code and are not sandboxed. Discovery does not
import them, loading is disabled by default, and only exact names in the
user-owned `plugins.allow` list can load. Project settings cannot enable or
allow plugins. Review a package before enabling it and use
`ifc-console plugins doctor` to validate its manifest and registration.
