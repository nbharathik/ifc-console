# Agent workflows

A workflow is primarily a reusable system prompt attached to an agent. Running
one asks for nothing: the console adds the viewer scope and whatever prompt you
type for that run, and the workflow's own prompt decides the rest. Advanced
workflows can still add deterministic tools, a human decision, or a report step.

```text
saved system prompt + viewer scope + this run's prompt -> agent/tools -> report
```

There are three places to run one, and they all read the same library:

| Door | For |
| --- | --- |
| **Library** | The workflow surface's first view. Every workflow is a row on the left; the page on the right says what it does, shows its stages and exact system prompt, takes an optional prompt for this run, and has the Run control. Tick rows to start several together. |
| **Runs** | The surface's second view: every run of the session, streaming or finished, with its stages, tool calls, decisions, result, and a follow-up box in the same thread. |
| **The chat composer** | Type `/` in the Agent panel and pick a workflow. It attaches to the conversation as a chip; Run starts it with the agent's own tools, and follow-ups stay in that thread. |

Open the surface from the terminal with `/workflows`, or from the Workflows
button in the Agent panel header. Authoring is a page of the Library, reached
from **Edit** on a workflow or **New workflow** in the top bar. Saved skills
remain available as slash commands and can also be bound to a workflow in the
builder.

## Running one from the chat

A workflow is mostly a system prompt, so the composer can carry it without a
separate run surface:

1. Type `/` in the Agent panel. The list opens with the workflows first, then
   the panel commands, then saved skills. Pick one, or keep typing to filter.
   `/measure` finds `measurement-audit`.
2. The workflow becomes a chip above the composer. Hover it, focus it, or
   press its name to read the exact text it adds: description, stages, and the
   system prompt. Once the run has started the preview shows the instructions
   the console actually sent, including the scope and the procedure written
   from the workflow's steps.
3. Add any other context the same way as for a normal message: the 3D
   selection is already a chip, `@` mentions project content, and the plus
   menu attaches a file or a screenshot. Anything typed in the composer is the
   prompt for this run; an empty composer sends the workflow's own task.
4. Press **Run**. If the workflow names an assistant that is installed, the
   panel switches to it first, so the run uses the tools the workflow was
   written for. A workflow declared `scope: selection` runs on the elements
   selected in the 3D view and refuses to start on nothing.
5. Keep asking. The chip stays pinned to the conversation and every later turn
   names the workflow again, so the console keeps one thread with the
   workflow's prompt in place. Remove the chip to start a fresh conversation
   without it.

The chat path has no staged runner: tool steps become "call this tool with
these arguments" lines, a gate becomes "stop and ask before continuing", and a
report step becomes the shape of the final answer. The session mode,
approval gates, and AI provenance all apply exactly as they do to any chat
turn. Use the workflow surface when you want the deterministic stages
executed by the console and kept as a run record.

**Run in chat** on a Library page hands that workflow, its scope, and the run
prompt typed there to the composer.

!!! note "Two kinds of workflow"

    This page is about staged runs over the open model, which can include
    agents. [Automation workflows](workflows.md) are a different thing: a
    deterministic manifest that fans one validation or query operation out
    over many files, with no LLM anywhere. Use those for batch checks in CI.

## What ships

| Workflow | What it does |
| --- | --- |
| `quick-model-check` | Captures project identity, spatial structure, and model health with deterministic tools; no AI provider is needed. |
| `property-completeness-review` | Checks whether a delivery-critical property is populated over the scope it chooses, or the viewer selection, and produces a storey-aware missing-data list. |
| `quantity-snapshot` | Aggregates stored or derived quantities and turns them into a takeoff with coverage warnings. |
| `coordination-clash-review` | Scans systems for overlaps or clearance breaches, triages hotspots, and waits for review before exporting. |
| `revision-qa-gate` | Validates schema and health, adds IDS when a run names one, then turns the failures into a punch list. |
| `revision-diff-review` | Compares the open model against another attached revision and explains what changed, with risk flags. |
| `measurement-audit` | Measures the model or exact viewer selection, uses optional run settings as constraints, and never assumes an expected value or tolerance. |
| `element-parameters` | For the viewer selection: lists the properties and quantities the schema expects, derives the missing values from geometry, model context, and project documents, waits for review, then writes the reviewed candidates as AI-marked proposals. |
| `model-quality-review` | Scores the open model deterministically on ten dimensions, adds health and schema findings, and has the review agent explain the grade and order the improvements. |

`/workflows list` prints what this project can run.

## Running one on the workflow surface

1. Open **Library**. Every workflow is a row: name and purpose, with a dot
   that says whether it needs a language model. Open one to read its page.
2. Press **Run** on the page, or the play control on the row. That is the
   whole requirement. There is no form to fill in, because a workflow's system
   prompt says what to do when nothing is specified.
3. To steer one run, type into **Prompt for this run**: a storey, a
   tolerance, an element class, who reads the report. It is layered on top of
   the system prompt for that run only and is never saved.
4. To scope a run to what you are looking at, select elements in the 3D view and
   switch the scope control in the top bar to **Selection**, or press
   **Run workflow** in the viewer's status bar, which opens the library with the
   selection already chosen. Workflows declared `scope: model` ignore it;
   workflows declared `scope: selection` require it and say so on their page.
5. To start several at once, tick their rows and press **Run selected** at
   the foot of the list. Each one starts independently with its own run prompt.
6. The view switches to **Runs**, where each run shows its captured context,
   streamed model output, tool calls and result previews, stage state,
   decisions, and token usage. Internal hidden model reasoning is not
   displayed. The finished report has its own **Result** view.
7. Ask a follow-up in the box at the bottom of a finished run. The question
   keeps the workflow's prompt, tools, and scope, and can see the report it
   already produced, so it re-checks the model rather than guessing.

The run history is kept for the lifetime of the open workflow workspace, up
to thirty runs, with each run's entries and tool previews bounded so a long
session cannot grow the page without limit. A running workflow continues when
another run is opened. **Run again** starts a new execution from the same
workflow context. The Agent control in the top bar switches directly back to
the Agent panel.

A workflow made only of tools, gates, and a report needs no provider or API
key: that work is the console's own. A language model is required only when
the workflow has an agent step.

## Writing your own

The shortest route is **New workflow** in the surface's top bar. Give the workflow a
name and detailed system prompt, choose an existing agent, optionally choose a
saved skill, and decide whether it can run on the whole model, a viewer
selection, or either. Optional default key/value settings are reusable context,
not a fixed form schema. Saving creates a project workflow and opens it.
The same editor can later change the full prompt or add a separate layer of
project instructions. **Create agent** jumps to the existing agent builder when the procedure needs
a new capability mix.

The file format remains available for procedures that need deterministic tool
steps or review gates. Workflow files are YAML in
`.ifc-console/agents/workflows/`. A project file overrides a built-in of the
same name, so you can adapt a shipped workflow without forking the package.

```yaml
version: "1"
name: door-widths
title: Door width audit
description: Review door widths using the current run context.
tags: [measurement]
scope: either
system_prompt: |
  Review clear door widths using evidence from IFC geometry. Treat the viewer
  selection as exact when present. Use each run setting as an instruction.
  When a target width is supplied, compare against it. Otherwise report the
  observed distribution without inventing a pass/fail threshold.
settings:
  audience: coordinator

steps:
  - id: audit
    kind: agent
    title: Measure and compare
    preset: measurement
    prompt: Carry out the workflow system prompt and return the report.

  - id: report
    kind: export
    title: Write the report
    needs: [audit]
    name: door-widths
    body: |
      # Door widths
      {{ steps.audit.text }}
```

### Step kinds

| Kind | Fields | Notes |
| --- | --- | --- |
| `tool` | `tool`, `arguments`, `optional` | Calls one console tool. `optional: true` turns a failure into a skip instead of ending the run. |
| `agent` | `prompt`, `agent`, `preset` or `blocks`, `role`, `max_tool_rounds`, `max_tool_calls` | Runs an installed agent or a scoped capability composition. `role` is the standing instruction for that step. |
| `gate` | `message`, `detail` | Stops for a human decision. Approving continues; refusing ends the run. |
| `export` | `name`, `body` | Writes a Markdown artifact. |

### Settings and inputs

Every workflow accepts free-form run settings. Defaults live in the top-level
`settings` mapping, and a person can add, override, or remove them in Setup.
They are appended to the agent's system prompt. `{{ settings.<key> }}` can
also reference a setting explicitly.

No shipped workflow declares `inputs`, and new ones should not: a value a run
needs belongs in the system prompt as a stated default, in `settings` as
reusable context, or in the run prompt. Declared inputs still parse, so a
hand-written file that needs typed tool arguments keeps working, and the Run
row shows those fields in a fold. Their types are `text`, `number`, `boolean`,
`choice` (with `choices`), `path`, and `model`, and each takes `label`,
`default`, `required`, and `help`.

### References

`{{ settings.<key> }}`, `{{ inputs.<id> }}`, and `{{ steps.<id>.text }}` are substituted wherever they
appear in a prompt, an argument, a gate message, or a report body.
`{{ steps.<id>.data.<field> }}` reaches into a tool's structured result. An
unknown reference becomes empty text rather than an error, and a substituted
value is inserted literally: workflow files are content, not a template
language, and nothing in them executes.

Steps must be declared in dependency order; `needs` names what a step reads.
Agent prompts also receive the run scope automatically. `{{ scope.text }}` and
`{{ scope.selections }}` expose it explicitly when a gate or report needs to
name the selection. `scope: model` always runs over the model, `selection`
requires a live viewer selection, and `either` lets the person choose at run
time.

## Keeping runs fast

`blocks` on an agent step is the lever that matters. A stage that only reads
validation results should not carry the geometry, document, and code tool
schemas: fewer tools means a smaller prompt, fewer wrong calls, and a cheaper
run. Give each stage the smallest set that can do its job.

## Safety

Workflows change nothing on their own. Agent steps run under the same rules as
the Agent panel: the session's ask/edit mode, capability policy, and approval
gates all still apply, and any property an agent proposes lands in the
reserved `IfcConsole_AI_` namespace for a human to approve. A `gate` step is a
stop you put in the workflow itself, on top of those.
