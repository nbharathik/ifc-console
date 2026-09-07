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

## What generated code can reach

Injected into every run, so ordinary work needs no import:

| name | is |
| ---- | -- |
| `ifc` | the loaded file (read-only outside edit mode) |
| `ifcopenshell`, `ifc_api` | the library and `ifcopenshell.api` |
| `element_util`, `selector_util`, `unit_util` | `ifcopenshell.util.element` / `.selector` / `.unit` |
| `shape_util`, `placement_util`, `representation_util` | `ifcopenshell.util.shape` / `.placement` / `.representation` |
| `schema_util`, `type_util` | `ifcopenshell.util.schema` / `.type` |
| `np` | numpy |
| `geom` | `ifcopenshell.geom`, the tessellator |
| `query(sel)`, `by_class(name)`, `psets(e)`, `qtos(e)`, `container(e)` | selector and lookup shortcuts |
| `get_ifc_file()` | the file object, for code that prefers a call |

The last five injected modules are imported on first use, so a run that never
touches geometry does not pay for the geometry engine.

### The import policy

`exec.import_policy` decides what code may import on top of that.

| policy | rule |
| ------ | ---- |
| `open` (default) | any installed package, except a denied set |
| `strict` | only the curated list below |

Under `open`, install the library that suits the job and the model can use it:
`numpy`, `shapely` and `trimesh` are here already, and `scipy`, `networkx`,
`manifold3d` or anything else works the moment you `pip install` it. A package
that is not installed fails with an ordinary `ModuleNotFoundError`, which costs
one call rather than a whole generated script.

Denied under both policies, because of the capability rather than the package:

| category | examples |
| -------- | -------- |
| the operating system | `os`, `sys`, `pathlib`, `shutil`, `tempfile`, `ctypes` |
| the network | `socket`, `urllib`, `http`, `requests`, `httpx`, `urllib3`, `aiohttp`, `paramiko`, `boto3` |
| other processes and interpreters | `subprocess`, `multiprocessing`, `threading`, `joblib`, `runpy`, `pdb`, `IPython`, `pip` |
| credentials | `keyring`, `openai`, `anthropic`, `mcp`, `dotenv` |
| deserializers that execute what they read | `pickle`, `marshal`, `dill`, `yaml`, `jinja2` |
| this console | `ifc_console`, `ifc_console_agents` |

Importing one of those is also SYSTEM-class code to the static classifier, so
it is refused with an explanation before the code runs, not part-way through.
There is no `bpy`: this is not Blender. `open()` is read-only and restricted to
the allowed directories, `io.open` is blocked so it cannot walk around that,
and writing an IFC file goes through `save_ifc_file`.

The curated `strict` list, for installations that want a fixed surface:

| group | modules |
| ----- | ------- |
| geometry and numerics | `numpy`, `shapely`, `trimesh` |
| IFC ecosystem | any `ifcopenshell` submodule, `ifctester`, `bcf` |
| numbers and text | `math`, `cmath`, `statistics`, `random`, `secrets`, `fractions`, `decimal`, `numbers`, `json`, `re`, `csv`, `textwrap`, `string`, `unicodedata`, `difflib`, `colorsys` |
| structure and iteration | `itertools`, `functools`, `operator`, `collections`, `heapq`, `bisect`, `graphlib`, `array`, `struct`, `enum`, `dataclasses`, `typing`, `abc`, `contextlib`, `copy`, `pprint`, `warnings`, `datetime`, `calendar`, `time`, `uuid`, `hashlib`, `base64`, `binascii`, `zlib`, `io` |

Under `strict`, numpy's own file readers (`np.load`, `np.fromfile`,
`np.savetxt`, ...) are blocked too, since a small allowlist is the point there
and they would be the one way around the guarded `open()`.

Either policy takes one more package by name:

```text
/settings exec.import_roots_extra ["scipy", "networkx"]
```

It wins over the denied set, so a user who really wants `numba` can have it,
and it is trusted input for that reason. Project settings cannot set it: like
the policy itself, it is a user decision. Naming a system module there still
leaves the static classifier's gate in place, so such code remains SYSTEM-class
and needs `exec.allow_system_access` in edit mode.

The worker imports the offered geometry libraries and anything on that list
before its audit hook is armed, because a library that touches
`object.__setattr__` while importing (trimesh does) cannot be imported once
generated code is running. A package installed after the worker started
therefore needs `/sandbox restart`.

### What the policy is, and is not

An import rule is guidance, not the boundary. Any library rich enough to be
worth having exposes a module attribute that reaches further than its own API,
so the controls that actually hold are elsewhere: the audit hook for read-only
code in the restricted process, and the mode gate for anything that changes the
model. `open` therefore buys real flexibility at little real cost, and `strict`
is there for installations that would rather present a small surface anyway.

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

In-process guards provide a curated namespace, an import policy, a
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
