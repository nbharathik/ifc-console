"""Automation commands: check, run, jobs, batch, workflows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ifc_console.cli.common import _new_store
from ifc_console.policy.modes import Mode


def add_check_parser(sub: argparse._SubParsersAction) -> None:
    check = sub.add_parser("check", help="Validate a model for CI: schema plus optional IDS files.")
    check.add_argument("model", help="IFC file to check.")
    check.add_argument(
        "--ids",
        action="append",
        default=[],
        metavar="FILE",
        help="buildingSMART IDS file to check against (repeatable).",
    )
    check.add_argument(
        "--express-rules",
        action="store_true",
        help="Also run EXPRESS where-rules (slow on large models).",
    )
    check.add_argument("--max-issues", type=int, default=200)
    check.add_argument("--format", choices=["text", "json", "sarif", "junit"], default="text")
    check.add_argument("--output", default=None, metavar="FILE", help="Write the report to a file.")
    check.set_defaults(func=_cmd_check)


def add_run_parser(sub: argparse._SubParsersAction) -> None:
    workflow_run = sub.add_parser(
        "run", help="Plan or execute a versioned IFC automation workflow."
    )
    workflow_run.add_argument("manifest", help="Workflow .json, .yaml, or .yml file.")
    workflow_run.add_argument(
        "--plan", action="store_true", help="Resolve and hash inputs without scheduling work."
    )
    workflow_run.add_argument("--json", action="store_true")
    workflow_run.add_argument(
        "--output-dir", default=None, metavar="DIR", help="Export workflow and step artifacts."
    )
    workflow_run.set_defaults(func=_cmd_workflow_run)


def add_jobs_parser(sub: argparse._SubParsersAction) -> None:
    jobs = sub.add_parser("jobs", help="Run and inspect durable automation jobs.")
    jobs_sub = jobs.add_subparsers(dest="jobs_cmd", required=True)
    jobs_validate = jobs_sub.add_parser(
        "validate", help="Run isolated validation and persist report artifacts."
    )
    jobs_validate.add_argument("model", help="IFC model to validate.")
    jobs_validate.add_argument("--ids", action="append", default=[], metavar="FILE")
    jobs_validate.add_argument("--express-rules", action="store_true")
    jobs_validate.add_argument("--max-issues", type=int, default=200)
    jobs_validate.add_argument("--expected-revision", default=None)
    jobs_validate.add_argument("--json", action="store_true", help="Print the job record.")
    jobs_validate.add_argument(
        "--output-dir", default=None, metavar="DIR", help="Export generated artifacts."
    )
    jobs_validate.set_defaults(func=_cmd_jobs_validate)
    jobs_commit = jobs_sub.add_parser(
        "commit", help="Run a journaled commit and stream transaction phases."
    )
    jobs_commit.add_argument("model")
    jobs_commit.add_argument("change_set_id")
    jobs_commit.add_argument("--approval", required=True, dest="approval_id")
    jobs_commit.add_argument("--json", action="store_true")
    jobs_commit.set_defaults(func=_cmd_jobs_commit)
    jobs_restore = jobs_sub.add_parser(
        "restore", help="Run a journaled restore and stream transaction phases."
    )
    jobs_restore.add_argument("model")
    jobs_restore.add_argument("commit_id")
    jobs_restore.add_argument("--confirm", action="store_true")
    jobs_restore.add_argument("--json", action="store_true")
    jobs_restore.set_defaults(func=_cmd_jobs_restore)
    jobs_list = jobs_sub.add_parser("list", help="List persisted jobs.")
    jobs_list.add_argument("--limit", type=int, default=50)
    jobs_list.add_argument("--json", action="store_true")
    jobs_list.set_defaults(func=_cmd_jobs_list)
    jobs_show = jobs_sub.add_parser("show", help="Show one persisted job.")
    jobs_show.add_argument("job_id")
    jobs_show.add_argument("--json", action="store_true")
    jobs_show.set_defaults(func=_cmd_jobs_show)
    jobs_cancel = jobs_sub.add_parser("cancel", help="Request cancellation of a running job.")
    jobs_cancel.add_argument("job_id")
    jobs_cancel.add_argument("--json", action="store_true")
    jobs_cancel.set_defaults(func=_cmd_jobs_cancel)


def add_batch_parser(sub: argparse._SubParsersAction) -> None:
    batch = sub.add_parser(
        "batch", help="Run resumable read-only automation across many IFC files."
    )
    batch_sub = batch.add_subparsers(dest="batch_cmd", required=True)
    batch_validate = batch_sub.add_parser(
        "validate", help="Capture and validate IFC files with bounded concurrency."
    )
    batch_validate.add_argument("models", nargs="+", help="IFC files to validate.")
    batch_validate.add_argument("--ids", action="append", default=[], metavar="FILE")
    batch_validate.add_argument("--express-rules", action="store_true")
    batch_validate.add_argument("--max-issues", type=int, default=200)
    batch_validate.add_argument("--concurrency", type=int, default=2)
    batch_validate.add_argument(
        "--failure-policy", choices=["continue", "fail_fast"], default="continue"
    )
    batch_validate.add_argument("--json", action="store_true")
    batch_validate.add_argument(
        "--output-dir", default=None, metavar="DIR", help="Export the manifest and reports."
    )
    batch_validate.set_defaults(func=_cmd_batch_validate)
    batch_query = batch_sub.add_parser(
        "query", help="Stream one selector result artifact per IFC file."
    )
    batch_query.add_argument("models", nargs="+", help="IFC files to query.")
    batch_query.add_argument("--selector", required=True, help="IfcOpenShell selector.")
    batch_query.add_argument(
        "--field",
        action="append",
        dest="fields",
        choices=["name", "predefined_type", "type_name", "storey", "description", "tag"],
        help="Result field beyond global_id and class (repeatable).",
    )
    batch_query.add_argument("--order-by", choices=["class", "name", "storey"], default="class")
    batch_query.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    batch_query.add_argument("--limit", type=int, default=100_000, help="Per-file row cap.")
    batch_query.add_argument("--concurrency", type=int, default=2)
    batch_query.add_argument(
        "--failure-policy", choices=["continue", "fail_fast"], default="continue"
    )
    batch_query.add_argument("--json", action="store_true")
    batch_query.add_argument(
        "--output-dir", default=None, metavar="DIR", help="Export the manifest and results."
    )
    batch_query.set_defaults(func=_cmd_batch_query)
    batch_list = batch_sub.add_parser("list", help="List durable batch records.")
    batch_list.add_argument("--limit", type=int, default=50)
    batch_list.add_argument("--json", action="store_true")
    batch_list.set_defaults(func=_cmd_batch_list)
    batch_show = batch_sub.add_parser("show", help="Show one batch and its children.")
    batch_show.add_argument("batch_id")
    batch_show.add_argument("--json", action="store_true")
    batch_show.set_defaults(func=_cmd_batch_show)
    batch_resume = batch_sub.add_parser(
        "resume", help="Verify captured sources and retry unfinished children."
    )
    batch_resume.add_argument("batch_id")
    batch_resume.add_argument("--json", action="store_true")
    batch_resume.set_defaults(func=_cmd_batch_resume)
    batch_cancel = batch_sub.add_parser("cancel", help="Cancel a running batch.")
    batch_cancel.add_argument("batch_id")
    batch_cancel.add_argument("--json", action="store_true")
    batch_cancel.set_defaults(func=_cmd_batch_cancel)


def add_workflows_parser(sub: argparse._SubParsersAction) -> None:
    workflows = sub.add_parser("workflows", help="Inspect the workflow schema and durable runs.")
    workflows_sub = workflows.add_subparsers(dest="workflows_cmd", required=True)
    workflows_schema = workflows_sub.add_parser(
        "schema", help="Print the version 1 workflow manifest JSON Schema."
    )
    workflows_schema.set_defaults(func=_cmd_workflows_schema)
    workflows_list = workflows_sub.add_parser("list", help="List durable workflow runs.")
    workflows_list.add_argument("--limit", type=int, default=50)
    workflows_list.add_argument("--json", action="store_true")
    workflows_list.set_defaults(func=_cmd_workflows_list)
    workflows_show = workflows_sub.add_parser("show", help="Show a workflow and its steps.")
    workflows_show.add_argument("workflow_id")
    workflows_show.add_argument("--json", action="store_true")
    workflows_show.set_defaults(func=_cmd_workflows_show)
    workflows_watch = workflows_sub.add_parser(
        "watch", help="Watch a workflow owned by this or another local process."
    )
    workflows_watch.add_argument("workflow_id")
    workflows_watch.add_argument("--json", action="store_true")
    workflows_watch.set_defaults(func=_cmd_workflows_watch)
    workflows_resume = workflows_sub.add_parser(
        "resume", help="Verify sources and retry unfinished workflow steps."
    )
    workflows_resume.add_argument("workflow_id")
    workflows_resume.add_argument("--json", action="store_true")
    workflows_resume.add_argument("--output-dir", default=None, metavar="DIR")
    workflows_resume.set_defaults(func=_cmd_workflows_resume)
    workflows_cancel = workflows_sub.add_parser("cancel", help="Cancel a running workflow.")
    workflows_cancel.add_argument("workflow_id")
    workflows_cancel.add_argument("--json", action="store_true")
    workflows_cancel.set_defaults(func=_cmd_workflows_cancel)


# --------------------------------------------------------------------------- check
def _cmd_check(args: argparse.Namespace) -> int:
    from ifc_console.checks import render, run_check
    from ifc_console.core.results import ToolError

    model = Path(args.model)
    if not model.is_file():
        print(f"error: {model} does not exist", file=sys.stderr)
        return 4
    try:
        report = run_check(
            model,
            ids_paths=[Path(p) for p in args.ids],
            express_rules=args.express_rules,
            max_issues=args.max_issues,
        )
    except ToolError as exc:  # missing ifctester extra or unreadable IDS file
        print(f"error: {exc.message}", file=sys.stderr)
        print(f"hint: {exc.hint}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: could not parse {model.name}: {exc}", file=sys.stderr)
        return 4
    rendered = render(report, args.format)
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
        print(f"wrote {args.format} report to {args.output}")
    else:
        print(rendered)
    return 0 if report["passed"] else 5


# --------------------------------------------------------------------------- jobs
def _automation_core(mode: Mode | None = None):
    from ifc_console.app import AppCore

    store = _new_store()
    return AppCore(store, mode=mode, transport="cli")


def _print_job(record: Any, *, as_json: bool) -> None:
    if as_json:
        print(record.model_dump_json(indent=2))
        return
    print(f"job       {record.job_id}")
    print(f"state     {record.state.value}")
    print(f"progress  {record.progress}%")
    print(f"message   {record.message}")
    print(f"phase     {record.phase}")
    print(f"cancel    {'allowed' if record.cancellable else 'closed'}")
    if record.transaction_id:
        print(f"transaction {record.transaction_id}")
    print(f"revision  {record.spec.revision.revision_id}")
    if record.summary:
        for key, value in record.summary.items():
            print(f"{key:<10} {value}")
    if record.failure is not None:
        print(f"error     {record.failure.code}: {record.failure.message}")
    for artifact in record.artifacts:
        print(f"artifact  {artifact.artifact_id}  {artifact.name}")


def _tool_error(exc: Exception) -> int:
    print(f"error: {getattr(exc, 'message', str(exc))}", file=sys.stderr)
    hint = getattr(exc, "hint", "")
    if hint:
        print(f"hint: {hint}", file=sys.stderr)
    return 2


def _cmd_jobs_validate(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    model = Path(args.model).expanduser().resolve()
    ids_paths = tuple(Path(path).expanduser().resolve() for path in args.ids)
    core = _automation_core()

    async def run():
        core.start_audit()
        core.add_allowed_dir(model.parent)
        for path in ids_paths:
            core.add_allowed_dir(path.parent)
        await core.open_model(model)
        submitted = await core.jobs.submit_validation(
            ids_paths=ids_paths,
            express_rules=args.express_rules,
            max_issues=args.max_issues,
            expected_revision=args.expected_revision,
        )
        completed = submitted
        async for update in core.jobs.watch(submitted.job_id):
            completed = update
            print(
                f"{update.job_id}  {update.progress:3}%  {update.message}",
                file=sys.stderr,
            )
        return completed

    try:
        record = asyncio.run(run())
        if args.output_dir:
            output_dir = Path(args.output_dir).expanduser().resolve()
            for artifact in record.artifacts:
                core.artifacts.export(artifact.artifact_id, output_dir / artifact.name)
        _print_job(record, as_json=args.json)
        if record.state.value != "succeeded":
            return 1
        return 0 if record.summary.get("passed") else 5
    except ToolError as exc:
        return _tool_error(exc)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_jobs_commit(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core(Mode.EDIT)

    async def run():
        model = Path(args.model).expanduser().resolve()
        core.start_audit()
        core.add_allowed_dir(model.parent)
        await core.open_model(model)
        submitted = await core.jobs.submit_commit(args.change_set_id, approval_id=args.approval_id)
        completed = submitted
        async for update in core.jobs.watch(submitted.job_id):
            completed = update
            print(
                f"{update.job_id}  {update.progress:3}%  {update.phase}  {update.message}",
                file=sys.stderr,
            )
        return completed

    try:
        record = asyncio.run(run())
        _print_job(record, as_json=args.json)
        return 0 if record.state.value == "succeeded" else 1
    except ToolError as exc:
        return _tool_error(exc)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_jobs_restore(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core(Mode.EDIT)

    async def run():
        model = Path(args.model).expanduser().resolve()
        core.start_audit()
        core.add_allowed_dir(model.parent)
        await core.open_model(model)
        submitted = await core.jobs.submit_restore(args.commit_id, confirm=args.confirm)
        completed = submitted
        async for update in core.jobs.watch(submitted.job_id):
            completed = update
            print(
                f"{update.job_id}  {update.progress:3}%  {update.phase}  {update.message}",
                file=sys.stderr,
            )
        return completed

    try:
        record = asyncio.run(run())
        _print_job(record, as_json=args.json)
        return 0 if record.state.value == "succeeded" else 1
    except ToolError as exc:
        return _tool_error(exc)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_jobs_list(args: argparse.Namespace) -> int:
    core = _automation_core()
    try:
        records = core.jobs.list(limit=args.limit)
        if args.json:
            print(json.dumps([record.model_dump(mode="json") for record in records], indent=2))
        elif not records:
            print("(no jobs)")
        else:
            for record in records:
                print(
                    f"{record.job_id}  {record.state.value:9}  "
                    f"{record.progress:3}%  {record.message}"
                )
        return 0
    finally:
        core.shutdown()


def _cmd_jobs_show(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        record = core.jobs.get(args.job_id)
        _print_job(record, as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_jobs_cancel(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        record = asyncio.run(core.jobs.cancel(args.job_id))
        _print_job(record, as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


# --------------------------------------------------------------------------- batches
def _print_batch(record: Any, *, as_json: bool) -> None:
    if as_json:
        print(record.model_dump_json(indent=2))
        return
    print(f"batch      {record.batch_id}")
    print(f"state      {record.state.value}")
    print(f"progress   {record.progress}%")
    print(f"message    {record.message}")
    print(f"inputs     {len(record.children)}")
    print(f"runs       {record.run_count}")
    print(f"concurrency {record.spec.concurrency}")
    print(f"policy     {record.spec.failure_policy}")
    for child in record.children:
        name = Path(child.source.path).name
        detail = f"job={child.job_id}" if child.job_id else "not submitted"
        print(
            f"child {child.index:03} {child.state.value:9} attempts={child.attempts} "
            f"{name}  {detail}"
        )
        if child.failure is not None:
            print(f"      error {child.failure.code}: {child.failure.message}")
    if record.aggregate_artifact is not None:
        print(
            f"manifest   {record.aggregate_artifact.artifact_id}  {record.aggregate_artifact.name}"
        )


async def _watch_batch(core: Any, batch_id: str) -> Any:
    completed = core.batches.get(batch_id)
    async for update in core.batches.watch(batch_id):
        completed = update
        print(
            f"{update.batch_id}  {update.progress:3}%  {update.message}",
            file=sys.stderr,
        )
    return completed


def _batch_exit_code(record: Any) -> int:
    if record.state.value != "succeeded":
        return 1
    return 0 if record.summary.get("passed") else 5


def _cmd_batch_validate(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    models = tuple(Path(path).expanduser().resolve() for path in args.models)
    ids_paths = tuple(Path(path).expanduser().resolve() for path in args.ids)
    core = _automation_core()

    async def run():
        core.start_audit()
        for path in (*models, *ids_paths):
            core.add_allowed_dir(path.parent)
        submitted = await core.batches.submit_validation(
            models,
            ids_paths=ids_paths,
            express_rules=args.express_rules,
            max_issues=args.max_issues,
            concurrency=args.concurrency,
            failure_policy=args.failure_policy,
        )
        return await _watch_batch(core, submitted.batch_id)

    try:
        record = asyncio.run(run())
        if args.output_dir:
            output_dir = Path(args.output_dir).expanduser().resolve()
            if record.aggregate_artifact is not None:
                core.artifacts.export(
                    record.aggregate_artifact.artifact_id,
                    output_dir / record.aggregate_artifact.name,
                )
            for child in record.children:
                for artifact in child.artifacts:
                    core.artifacts.export(
                        artifact.artifact_id,
                        output_dir / f"{child.index:03}-{artifact.name}",
                    )
        _print_batch(record, as_json=args.json)
        return _batch_exit_code(record)
    except ToolError as exc:
        return _tool_error(exc)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_batch_query(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    models = tuple(Path(path).expanduser().resolve() for path in args.models)
    fields = tuple(args.fields or ("name", "storey", "type_name"))
    core = _automation_core()

    async def run():
        core.start_audit()
        for path in models:
            core.add_allowed_dir(path.parent)
        submitted = await core.batches.submit_query(
            models,
            query=args.selector,
            fields=fields,
            order_by=args.order_by,
            output_format=args.format,
            limit=args.limit,
            concurrency=args.concurrency,
            failure_policy=args.failure_policy,
        )
        return await _watch_batch(core, submitted.batch_id)

    try:
        record = asyncio.run(run())
        if args.output_dir:
            output_dir = Path(args.output_dir).expanduser().resolve()
            if record.aggregate_artifact is not None:
                core.artifacts.export(
                    record.aggregate_artifact.artifact_id,
                    output_dir / record.aggregate_artifact.name,
                )
            for child in record.children:
                for artifact in child.artifacts:
                    core.artifacts.export(
                        artifact.artifact_id,
                        output_dir / f"{child.index:03}-{artifact.name}",
                    )
        _print_batch(record, as_json=args.json)
        return _batch_exit_code(record)
    except ToolError as exc:
        return _tool_error(exc)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_batch_list(args: argparse.Namespace) -> int:
    core = _automation_core()
    try:
        records = core.batches.list(limit=args.limit)
        if args.json:
            print(json.dumps([record.model_dump(mode="json") for record in records], indent=2))
        elif not records:
            print("(no batches)")
        else:
            for record in records:
                print(
                    f"{record.batch_id}  {record.state.value:11}  "
                    f"{record.progress:3}%  {len(record.children):4} inputs  {record.message}"
                )
        return 0
    finally:
        core.shutdown()


def _cmd_batch_show(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        _print_batch(core.batches.get(args.batch_id), as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_batch_resume(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core()

    async def run():
        core.start_audit()
        resumed = await core.batches.resume(args.batch_id)
        return await _watch_batch(core, resumed.batch_id)

    try:
        record = asyncio.run(run())
        _print_batch(record, as_json=args.json)
        return _batch_exit_code(record)
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_batch_cancel(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        core.start_audit()
        record = asyncio.run(core.batches.cancel(args.batch_id))
        _print_batch(record, as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


# --------------------------------------------------------------------------- workflows
def _print_workflow(record: Any, *, as_json: bool) -> None:
    if as_json:
        print(record.model_dump_json(indent=2))
        return
    print(f"workflow    {record.workflow_id}")
    print(f"name        {record.plan.spec.name}")
    print(f"plan        {record.plan.plan_id}")
    print(f"state       {record.state.value}")
    print(f"progress    {record.progress}%")
    print(f"message     {record.message}")
    print(f"runs        {record.run_count}")
    for step in record.steps:
        detail = f"batch={step.batch_id}" if step.batch_id else "not submitted"
        print(
            f"step {step.id:<20} {step.state.value:11} attempts={step.attempts} "
            f"output={step.output} {detail}"
        )
        if step.failure is not None:
            print(f"     error {step.failure.code}: {step.failure.message}")
    if record.aggregate_artifact is not None:
        print(
            f"manifest    {record.aggregate_artifact.artifact_id}  {record.aggregate_artifact.name}"
        )


def _print_workflow_plan(plan: Any, *, as_json: bool) -> None:
    if as_json:
        print(plan.model_dump_json(indent=2))
        return
    print(f"workflow    {plan.spec.name}")
    print(f"plan        {plan.plan_id}")
    print(f"version     {plan.spec.version}")
    print(f"steps       {len(plan.steps)}")
    print(f"children    {plan.total_children}")
    for step in plan.steps:
        dependencies = ",".join(step.needs) if step.needs else "-"
        print(
            f"step {step.id:<20} {step.batch_spec.operation.kind:10} "
            f"inputs={len(step.batch_spec.inputs)} needs={dependencies} output={step.output}"
        )


async def _watch_workflow(core: Any, workflow_id: str) -> Any:
    completed = core.workflows.get(workflow_id)
    async for update in core.workflows.watch(workflow_id):
        completed = update
        print(
            f"{update.workflow_id}  {update.progress:3}%  {update.message}",
            file=sys.stderr,
        )
    return completed


def _workflow_exit_code(record: Any) -> int:
    if record.state.value != "succeeded":
        return 1
    return 0 if record.summary.get("passed") else 5


def _export_workflow(core: Any, record: Any, output_dir: Path) -> None:
    if record.aggregate_artifact is not None:
        core.artifacts.export(
            record.aggregate_artifact.artifact_id,
            output_dir / record.aggregate_artifact.name,
        )
    for step in record.steps:
        if step.batch_id is None:
            continue
        batch = core.batches.get(step.batch_id)
        if batch.aggregate_artifact is not None:
            core.artifacts.export(
                batch.aggregate_artifact.artifact_id,
                output_dir / f"{step.output}-{batch.aggregate_artifact.name}",
            )
        for child in batch.children:
            for artifact in child.artifacts:
                core.artifacts.export(
                    artifact.artifact_id,
                    output_dir / f"{step.output}-{child.index:03}-{artifact.name}",
                )


def _cmd_workflow_run(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    manifest = Path(args.manifest).expanduser().resolve()
    core = _automation_core()

    async def run() -> Any:
        core.start_audit()
        core.add_allowed_dir(manifest.parent)
        plan = await core.workflows.plan_manifest(manifest)
        if args.plan:
            return plan
        submitted = await core.workflows.submit_plan(plan)
        return await _watch_workflow(core, submitted.workflow_id)

    try:
        result = asyncio.run(run())
        if args.plan:
            _print_workflow_plan(result, as_json=args.json)
            return 0
        if args.output_dir:
            _export_workflow(core, result, Path(args.output_dir).expanduser().resolve())
        _print_workflow(result, as_json=args.json)
        return _workflow_exit_code(result)
    except ToolError as exc:
        return _tool_error(exc)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_workflows_list(args: argparse.Namespace) -> int:
    core = _automation_core()
    try:
        records = core.workflows.list(limit=args.limit)
        if args.json:
            print(json.dumps([record.model_dump(mode="json") for record in records], indent=2))
        elif not records:
            print("(no workflows)")
        else:
            for record in records:
                print(
                    f"{record.workflow_id}  {record.state.value:11}  "
                    f"{record.progress:3}%  {len(record.steps):3} steps  {record.plan.spec.name}"
                )
        return 0
    finally:
        core.shutdown()


def _cmd_workflows_schema(_args: argparse.Namespace) -> int:
    from ifc_console.core.workflows import WorkflowSpec

    print(json.dumps(WorkflowSpec.model_json_schema(), indent=2))
    return 0


def _cmd_workflows_show(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        _print_workflow(core.workflows.get(args.workflow_id), as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_workflows_watch(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        record = asyncio.run(_watch_workflow(core, args.workflow_id))
        _print_workflow(record, as_json=args.json)
        return _workflow_exit_code(record)
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_workflows_resume(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core()

    async def run() -> Any:
        core.start_audit()
        existing = core.workflows.get(args.workflow_id)
        if existing.plan.manifest_path:
            core.add_allowed_dir(Path(existing.plan.manifest_path).parent)
        resumed = await core.workflows.resume(args.workflow_id)
        return await _watch_workflow(core, resumed.workflow_id)

    try:
        record = asyncio.run(run())
        if args.output_dir:
            _export_workflow(core, record, Path(args.output_dir).expanduser().resolve())
        _print_workflow(record, as_json=args.json)
        return _workflow_exit_code(record)
    except ToolError as exc:
        return _tool_error(exc)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_workflows_cancel(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        core.start_audit()
        record = asyncio.run(core.workflows.cancel(args.workflow_id))
        _print_workflow(record, as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()
