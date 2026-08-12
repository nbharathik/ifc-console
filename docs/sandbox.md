# Code sandbox

The mode switch and sandbox solve different problems:

- **Mode** controls whether generated code may change the IFC model.
- **Sandbox** limits what eligible read-only code can do to the rest of the
  machine.

> **Python compatibility:** Secure sandboxing requires CPython 3.12+. On Python
> 3.10 and 3.11, `auto` uses a reported guarded fallback and `strict` refuses
> the run.

## Which mode should I use?

| sandbox mode | behavior | best for |
| ------------ | -------- | -------- |
| `auto` (default) | isolate when possible; report and use guarded fallback otherwise | normal interactive use |
| `strict` | refuse read-only code that cannot be isolated | sensitive or untrusted work |
| `off` | use in-process guards only | trusted debugging when isolation is unwanted |

Change it from the console:

```text
/sandbox
/sandbox strict
/sandbox auto
/sandbox restart
```

## What runs where

| generated code | execution location |
| -------------- | ------------------ |
| read-only code with a clean, eligible model on CPython 3.12+ | restricted process |
| read-only code when isolation is unavailable in `auto` | guarded main process |
| read-only code when isolation is unavailable in `strict` | refused |
| model-changing code in `edit` mode | guarded main process |

The sandbox reads its own model copy from disk. If the console has unsaved
changes, that disk copy no longer matches the live model, so read-only code
falls back in `auto` or is refused in `strict`. Saving or reloading restores
eligibility.

`strict` does not block edits. Mutating code is never sandbox-eligible because
its changes must reach the model held by the console.

## What the restricted process enforces

| control | effect |
| ------- | ------ |
| network | socket and common network-library operations are refused |
| subprocesses | child process creation is refused |
| credentials | environment keys and tokens are not inherited; common credential paths are blocked |
| reading | limited to model directories, Python files needed by the worker, and sandbox scratch space |
| writing | limited to sandbox scratch space; the model file is not writable |
| native memory | dangerous `ctypes` loading and raw-memory operations are refused |
| resources | execution time and memory are capped |
| lifetime | the worker exits with the console |

The worker uses CPython audit hooks for dangerous operations. These checks run
below the namespace presented to generated code and cannot be removed after
installation in that process.

The console home and common credential locations such as `.ssh`, `.aws`,
`.git`, `.config`, `.env`, and package-manager credential files remain blocked
even if they are inside an allowed model root.

## Why a separate process matters

In-process guards provide a curated namespace, an import allowlist, a
write-blocking `open`, and a model object that rejects mutation methods. These
controls are useful against mistakes, but a determined Python payload can
eventually recover real builtins through the object graph.

Inside the restricted process, recovering a builtin does not restore network,
subprocess, or unrestricted file access. The dangerous operation itself is
blocked. Unexpected model changes also affect only the worker's disposable
copy and are recorded as contained.

The process boundary also makes timeouts easier to recover from: the console
can kill a timed-out sandbox worker and start a clean one for the next call.

## Settings and cost

| setting | default | purpose |
| ------- | ------- | ------- |
| `sandbox.mode` | `auto` | choose `auto`, `strict`, or `off` |
| `sandbox.memory_mb` | `2048` | worker memory cap |
| `sandbox.max_model_mb` | `512` | do not copy larger models into the worker |
| `sandbox.startup_timeout` | `120` | seconds allowed for worker startup |
| `sandbox.load_timeout` | `600` | seconds allowed to load the model copy |
| `sandbox.warm_on_load` | `false` | start the worker when the model opens |

The worker keeps a second model copy, so it can nearly double model memory use.
The first read-only code run also pays the startup and model-load cost.
`sandbox.warm_on_load=true` moves that delay to model-open time.

Project settings cannot change sandbox options. Only user settings,
environment variables, or explicit command-line choices may weaken or expand
the boundary.

## Limitations

The sandbox does not defend against an interpreter or operating-system
vulnerability. It also cannot identify every secret stored under an arbitrary
name inside an allowed model directory. Treat ordinary files in allowed roots
as readable by generated code.

For the complete permission and persistence model, see [Safety](safety.md).
