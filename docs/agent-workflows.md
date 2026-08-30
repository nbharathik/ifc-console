# Agent workflows

A workflow is a saved sequence of steps you start with one click: run some
tools, let an agent read the results, stop for a human decision, write a
report. It is the answer to typing the same prompt every week.

```text
inputs -> tool steps -> agent step -> gate -> report artifact
```

Open the surface from the terminal with `/workflows`, or from the Workflows
button in the Agent panel header. Both mount the same component and read the
same workflows, so neither has a private copy of the truth.

!!! note "Two kinds of workflow"

    This page is about staged runs over the open model, which can include
    agents. [Automation workflows](workflows.md) are a different thing: a
    deterministic manifest that fans one validation or query operation out
    over many files, with no LLM anywhere. Use those for batch checks in CI.

## What ships

| Workflow | What it does |
| --- | --- |
| `revision-qa-gate` | Validates schema, health, and (optionally) IDS, then turns the failures into a punch list. |
| `revision-diff-review` | Compares the open model against another attached revision and explains what changed, with risk flags. |
| `measurement-audit` | Applies a saved skill or recipe across a selector and reports every deviation. |

`/workflows list` prints what this project can run.

## Running one

1. Pick it in the list. Its inputs appear with their defaults.
2. Fill anything required and press **Run workflow**.
3. Steps light up as they run. A gate stops the run and waits for you.
4. The report appears at the bottom and is saved as an artifact.

A workflow made only of tools, gates, and a report needs no provider or API
key: that work is the console's own. A model is required only when the
workflow has an agent step.

## Writing your own

Workflow files are YAML in `.ifc-console/agents/workflows/`. A project file
overrides a built-in of the same name, so you can adapt a shipped workflow
without forking the package.

```yaml
version: "1"
name: door-widths
title: Door width audit
description: Check every door against the project minimum.
tags: [measurement]

inputs:
  - id: minimum
    label: Minimum clear width
    type: text
    default: "900 mm"
    required: true

steps:
  - id: doors
    kind: tool
    title: Find the doors
    tool: query_elements
    arguments:
      selector: IfcDoor

  - id: audit
    kind: agent
    title: Measure and compare
    needs: [doors]
    preset: measurement
    prompt: |
      Check these doors against a clear width of {{ inputs.minimum }}.
      The query returned:
      {{ steps.doors.text }}
      Report every door below the minimum with its name, GlobalId, and storey.

  - id: report
    kind: export
    title: Write the report
    needs: [audit]
    name: door-widths
    body: |
      # Door widths
      Minimum: {{ inputs.minimum }}

      {{ steps.audit.text }}
```

### Step kinds

| Kind | Fields | Notes |
| --- | --- | --- |
| `tool` | `tool`, `arguments`, `optional` | Calls one console tool. `optional: true` turns a failure into a skip instead of ending the run. |
| `agent` | `prompt`, `preset` or `blocks`, `role`, `max_tool_rounds`, `max_tool_calls` | Runs one agent. `blocks` scopes its tools. |
| `gate` | `message`, `detail` | Stops for a human decision. Approving continues; refusing ends the run. |
| `export` | `name`, `body` | Writes a Markdown artifact. |

### Inputs

`text`, `number`, `boolean`, `choice` (with `choices`), and `path`. Each takes
`label`, `default`, `required`, and `help`.

### References

`{{ inputs.<id> }}` and `{{ steps.<id>.text }}` are substituted wherever they
appear in a prompt, an argument, a gate message, or a report body.
`{{ steps.<id>.data.<field> }}` reaches into a tool's structured result. An
unknown reference becomes empty text rather than an error, and a substituted
value is inserted literally: workflow files are content, not a template
language, and nothing in them executes.

Steps must be declared in dependency order; `needs` names what a step reads.

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
