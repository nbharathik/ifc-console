"""Change commands: transactions, artifacts, and safe edits (changes)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ifc_console.cli.automation import _automation_core, _tool_error
from ifc_console.cli.common import _new_store
from ifc_console.policy.modes import Mode


def add_transactions_parser(sub: argparse._SubParsersAction) -> None:
    transactions = sub.add_parser(
        "transactions", help="Inspect durable commit and restore recovery journals."
    )
    transactions_sub = transactions.add_subparsers(dest="transactions_cmd", required=True)
    transactions_list = transactions_sub.add_parser("list")
    transactions_list.add_argument("--json", action="store_true")
    transactions_list.set_defaults(func=_cmd_transactions_list)
    transactions_show = transactions_sub.add_parser("show")
    transactions_show.add_argument("transaction_id")
    transactions_show.add_argument("--json", action="store_true")
    transactions_show.set_defaults(func=_cmd_transactions_show)


def add_artifacts_parser(sub: argparse._SubParsersAction) -> None:
    artifacts = sub.add_parser("artifacts", help="Inspect and export durable job outputs.")
    artifacts_sub = artifacts.add_subparsers(dest="artifacts_cmd", required=True)
    artifacts_list = artifacts_sub.add_parser("list")
    artifacts_list.add_argument("--limit", type=int, default=50)
    artifacts_list.add_argument("--json", action="store_true")
    artifacts_list.set_defaults(func=_cmd_artifacts_list)
    artifacts_show = artifacts_sub.add_parser("show")
    artifacts_show.add_argument("artifact_id")
    artifacts_show.add_argument("--json", action="store_true")
    artifacts_show.set_defaults(func=_cmd_artifacts_show)
    artifacts_export = artifacts_sub.add_parser("export")
    artifacts_export.add_argument("artifact_id")
    artifacts_export.add_argument("path")
    artifacts_export.add_argument("--overwrite", action="store_true")
    artifacts_export.set_defaults(func=_cmd_artifacts_export)
    artifacts_pin = artifacts_sub.add_parser("pin", help="Retain an artifact across cleanup.")
    artifacts_pin.add_argument("artifact_id")
    artifacts_pin.set_defaults(func=_cmd_artifacts_pin)
    artifacts_unpin = artifacts_sub.add_parser(
        "unpin", help="Remove an explicit artifact retention pin."
    )
    artifacts_unpin.add_argument("artifact_id")
    artifacts_unpin.set_defaults(func=_cmd_artifacts_unpin)
    artifacts_gc = artifacts_sub.add_parser(
        "gc", help="Plan or explicitly apply reference-aware artifact cleanup."
    )
    artifacts_gc.add_argument("--older-than-days", type=int, default=None)
    artifacts_gc.add_argument("--apply", action="store_true")
    artifacts_gc.add_argument("--confirm", action="store_true")
    artifacts_gc.add_argument("--json", action="store_true")
    artifacts_gc.set_defaults(func=_cmd_artifacts_gc)


def add_changes_parser(sub: argparse._SubParsersAction) -> None:
    changes = sub.add_parser("changes", help="Preview, approve, commit, and restore safe edits.")
    changes_sub = changes.add_subparsers(dest="changes_cmd", required=True)
    changes_preview = changes_sub.add_parser(
        "preview", help="Preview an existing occurrence property value update."
    )
    changes_preview.add_argument("model", help="IFC model to inspect without modifying it.")
    changes_preview.add_argument("--global-id", action="append", required=True, dest="global_ids")
    changes_preview.add_argument("--pset", required=True, dest="pset_name")
    changes_preview.add_argument("--property", required=True, dest="property_name")
    changes_preview.add_argument(
        "--create-missing",
        action="store_true",
        help="Explicitly preview creating a missing occurrence property or property set.",
    )
    changes_preview.add_argument(
        "--nominal-type",
        default=None,
        help="IFC value type for creation, such as IfcLabel or IfcLengthMeasure.",
    )
    value_group = changes_preview.add_mutually_exclusive_group(required=True)
    value_group.add_argument("--value", dest="plain_value", help="String property value.")
    value_group.add_argument(
        "--value-json",
        dest="json_value",
        help="JSON scalar for a string, number, boolean, or null value.",
    )
    changes_preview.add_argument("--expected-revision", default=None)
    changes_preview.add_argument("--json", action="store_true")
    changes_preview.set_defaults(func=_cmd_changes_preview)

    changes_classify = changes_sub.add_parser(
        "classify", help="Preview a direct occurrence classification assignment."
    )
    changes_classify.add_argument("model", help="IFC model to inspect without modifying it.")
    changes_classify.add_argument("--global-id", action="append", required=True, dest="global_ids")
    changes_classify.add_argument("--system", required=True, dest="classification_name")
    changes_classify.add_argument("--identification", required=True)
    changes_classify.add_argument("--name", required=True, dest="reference_name")
    changes_classify.add_argument("--expected-revision", default=None)
    changes_classify.add_argument("--json", action="store_true")
    changes_classify.set_defaults(func=_cmd_changes_classify)

    changes_show = changes_sub.add_parser("show", help="Show one ChangeSet preview.")
    changes_show.add_argument("change_set_id")
    changes_show.add_argument("--json", action="store_true")
    changes_show.set_defaults(func=_cmd_changes_show)

    changes_approve = changes_sub.add_parser(
        "approve", help="Create an explicit caller approval for a ChangeSet."
    )
    changes_approve.add_argument("change_set_id")
    changes_approve.add_argument("--by", required=True, dest="approved_by")
    changes_approve.add_argument("--reason", default="")
    changes_approve.add_argument("--json", action="store_true")
    changes_approve.set_defaults(func=_cmd_changes_approve)

    changes_commit = changes_sub.add_parser(
        "commit", help="Verify and atomically commit an approved ChangeSet."
    )
    changes_commit.add_argument("model")
    changes_commit.add_argument("change_set_id")
    changes_commit.add_argument("--approval", required=True, dest="approval_id")
    changes_commit.add_argument("--json", action="store_true")
    changes_commit.set_defaults(func=_cmd_changes_commit)

    changes_receipt = changes_sub.add_parser("receipt", help="Show a commit receipt.")
    changes_receipt.add_argument("commit_id")
    changes_receipt.add_argument("--json", action="store_true")
    changes_receipt.set_defaults(func=_cmd_changes_receipt)

    changes_restore = changes_sub.add_parser(
        "restore", help="Restore the verified backup from a commit receipt."
    )
    changes_restore.add_argument("model")
    changes_restore.add_argument("commit_id")
    changes_restore.add_argument(
        "--confirm", action="store_true", help="Required explicit restore confirmation."
    )
    changes_restore.add_argument("--json", action="store_true")
    changes_restore.set_defaults(func=_cmd_changes_restore)


# --------------------------------------------------------------------------- transactions
def _cmd_transactions_list(args: argparse.Namespace) -> int:
    core = _automation_core()
    try:
        journals = core.transactions.journals.list()
        if args.json:
            print(json.dumps([item.model_dump(mode="json") for item in journals], indent=2))
        elif not journals:
            print("(no transactions)")
        else:
            for item in journals:
                print(
                    f"{item.transaction_id}  {item.kind.value:7}  "
                    f"{item.phase.value:18}  {item.target_path}"
                )
        return 0
    finally:
        core.shutdown()


def _cmd_transactions_show(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        journal = core.transactions.journals.get(args.transaction_id)
        if args.json:
            print(journal.model_dump_json(indent=2))
        else:
            print(f"transaction {journal.transaction_id}")
            print(f"kind        {journal.kind.value}")
            print(f"phase       {journal.phase.value}")
            print(f"target      {journal.target_path}")
            print(f"before      {journal.expected_before_sha256}")
            print(f"after       {journal.desired_after_sha256}")
            print(f"cancellable {journal.cancellable}")
            if journal.rollback_artifact_id:
                print(f"rollback    {journal.rollback_artifact_id}")
            if journal.receipt_artifact_id:
                print(f"receipt     {journal.receipt_artifact_id}")
            if journal.error:
                print(f"error       {journal.error}")
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


# --------------------------------------------------------------------------- artifacts
def _artifact_service():
    from ifc_console.application.artifacts import ArtifactService

    store = _new_store()
    store.ensure_dirs()
    return ArtifactService(store.artifacts_dir)


def _artifact_retention_service():
    from ifc_console.application.artifacts import ArtifactService
    from ifc_console.application.retention import ArtifactRetentionService
    from ifc_console.audit import AuditLog

    store = _new_store()
    store.ensure_dirs()
    artifacts = ArtifactService(store.artifacts_dir)
    audit = AuditLog(store.sessions_dir, store.settings.sessions.retention)
    audit.start({"interface": "cli", "command": "artifacts"})
    return (
        ArtifactRetentionService(
            artifacts,
            store.jobs_dir,
            batches_root=store.batches_dir,
            workflows_root=store.workflows_dir,
            default_retention_days=store.settings.automation.artifact_retention_days,
        ),
        audit,
    )


def _cmd_artifacts_list(args: argparse.Namespace) -> int:
    refs = _artifact_service().list(limit=args.limit)
    if args.json:
        print(json.dumps([ref.model_dump(mode="json") for ref in refs], indent=2))
    elif not refs:
        print("(no artifacts)")
    else:
        for ref in refs:
            print(f"{ref.artifact_id}  {ref.size_bytes:8}  {ref.name}")
    return 0


def _cmd_artifacts_show(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    try:
        ref = _artifact_service().get(args.artifact_id)
        if args.json:
            print(ref.model_dump_json(indent=2))
        else:
            print(f"artifact  {ref.artifact_id}")
            print(f"name      {ref.name}")
            print(f"type      {ref.media_type}")
            print(f"size      {ref.size_bytes}")
            print(f"producer  {ref.producer}")
        return 0
    except ToolError as exc:
        return _tool_error(exc)


def _cmd_artifacts_export(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    try:
        target = _artifact_service().export(
            args.artifact_id, Path(args.path), overwrite=args.overwrite
        )
        print(target)
        return 0
    except ToolError as exc:
        return _tool_error(exc)


def _cmd_artifacts_pin(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    service, audit = _artifact_retention_service()
    try:
        ref = service.pin(args.artifact_id)
        audit.record("artifact_pinned", artifact_id=ref.artifact_id)
        print(f"pinned {ref.artifact_id}")
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        audit.end()


def _cmd_artifacts_unpin(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    service, audit = _artifact_retention_service()
    try:
        removed = service.unpin(args.artifact_id)
        audit.record("artifact_unpinned", artifact_id=args.artifact_id, pin_existed=removed)
        print("unpinned" if removed else "not pinned")
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        audit.end()


def _cmd_artifacts_gc(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    service, audit = _artifact_retention_service()
    try:
        plan = service.plan(older_than_days=args.older_than_days)
        audit.record(
            "artifact_gc_planned",
            cutoff=plan.cutoff.isoformat(),
            candidate_count=plan.candidate_count,
            candidate_bytes=plan.candidate_bytes,
        )
        if args.apply:
            result = service.collect(plan, confirm=args.confirm)
            audit.record(
                "artifact_gc_completed",
                deleted_count=result.deleted_count,
                deleted_bytes=result.deleted_bytes,
                deleted_ids=list(result.deleted_ids),
            )
            if args.json:
                print(result.model_dump_json(indent=2))
            else:
                print(f"deleted {result.deleted_count} artifact(s), {result.deleted_bytes} byte(s)")
            return 0
        if args.json:
            print(plan.model_dump_json(indent=2))
        else:
            print(f"scanned    {plan.scanned_count}")
            print(f"retained   {plan.retained_count}")
            print(f"candidates {plan.candidate_count}")
            print(f"bytes      {plan.candidate_bytes}")
            for warning in plan.warnings:
                print(f"warning    {warning}")
            if plan.candidate_count:
                print("dry-run only; use --apply --confirm after reviewing the JSON plan")
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        audit.end()


# --------------------------------------------------------------------------- changes
def _change_value(args: argparse.Namespace) -> Any:
    if args.json_value is None:
        return args.plain_value
    try:
        value = json.loads(args.json_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--value-json is invalid JSON: {exc}") from exc
    if value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("--value-json must be a string, number, boolean, or null")
    return value


def _print_change_set(record: Any, *, as_json: bool) -> None:
    if as_json:
        print(record.model_dump_json(indent=2))
        return
    print(f"change set  {record.change_set_id}")
    print(f"revision    {record.change_set.revision.revision_id}")
    print(f"source      {record.change_set.source.path}")
    for change in record.change_set.changes:
        if change.kind == "classification_assignment":
            print(
                f"change      {change.global_id}  {change.classification_name}."
                f"{change.identification}  assign {change.reference_name!r}"
            )
        else:
            print(
                f"change      {change.global_id}  {change.pset_name}.{change.property_name}  "
                f"{change.before!r} -> {change.after!r}"
            )


def _cmd_changes_preview(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core(Mode.ASK)

    async def run():
        model = Path(args.model).expanduser().resolve()
        core.start_audit()
        core.add_allowed_dir(model.parent)
        await core.open_model(model)
        return await core.transactions.preview_property_value(
            global_ids=args.global_ids,
            pset_name=args.pset_name,
            property_name=args.property_name,
            value=_change_value(args),
            create_missing=args.create_missing,
            nominal_type=args.nominal_type,
            expected_revision=args.expected_revision,
        )

    try:
        record = asyncio.run(run())
        _print_change_set(record, as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_changes_classify(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core(Mode.ASK)

    async def run():
        model = Path(args.model).expanduser().resolve()
        core.start_audit()
        core.add_allowed_dir(model.parent)
        await core.open_model(model)
        return await core.transactions.preview_classification_assignment(
            global_ids=args.global_ids,
            classification_name=args.classification_name,
            identification=args.identification,
            reference_name=args.reference_name,
            expected_revision=args.expected_revision,
        )

    try:
        record = asyncio.run(run())
        _print_change_set(record, as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_changes_show(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        record = core.transactions.get_change_set(args.change_set_id)
        _print_change_set(record, as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_changes_approve(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        core.start_audit()
        record = core.transactions.approve(
            args.change_set_id,
            approved_by=args.approved_by,
            reason=args.reason,
        )
        if args.json:
            print(record.model_dump_json(indent=2))
        else:
            print(f"approval    {record.approval_id}")
            print(f"change set  {record.approval.change_set_id}")
            print(f"approved by {record.approval.approved_by}")
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_changes_commit(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core(Mode.EDIT)

    async def run():
        model = Path(args.model).expanduser().resolve()
        core.start_audit()
        core.add_allowed_dir(model.parent)
        await core.open_model(model)
        submitted = await core.jobs.submit_commit(args.change_set_id, approval_id=args.approval_id)
        completed = await core.jobs.wait(submitted.job_id)
        if completed.state.value != "succeeded":
            failure = completed.failure
            raise ToolError(
                failure.code if failure else "JOB_CANCELLED",
                failure.message if failure else "the commit job did not complete",
                failure.hint if failure else "Inspect the durable job record.",
            )
        return core.transactions.get_commit(str(completed.summary["commit_id"]))

    try:
        record = asyncio.run(run())
        if args.json:
            print(record.model_dump_json(indent=2))
        else:
            print(f"commit      {record.commit_id}")
            print(f"target      {record.result.target_path}")
            print(f"checksum    {record.result.committed_sha256}")
            print(f"backup      {record.result.backup_artifact.artifact_id}")
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_changes_receipt(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        record = core.transactions.get_commit(args.commit_id)
        if args.json:
            print(record.model_dump_json(indent=2))
        else:
            print(f"commit      {record.commit_id}")
            print(f"change set  {record.result.change_set_id}")
            print(f"target      {record.result.target_path}")
            print(f"checksum    {record.result.committed_sha256}")
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_changes_restore(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core(Mode.EDIT)

    async def run():
        model = Path(args.model).expanduser().resolve()
        core.start_audit()
        core.add_allowed_dir(model.parent)
        await core.open_model(model)
        submitted = await core.jobs.submit_restore(args.commit_id, confirm=args.confirm)
        completed = await core.jobs.wait(submitted.job_id)
        if completed.state.value != "succeeded":
            failure = completed.failure
            raise ToolError(
                failure.code if failure else "JOB_CANCELLED",
                failure.message if failure else "the restore job did not complete",
                failure.hint if failure else "Inspect the durable job record.",
            )
        return core.transactions.get_restore(str(completed.summary["restore_id"]))

    try:
        record = asyncio.run(run())
        if args.json:
            print(record.model_dump_json(indent=2))
        else:
            print(f"restore     {record.restore_id}")
            print(f"target      {record.result.target_path}")
            print(f"checksum    {record.result.restored_sha256}")
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()
