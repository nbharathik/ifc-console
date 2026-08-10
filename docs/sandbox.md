# Code sandbox

`execute_ifc_code` runs Python that a language model wrote. The mode switch
decides **whether** that code may change your model. The sandbox decides **what
the code can do to the rest of your machine**, which is a different question and
needs a different mechanism.

Eligible read-only runs use a separate process that has no network, cannot
start another process, inherits no credential-bearing environment variables,
blocks common credential stores, and can otherwise read the directories your
model lives in. The default auto mode reports and uses guarded in-process
fallback if the model copy or worker is unavailable; strict mode refuses it.

## Why a separate process

In-process guards shape the namespace generated code sees: an import allowlist,
a write-blocking `open`, a model object that refuses mutating methods. They are
good at stopping mistakes. They cannot stop a determined payload, because any
Python object graph eventually leads back to the real builtins. The test suite
has always documented one such escape on purpose.

A separate process changes the question. Even code that reaches the real
builtins runs inside a process where the dangerous operations themselves fail:

```python
# reaches the genuine builtins through the object graph
for c in ().__class__.__base__.__subclasses__():
    if c.__name__ == 'catch_warnings':
        b = c()._module.__builtins__
b['__import__']('socket').socket()
# EXEC_BLOCKED: sandbox: socket.__new__ is blocked
```

## What the sandbox enforces

| Control | How |
| --- | --- |
| No network | Every socket, urllib, http, ftp, and smtp operation is refused |
| No subprocesses | Process creation is refused, and on Windows the OS job object caps the sandbox at one process |
| Credential reduction | The worker gets a hand-built environment with no inherited keys or tokens, and common credential stores are denied even inside an allowed root |
| Read allowlist | Only the model directories, the sandbox scratch, and the Python installation |
| Write allowlist | Only the sandbox's own scratch directory; the model file itself is not writable |
| No arbitrary memory | `ctypes` calls that load libraries or address raw memory are refused |
| Memory cap | `sandbox.memory_mb`, enforced by the OS |
| Time cap | `exec.timeout_seconds`, enforced by killing the process |
| Dies with the console | It exits when the console closes its pipe; on Windows the job object also kills it if the console dies abruptly |

Enforcement sits on CPython's audit hooks, which fire inside the C
implementation of each operation and cannot be removed once installed. That is
why the escape above still fails: the block is below the object graph, not
inside it.

The console's own home directory and common credential locations such as
`.ssh`, `.aws`, `.git`, `.config`, package-manager credential files, `.env`,
`.env.*`, and `.envrc` are denied even when they sit inside a directory you
allowed. The console bearer token is never readable from the sandbox.

## What runs where

| Run | Where | Why |
| --- | --- | --- |
| Any code in `ask` mode | Sandbox when eligible | Auto mode reports guarded fallback; strict mode refuses it |
| Query-classified code in `edit` mode | Sandbox when eligible | Reading normally does not need the live model |
| Mutating code in `edit` mode | In-process, behind the guards | The edit has to land in the model the console is holding |

Mutating code always stays in-process, and reaching it already required you to
turn on edit mode by hand. A non-mutating run can also use the guarded
in-process path when sandboxing is off or when auto mode reports a fallback.

The sandbox reads the model **from disk**, so it can only be used when the file
matches what the console holds in memory. After an unsaved edit the console
falls back to the in-process path and says so in the response. Saving brings the
sandbox back.

## Two useful side effects

**Timeouts stop being sticky.** An in-process run that exceeds the timeout
leaves a thread CPython cannot kill, which pauses the session until you
`/reload`. A sandboxed run is a process: it gets killed, and the next call
works.

**Classifier misses get absorbed.** If code slips past both the classifier and
the guards and mutates the model, it mutates the sandbox's throwaway copy. Your
model is untouched. The response says so and the audit log records
`taint_contained`, instead of the session going tainted.

## Controlling it

```
/sandbox                 status: mode, worker, controls in force, next run
/sandbox auto            sandbox when possible, fall back otherwise (default)
/sandbox strict          refuse a read-only run that cannot be sandboxed
/sandbox off             in-process guards only
/sandbox restart         drop the worker; the next run starts a fresh one
```

`strict` never blocks edits: mutating code is exempt because it can never be
sandboxed by design. It applies to read-only runs, which is where the choice is
real.

| Setting | Default | Meaning |
| --- | --- | --- |
| `sandbox.mode` | `auto` | `auto`, `strict`, or `off` |
| `sandbox.memory_mb` | `2048` | Memory cap for the worker |
| `sandbox.max_model_mb` | `512` | Above this, the second copy costs more than the isolation is worth |
| `sandbox.startup_timeout` | `120` | Seconds to wait for the worker to come up |
| `sandbox.load_timeout` | `600` | Seconds to wait for the worker to read the model |
| `sandbox.warm_on_load` | `false` | Start the worker when a model loads, so the first code run is not the slow one |

None of these can be set from a project file. A cloned repository must not be
able to weaken your sandbox.

## Costs

The worker holds its own copy of the model, so a sandboxed session uses roughly
twice the memory of an unsandboxed one, and the first code run pays for starting
the process and reading the file. `sandbox.warm_on_load true` moves that cost to
model-open time; `sandbox.max_model_mb` sets the point where it stops being
worth paying at all.

## What it still does not guarantee

The sandbox is a strong containment boundary, not a virtual machine. It does not
defend against a kernel or interpreter vulnerability, and it does not stop code
from reading the model directory you deliberately gave it. The worker strips
environment credentials and blocks common credential stores, but it cannot
identify an arbitrary secret saved under another name such as `secrets.txt`
inside an allowed root. Treat every other file under an allowed root as readable
by generated code. The sandbox still blocks network and subprocess access and
prevents writes outside its scratch directory.

See also: [Safety model](safety.md).
