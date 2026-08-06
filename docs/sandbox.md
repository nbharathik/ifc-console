# Code sandbox

`execute_ifc_code` runs Python that a language model wrote. The mode switch
decides **whether** that code may change your model. The sandbox decides **what
the code can do to the rest of your machine**, which is a different question and
needs a different mechanism.

Read-only runs execute in a separate process that has no network, cannot start
another process, holds no credentials, and can only read the directories your
model lives in. That is the whole idea in one sentence.

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
| No credentials | The worker gets a hand-built environment: no API keys, no cloud credentials, not even the console's own token |
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

The console's own home directory is denied outright, even when it sits inside a
directory you allowed. Your bearer token is never readable from the sandbox.

## What runs where

| Run | Where | Why |
| --- | --- | --- |
| Any code in `ask` mode | Sandbox | The default posture: nothing the model writes touches the console process |
| Query-classified code in `edit` mode | Sandbox | Reading does not need the live model |
| Mutating code in `edit` mode | In-process, behind the guards | The edit has to land in the model the console is holding |

Mutating code is the one path that stays in-process, and reaching it already
required you to turn on edit mode by hand.

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
from reading the model you deliberately gave it. Treat it as: generated code
cannot reach your network, your credentials, or your files, and cannot damage
anything outside the directories you opened.

See also: [Safety model](safety.md).
