"""Admin commands: settings, recents, sessions, knowledge, keys, plugins."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ifc_console.cli.common import _agent_module, _new_store


def add_settings_parser(sub: argparse._SubParsersAction) -> None:
    st = sub.add_parser("settings", help="Inspect and edit user settings.")
    st_sub = st.add_subparsers(dest="settings_cmd", required=True)
    st_list = st_sub.add_parser("list")
    st_list.add_argument("--sources", action="store_true")
    st_list.add_argument("--json", action="store_true")
    st_list.set_defaults(func=_cmd_settings_list)
    st_get = st_sub.add_parser("get")
    st_get.add_argument("key")
    st_get.set_defaults(func=_cmd_settings_get)
    st_set = st_sub.add_parser("set")
    st_set.add_argument("key")
    st_set.add_argument("value")
    st_set.set_defaults(func=_cmd_settings_set)
    st_unset = st_sub.add_parser("unset")
    st_unset.add_argument("key")
    st_unset.set_defaults(func=_cmd_settings_unset)
    st_path = st_sub.add_parser("path")
    st_path.set_defaults(func=_cmd_settings_path)


def add_recents_parser(sub: argparse._SubParsersAction) -> None:
    rec = sub.add_parser("recents", help="Recently opened models.")
    rec_sub = rec.add_subparsers(dest="recents_cmd", required=True)
    rec_list = rec_sub.add_parser("list")
    rec_list.add_argument("--json", action="store_true")
    rec_list.set_defaults(func=_cmd_recents_list)
    rec_clear = rec_sub.add_parser("clear")
    rec_clear.set_defaults(func=_cmd_recents_clear)


def add_knowledge_parser(sub: argparse._SubParsersAction) -> None:
    kb = sub.add_parser("knowledge", help="The offline IFC reference and project document index.")
    kb_sub = kb.add_subparsers(dest="knowledge_cmd", required=True)
    kb_ingest = kb_sub.add_parser(
        "ingest",
        help="Index project documents (md, txt, pdf, png/jpg) for retrieval.",
    )
    kb_ingest.add_argument("paths", nargs="+", type=Path, help="Documents or folders of them.")
    kb_ingest.add_argument(
        "--replace",
        action="store_true",
        help="Reset the corpus to exactly these documents.",
    )
    kb_ingest.set_defaults(func=_cmd_knowledge_ingest)
    kb_build = kb_sub.add_parser("build", help="Build or rebuild the index.")
    kb_build.add_argument("--force", action="store_true", help="Rebuild even if it exists.")
    kb_build.set_defaults(func=_cmd_knowledge_build)
    kb_status = kb_sub.add_parser("status", help="Show where the index is and what it holds.")
    kb_status.add_argument("--json", action="store_true")
    kb_status.set_defaults(func=_cmd_knowledge_status)
    kb_search = kb_sub.add_parser("search", help="Search the index from the shell.")
    kb_search.add_argument("query", nargs="+")
    kb_search.add_argument("--kind", action="append", default=None, metavar="KIND")
    kb_search.add_argument("--schema", default=None)
    kb_search.add_argument("--limit", type=int, default=10)
    kb_search.add_argument("--json", action="store_true")
    kb_search.set_defaults(func=_cmd_knowledge_search)


def add_keys_parser(sub: argparse._SubParsersAction) -> None:
    keys = sub.add_parser(
        "keys",
        help="Provider API keys in the system keyring (never plain text).",
    )
    keys_sub = keys.add_subparsers(dest="keys_cmd", required=True)
    keys_set = keys_sub.add_parser("set", help="Store or replace one provider key.")
    keys_set.add_argument("provider", help="openai, anthropic, openrouter, or local")
    keys_set.add_argument(
        "--key", default=None, help="The key; omitted, it is prompted for without echo."
    )
    keys_set.set_defaults(func=_cmd_keys_set)
    keys_list = keys_sub.add_parser("list", help="Providers with a stored key.")
    keys_list.set_defaults(func=_cmd_keys_list)
    keys_delete = keys_sub.add_parser("delete", help="Remove one stored key.")
    keys_delete.add_argument("provider")
    keys_delete.set_defaults(func=_cmd_keys_delete)


def add_plugins_parser(sub: argparse._SubParsersAction) -> None:
    plugins = sub.add_parser("plugins", help="Inspect trusted operation plugins.")
    plugins_sub = plugins.add_subparsers(dest="plugins_cmd", required=True)
    plugins_list = plugins_sub.add_parser(
        "list", help="List discovered plugins without importing their code."
    )
    plugins_list.add_argument("--json", action="store_true")
    plugins_list.set_defaults(func=_cmd_plugins_list)
    plugins_doctor = plugins_sub.add_parser(
        "doctor", help="Load configured plugins and validate registration."
    )
    plugins_doctor.add_argument("--json", action="store_true")
    plugins_doctor.set_defaults(func=_cmd_plugins_doctor)


def add_sessions_parser(sub: argparse._SubParsersAction) -> None:
    ses = sub.add_parser("sessions", help="Audit-log sessions.")
    ses_sub = ses.add_subparsers(dest="sessions_cmd", required=True)
    ses_list = ses_sub.add_parser("list")
    ses_list.add_argument("--json", action="store_true")
    ses_list.set_defaults(func=_cmd_sessions_list)
    ses_show = ses_sub.add_parser("show")
    ses_show.add_argument("id")
    ses_show.set_defaults(func=_cmd_sessions_show)
    ses_verify = ses_sub.add_parser("verify", help="Verify an audit session's hash chain.")
    ses_verify.add_argument("id")
    ses_verify.add_argument("--json", action="store_true")
    ses_verify.set_defaults(func=_cmd_sessions_verify)
    ses_clear = ses_sub.add_parser("clear")
    ses_clear.set_defaults(func=_cmd_sessions_clear)


# --------------------------------------------------------------------------- settings/recents/sessions
def _cmd_settings_list(args: argparse.Namespace) -> int:
    store = _new_store()
    flat = store.flat()
    if args.json:
        payload = (
            {k: {"value": v, "source": store.provenance.get(k, "default")} for k, v in flat.items()}
            if args.sources
            else flat
        )
        print(json.dumps(payload, indent=2, default=str))
        return 0
    width = max(len(k) for k in flat)
    for key, value in sorted(flat.items()):
        line = f"{key:<{width}}  {json.dumps(value, default=str)}"
        if args.sources:
            line += f"    [{store.provenance.get(key, 'default')}]"
        print(line)
    for w in store.warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


def _cmd_settings_get(args: argparse.Namespace) -> int:
    store = _new_store()
    try:
        print(json.dumps(store.get(args.key), default=str))
        return 0
    except KeyError:
        print(f"error: unknown setting {args.key!r}", file=sys.stderr)
        return 3


def _cmd_settings_set(args: argparse.Namespace) -> int:
    store = _new_store()
    store.ensure_dirs()
    try:
        value = store.set_user(args.key, args.value)
    except KeyError:
        print(f"error: unknown setting {args.key!r}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"error: invalid value: {exc}", file=sys.stderr)
        return 3
    print(f"{args.key} = {json.dumps(value, default=str)}  (written to {store.user_file})")
    return 0


def _cmd_settings_unset(args: argparse.Namespace) -> int:
    store = _new_store()
    store.unset_user(args.key)
    print(f"{args.key} removed from {store.user_file}")
    return 0


def _cmd_settings_path(_args: argparse.Namespace) -> int:
    print(_new_store().user_file)
    return 0


def _cmd_recents_list(args: argparse.Namespace) -> int:
    store = _new_store()
    from ifc_console.recents import RecentsStore

    entries = RecentsStore(store.recents_file).entries()
    if args.json:
        print(json.dumps(entries, indent=2))
        return 0
    if not entries:
        print("(no recent models)")
        return 0
    for e in entries:
        size_mb = e.get("size_bytes", 0) / 1_048_576
        print(
            f"{e['path']}  ({size_mb:.1f} MB, {e.get('schema', '?')}, "
            f"last {e.get('last_opened', '?')})"
        )
    return 0


def _cmd_recents_clear(_args: argparse.Namespace) -> int:
    store = _new_store()
    from ifc_console.recents import RecentsStore

    RecentsStore(store.recents_file).clear()
    print("recents cleared")
    return 0


def _cmd_sessions_list(args: argparse.Namespace) -> int:
    store = _new_store()
    from ifc_console.audit import AuditLog

    ids = AuditLog(store.sessions_dir).list_sessions()
    if args.json:
        print(json.dumps(ids, indent=2))
    else:
        print("\n".join(ids) if ids else "(no sessions)")
    return 0


def _cmd_sessions_show(args: argparse.Namespace) -> int:
    store = _new_store()
    from ifc_console.audit import AuditLog

    records = AuditLog(store.sessions_dir).read_session(args.id)
    if not records:
        print(f"error: no session {args.id!r}", file=sys.stderr)
        return 4
    for record in records:
        print(json.dumps(record, ensure_ascii=False, default=str))
    return 0


def _cmd_sessions_verify(args: argparse.Namespace) -> int:
    store = _new_store()
    from ifc_console.audit import AuditLog

    result = AuditLog(store.sessions_dir).verify_session(args.id)
    if args.json:
        print(result.model_dump_json(indent=2))
    elif result.valid:
        print(f"valid: {result.event_count} chained event(s)")
    else:
        print(f"invalid: {result.error}", file=sys.stderr)
    return 0 if result.valid else 5


def _cmd_sessions_clear(_args: argparse.Namespace) -> int:
    store = _new_store()
    from ifc_console.audit import AuditLog

    n = AuditLog(store.sessions_dir).clear()
    print(f"removed {n} session(s)")
    return 0


# --------------------------------------------------------------------------- knowledge
def _knowledge():
    from ifc_console.knowledge import KnowledgeBase

    store = _new_store()
    store.ensure_dirs()
    return KnowledgeBase(store.home, schemas=tuple(store.settings.knowledge.schemas))


def _cmd_knowledge_build(args: argparse.Namespace) -> int:
    kb = _knowledge()
    print(f"building the reference index at {kb.path} …")
    info = kb.build(force=args.force)
    if not info.get("built"):
        print("already built; --force rebuilds it")
        return 0
    counts = ", ".join(f"{k} {v}" for k, v in sorted(info["counts"].items()))
    print(f"indexed {info['total']} records ({counts}), {info['size_bytes'] / 1e6:.1f} MB")
    return 0


def _cmd_knowledge_status(args: argparse.Namespace) -> int:
    from ifc_console.knowledge.project import ProjectKnowledge

    kb = _knowledge()
    stats = kb.stats()
    project = ProjectKnowledge(_new_store().project_dir)
    project_stats = project.stats()
    if args.json:
        print(
            json.dumps(
                {"path": str(kb.path), **stats, "project": project_stats},
                indent=2,
                default=str,
            )
        )
        return 0
    if not stats["ready"]:
        print(f"not built ({kb.path})\nbuild it with: ifc-console knowledge build")
        return 1
    counts = ", ".join(f"{k} {v}" for k, v in sorted(stats["counts"].items()))
    print(f"index    {kb.path}")
    print(f"records  {stats['total']} ({counts})")
    print(f"search   {stats['search']}   ifcopenshell {stats.get('ifcopenshell', '?')}")
    if project_stats.get("ready"):
        print(
            f"project  {project_stats['path']} "
            f"({project_stats['documents']} documents, {project_stats.get('total', 0)} records)"
        )
    else:
        print("project  no documents ingested (ifc-console knowledge ingest <paths>)")
    return 0


def _cmd_knowledge_ingest(args: argparse.Namespace) -> int:
    from ifc_console.knowledge.project import ProjectKnowledge
    from ifc_console.mcp.envelope import ToolError

    store = _new_store()
    project = ProjectKnowledge(store.project_dir)
    try:
        report = project.ingest([Path(p) for p in args.paths], replace=args.replace)
    except ToolError as exc:
        print(f"{exc.code}: {exc}")
        if exc.hint:
            print(exc.hint)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, default=str))
        return 0
    print(f"indexed {report['records']} chunks from {report['documents']} documents")
    print(f"index   {report['index']}")
    for entry in report["files"]:
        note = (
            " (visual pages available; no searchable text)"
            if entry.get("no_text") and entry.get("media") == "pdf"
            else " (no extractable text)"
            if entry.get("no_text")
            else ""
        )
        print(f"  {entry['media']:8} {entry['path']} ({entry['records']} chunks){note}")
    for key, label in (
        ("skipped_unsupported", "skipped"),
        ("dropped_missing", "dropped, missing"),
    ):
        for item in report.get(key, ()):
            print(f"  {label}: {item}")
    if report.get("instruction_like_chunks"):
        print(
            f"note: {report['instruction_like_chunks']} chunks look like instructions; "
            "they are stored as data and never followed"
        )
    return 0


def _cmd_knowledge_search(args: argparse.Namespace) -> int:
    kb = _knowledge()
    if not kb.ready:
        print("the index is not built; run: ifc-console knowledge build")
        return 1
    hits = kb.search(
        " ".join(args.query),
        kind=tuple(args.kind) if args.kind else None,
        schema=args.schema,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(hits, indent=2, default=str))
        return 0
    if not hits:
        print("no matches")
        return 1
    for hit in hits:
        schema = f" [{hit['schema']}]" if hit.get("schema") else ""
        print(f"{hit['kind']:9} {hit['name']}{schema}\n    {hit['summary'][:100]}")
    return 0


# --------------------------------------------------------------------------- keys
def _cmd_keys_set(args: argparse.Namespace) -> int:
    credentials = _agent_module("credentials")
    from ifc_console.mcp.envelope import ToolError

    key = args.key
    if key is None:
        import getpass

        if sys.stdin.isatty():
            key = getpass.getpass(f"API key for {args.provider}: ")
        else:
            key = sys.stdin.readline()
    store = _new_store()
    try:
        credentials.set_api_key(store.home, args.provider.lower(), key or "")
    except ToolError as exc:
        print(f"{exc.code}: {exc}")
        if exc.hint:
            print(exc.hint)
        return 1
    print(f"key for {args.provider} stored in the system keyring")
    return 0


def _cmd_keys_list(args: argparse.Namespace) -> int:
    credentials = _agent_module("credentials")

    store = _new_store()
    if not credentials.keyring_available():
        print("the bundled keyring package is missing; reinstall or upgrade ifc-console")
        print("keys currently come from environment variables only")
        return 1
    providers = credentials.stored_providers(store.home)
    if not providers:
        print("no keys stored; add one with: ifc-console keys set <provider>")
        return 0
    for provider in providers:
        print(f"{provider}: stored in the system keyring")
    return 0


def _cmd_keys_delete(args: argparse.Namespace) -> int:
    credentials = _agent_module("credentials")
    from ifc_console.mcp.envelope import ToolError

    store = _new_store()
    try:
        existed = credentials.delete_api_key(store.home, args.provider.lower())
    except ToolError as exc:
        print(f"{exc.code}: {exc}")
        if exc.hint:
            print(exc.hint)
        return 1
    print(f"key for {args.provider} removed" if existed else f"no stored key for {args.provider}")
    return 0


# --------------------------------------------------------------------------- plugins
def _print_plugin_records(records, *, as_json: bool) -> None:
    payload = [record.model_dump(mode="json") for record in records]
    if as_json:
        print(json.dumps({"plugins": payload}, indent=2))
        return
    if not payload:
        print("no ifc-console plugins are installed")
        return
    for record in payload:
        version = (record.get("manifest") or {}).get("version")
        suffix = f" {version}" if version else ""
        detail = ", ".join(record.get("operations") or ())
        if record.get("error"):
            detail = record["error"]
        print(f"{record['name']}{suffix}: {record['status']}" + (f" ({detail})" if detail else ""))


def _cmd_plugins_list(args: argparse.Namespace) -> int:
    from ifc_console.plugins import PluginManager

    store = _new_store()
    allow = {name.strip().lower() for name in store.settings.plugins.allow if name.strip()}
    records = PluginManager().inventory(
        enabled=store.settings.plugins.enabled,
        allow=allow,
    )
    _print_plugin_records(records, as_json=args.json)
    return 0


def _cmd_plugins_doctor(args: argparse.Namespace) -> int:
    from ifc_console.app import AppCore
    from ifc_console.application.operations import build_operations

    store = _new_store()
    core = AppCore(store, transport="embedded")
    core.start_audit()
    try:
        build_operations(core)
        records = core.plugins.records
        _print_plugin_records(records, as_json=args.json)
        return 1 if any(record.status in {"error", "missing"} for record in records) else 0
    finally:
        core.shutdown()
