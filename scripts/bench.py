"""Benchmark harness: load, queries, contention, edits, tokens, imports.

Writes one JSON document so runs can be compared before and after a change.

    uv run python scripts/bench.py --out dev/bench/baseline.json
    uv run python scripts/bench.py --quick          # smoke run on one small model
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MODELS = (
    ROOT / "tests" / "model_126.ifc",
    ROOT / "tests" / "model_190.ifc",
    ROOT / "tests" / "NK_XX.2_GRB_WSB_FM_V2b260629.ifc",
)
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _percentiles(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return {
        "n": len(ordered),
        "p50_ms": round(statistics.median(ordered), 2),
        "p95_ms": round(p95, 2),
        "max_ms": round(ordered[-1], 2),
    }


def _rss_mb() -> float | None:
    from ifc_console.resources import process_memory

    rss = process_memory().get("rss_bytes")
    return round(rss / 1_048_576, 1) if rss else None


def bench_imports() -> dict[str, Any]:
    """Wall time of a cold import in a fresh interpreter, and what it dragged in."""
    probe = (
        "import sys, time; t = time.perf_counter(); import ifc_console.cli; "
        "ms = (time.perf_counter() - t) * 1000; "
        "agents = any(m.startswith('ifc_console.agents') for m in sys.modules); "
        "print(round(ms, 1), agents, len(sys.modules))"
    )
    runs = []
    for _ in range(3):
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        ).stdout.split()
        runs.append((float(out[0]), out[1] == "True", int(out[2])))
    return {
        "cli_import_ms": min(run[0] for run in runs),
        "agents_imported": any(run[1] for run in runs),
        "modules_loaded": runs[0][2],
    }


def bench_dependencies() -> dict[str, Any]:
    lock = ROOT / "uv.lock"
    locked = None
    if lock.is_file():
        locked = sum(
            1 for line in lock.read_text(encoding="utf-8").splitlines() if line == "[[package]]"
        )
    return {
        "installed_distributions": len(list(importlib.metadata.distributions())),
        "locked_packages": locked,
    }


async def bench_tokens(home: Path) -> dict[str, Any]:
    """Size of tools/list plus instructions, the tokens every client pays."""
    from ifc_console.app import AppCore
    from ifc_console.mcp.server import INSTRUCTIONS, build_mcp
    from ifc_console.settings import SettingsStore

    core = AppCore(SettingsStore(home=home, project_dir=home, env={}), viewer=True)
    try:
        started = time.perf_counter()
        mcp = build_mcp(core)
        build_ms = _ms(started)
        from ifc_console.mcp import catalog

        everything = await mcp.list_tools()
        tools = catalog.for_wire(everything, catalog.FULL)
        payload = [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]
        listing = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        biggest = sorted(
            ((len(json.dumps(item, separators=(",", ":"))), item["name"]) for item in payload),
            reverse=True,
        )[:12]
        sizes = {
            item["name"]: {
                "description": len(item.get("description", "")),
                "input": len(json.dumps(item.get("inputSchema"), separators=(",", ":"))),
            }
            for item in payload
        }

        def part(key: str) -> int:
            return sum(len(json.dumps(item.get(key), separators=(",", ":"))) for item in payload if key in item)

        lean = catalog.for_wire(everything, catalog.LEAN)
        lean_payload = [
            tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in lean
        ]
        lean_chars = len(json.dumps(lean_payload, separators=(",", ":"), ensure_ascii=False))
        return {
            "tool_count": len(payload),
            "list_chars": len(listing),
            "instructions_chars": len(INSTRUCTIONS),
            "total_chars": len(listing) + len(INSTRUCTIONS),
            "lean_chars": lean_chars + len(INSTRUCTIONS),
            "breakdown": {
                "descriptions": part("description"),
                "input_schemas": part("inputSchema"),
                "output_schemas": part("outputSchema"),
                "meta": part("_meta"),
                "annotations": part("annotations"),
            },
            "build_mcp_ms": build_ms,
            "largest_tools": [
                {"name": name, "chars": chars, **sizes[name]} for chars, name in biggest
            ],
        }
    finally:
        core.shutdown()


def _rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    for value in data.values():
        if (
            isinstance(value, list)
            and value
            and isinstance(value[0], dict)
            and "global_id" in value[0]
        ):
            return value
    return []


async def _timed(wb: Any, name: str, **kwargs: Any) -> tuple[float, dict[str, Any]]:
    started = time.perf_counter()
    envelope = await wb.call(name, **kwargs)
    return _ms(started), envelope


async def _cold_warm(wb: Any, name: str, **kwargs: Any) -> dict[str, Any]:
    cold, envelope = await _timed(wb, name, **kwargs)
    warm, _ = await _timed(wb, name, **kwargs)
    result: dict[str, Any] = {"cold_ms": cold, "warm_ms": warm, "ok": bool(envelope.get("ok"))}
    if not result["ok"]:
        result["error"] = (envelope.get("error") or {}).get("code")
    return result


async def bench_queries(wb: Any) -> tuple[dict[str, Any], list[str]]:
    results: dict[str, Any] = {}
    results["orient"] = await _cold_warm(wb, "orient")
    results["query_page_1"] = await _cold_warm(
        wb, "query_elements", query="IfcElement", limit=50, offset=0
    )
    results["query_page_5"] = await _cold_warm(
        wb, "query_elements", query="IfcElement", limit=50, offset=200
    )
    results["query_order_storey"] = await _cold_warm(
        wb, "query_elements", query="IfcElement", limit=50, order_by="storey"
    )
    results["query_3_properties"] = await _cold_warm(
        wb,
        "query_elements",
        query="IfcElement",
        limit=50,
        properties=["Pset_WallCommon.FireRating", "Pset_WallCommon.IsExternal", "Pset_DoorCommon.FireRating"],
    )
    results["search_text"] = await _cold_warm(wb, "search_elements", term="wall", limit=50)
    _, envelope = await _timed(wb, "query_elements", query="IfcElement", limit=100)
    guids = [row["global_id"] for row in _rows(envelope.get("data") or {})]
    if guids:
        results["get_element_50"] = await _cold_warm(wb, "get_element", global_ids=guids[:50])
        results["get_psets_100"] = await _cold_warm(wb, "get_psets", global_ids=guids[:100])
    return results, guids


def bench_plumbing(wb: Any) -> dict[str, Any]:
    from ifc_console.core.results import ok

    rows = [
        {"global_id": f"{i:022d}", "class": "IfcWall", "name": f"Wall {i}", "tag": str(i)}
        for i in range(500)
    ]
    started = time.perf_counter()
    for _ in range(20):
        ok({"rows": rows}, {"model": "bench"}, char_limit=1_000_000)
    fits = round(_ms(started) / 20, 2)
    started = time.perf_counter()
    for _ in range(20):
        ok({"rows": rows}, {"model": "bench"}, char_limit=5_000)
    overflow = round(_ms(started) / 20, 2)
    core = wb.core
    started = time.perf_counter()
    for i in range(1000):
        core.audit.record("bench", i=i)
    return {
        "ok_500_rows_ms": fits,
        "ok_500_rows_overflow_ms": overflow,
        "audit_1000_records_ms": _ms(started),
    }


async def bench_serialize(wb: Any) -> dict[str, Any]:
    session = wb.core.session
    started = time.perf_counter()
    payload = await session.run(lambda: session.ifc.to_string().encode("utf-8"), timeout=300)
    return {"to_string_ms": _ms(started), "bytes": len(payload)}


async def _probe(wb: Any, seconds: float) -> list[float]:
    latencies: list[float] = []
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        started = time.perf_counter()
        await asyncio.wait_for(wb.call("get_ifc_project_info"), 60)
        latencies.append((time.perf_counter() - started) * 1000)
        await asyncio.sleep(0.05)
    return latencies


async def bench_contention(model: Path, home: Path, seconds: float) -> dict[str, Any]:
    """Latency of a cached read while a long run is in flight."""
    from ifc_console.sdk import AsyncWorkbench

    out: dict[str, Any] = {}
    code = f"import time\ntime.sleep({seconds})"
    for label, mode in (("edit_run", "edit"), ("sandboxed_run", "ask")):
        wb = await AsyncWorkbench.create(model, mode=mode, home=home)
        try:
            await wb.call("get_ifc_project_info")
            baseline = await _probe(wb, 0.5)
            runner = asyncio.create_task(
                wb.call("execute_ifc_code", code=code, description="bench contention")
            )
            await asyncio.sleep(0.2)
            during = await _probe(wb, seconds)
            envelope = await runner
            out[label] = {
                "idle": _percentiles(baseline),
                "during_run": _percentiles(during),
                "run_ok": bool(envelope.get("ok")),
            }
            if not envelope.get("ok"):
                out[label]["run_error"] = (envelope.get("error") or {}).get("code")
        finally:
            await wb.aclose()
    return out


_EDITS = {
    "one_property": (
        "p = ifc.by_type('IfcProduct')[0]\np.Description = 'bench'"
    ),
    "create_5k_points": (
        "for i in range(5000):\n    ifc.create_entity('IfcCartesianPoint', Coordinates=(float(i), 0.0, 0.0))"
    ),
    "remove_200_products": (
        "for e in list(ifc.by_type('IfcProduct'))[:200]:\n    ifc.remove(e)"
    ),
}


async def bench_edits(model: Path, home: Path) -> dict[str, Any]:
    from ifc_console.sdk import AsyncWorkbench

    wb = await AsyncWorkbench.create(model, mode="edit", home=home)
    out: dict[str, Any] = {}
    try:
        for label, code in _EDITS.items():
            started = time.perf_counter()
            envelope = await wb.call("execute_ifc_code", code=code, description=f"bench {label}")
            entry: dict[str, Any] = {"ms": _ms(started), "ok": bool(envelope.get("ok"))}
            if not entry["ok"]:
                entry["error"] = (envelope.get("error") or {}).get("code")
            out[label] = entry
    finally:
        await wb.aclose()
    return out


async def bench_model(model: Path, args: argparse.Namespace) -> dict[str, Any]:
    from ifc_console.sdk import AsyncWorkbench

    result: dict[str, Any] = {"file": model.name, "size_mb": round(model.stat().st_size / 1e6, 2)}
    with tempfile.TemporaryDirectory(prefix="ifc-bench-") as raw:
        home = Path(raw)
        rss_before = _rss_mb()
        started = time.perf_counter()
        wb = await AsyncWorkbench.create(model, mode="ask", home=home)
        try:
            result["load_ms"] = _ms(started)
            result["rss_mb"] = {"before": rss_before, "after_load": _rss_mb()}
            result["queries"], _ = await bench_queries(wb)
            result["plumbing"] = bench_plumbing(wb)
            result["serialize"] = await bench_serialize(wb)
            result["rss_mb"]["after_queries"] = _rss_mb()
        finally:
            await wb.aclose()
        if not args.quick:
            result["edits"] = await bench_edits(model, home)
        if not args.quick and not args.skip_contention:
            result["contention"] = await bench_contention(model, home, args.contention_seconds)
    return result


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from ifc_console import __version__

    report: dict[str, Any] = {
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "imports": bench_imports(),
        "dependencies": bench_dependencies(),
    }
    with tempfile.TemporaryDirectory(prefix="ifc-bench-tokens-") as raw:
        report["tokens"] = await bench_tokens(Path(raw))
    models = [MODELS[1]] if args.quick else [Path(m) for m in (args.models or MODELS)]
    report["models"] = [await bench_model(model, args) for model in models if model.is_file()]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write the JSON report here")
    parser.add_argument("--models", nargs="*", help="IFC files to benchmark")
    parser.add_argument("--quick", action="store_true", help="one small model, no edits")
    parser.add_argument("--skip-contention", action="store_true")
    parser.add_argument("--contention-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    report = asyncio.run(run(args))
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
