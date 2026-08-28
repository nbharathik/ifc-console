"""Verify source metadata before building or publishing a release."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE_AGENT_SHIMS = (
    "src/ifc_console/agents/__init__.py",
    "src/ifc_console/chat/__init__.py",
    "src/ifc_console/credentials.py",
    "src/ifc_console/devkit/__init__.py",
    "src/ifc_console/integrations/langgraph.py",
    "src/ifc_console/mcp/tools_skills.py",
    "src/ifc_console/testing.py",
)


def _match(path: Path, pattern: str, label: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    if match is None:
        raise ValueError(f"could not read {label} from {path}")
    return match.group(1)


def _compatibility_range(version: str) -> str:
    match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", version)
    if match is None:
        raise ValueError(f"cannot derive compatibility range from version {version!r}")
    major, minor = (int(part) for part in match.groups())
    return f">={major}.{minor},<{major}.{minor + 1}"


def _agent_compatibility_range(version: str) -> str:
    """Keep the active companion at least as new as the matching core release."""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:\D|$)", version)
    if match is None:
        raise ValueError(f"cannot derive agent compatibility range from version {version!r}")
    major, minor, _patch = (int(part) for part in match.groups())
    return f">={version},<{major}.{minor + 1}"


def release_issues(root: Path, *, tag: str | None = None) -> tuple[str, list[str]]:
    core_init = root / "src" / "ifc_console" / "__init__.py"
    agent_project = root / "packages" / "ifc-console-agents" / "pyproject.toml"
    agent_init = (
        root / "packages" / "ifc-console-agents" / "src" / "ifc_console_agents" / "__init__.py"
    )
    viewer_project = root / "packages" / "ifc-console-viewer" / "pyproject.toml"
    viewer_init = (
        root / "packages" / "ifc-console-viewer" / "src" / "ifc_console_viewer" / "__init__.py"
    )
    core_project = root / "pyproject.toml"
    changelog = root / "CHANGELOG.md"

    core_version = _match(core_init, r'^__version__\s*=\s*"([^"]+)"', "core version")
    agent_version = _match(agent_init, r'^__version__\s*=\s*"([^"]+)"', "agent version")
    viewer_version = _match(viewer_project, r'^version\s*=\s*"([^"]+)"', "viewer version")
    viewer_runtime_version = _match(
        viewer_init,
        r'^__version__\s*=\s*"([^"]+)"',
        "viewer runtime version",
    )
    core_python = _match(core_project, r'^requires-python\s*=\s*"([^"]+)"', "Python range")
    agent_python = _match(agent_project, r'^requires-python\s*=\s*"([^"]+)"', "Python range")
    viewer_python = _match(viewer_project, r'^requires-python\s*=\s*"([^"]+)"', "Python range")
    shim_requirement = _match(
        viewer_project,
        r'^\s*dependencies\s*=\s*\[\s*"ifc-console([^"]+)"\s*\]\s*$',
        "compatibility shim dependency range",
    ).replace(" ", "")
    agent_requirement = _match(
        agent_project,
        r'"ifc-console((?:[<>=!~][^"]*)+)"',
        "agent core dependency range",
    ).replace(" ", "")
    graph_bridge_requirement = _match(
        core_project,
        r'^\s*graph\s*=\s*\[\s*"ifc-console-agents\[graph\]([^"]+)"\s*\]\s*$',
        "legacy graph bridge range",
    ).replace(" ", "")
    keys_bridge_requirement = _match(
        core_project,
        r'^\s*keys\s*=\s*\[\s*"ifc-console-agents([^"]+)"\s*\]\s*$',
        "legacy keys bridge range",
    ).replace(" ", "")

    issues: list[str] = []
    if agent_version != core_version:
        issues.append(
            f"package versions differ: ifc-console={core_version}, "
            f"ifc-console-agents={agent_version}"
        )
    if viewer_version != core_version:
        issues.append(
            f"package versions differ: ifc-console={core_version}, "
            f"ifc-console-viewer={viewer_version}"
        )
    if viewer_runtime_version != viewer_version:
        issues.append(
            f"viewer versions differ: metadata={viewer_version}, runtime={viewer_runtime_version}"
        )
    if viewer_python != core_python:
        issues.append(
            f"Python ranges differ: ifc-console={core_python}, ifc-console-viewer={viewer_python}"
        )
    if agent_python != core_python:
        issues.append(
            f"Python ranges differ: ifc-console={core_python}, "
            f"ifc-console-agents={agent_python}"
        )
    expected_agent_range = _agent_compatibility_range(core_version)
    if agent_requirement != expected_agent_range:
        issues.append(
            f"agent core dependency range is {agent_requirement!r}, "
            f"expected {expected_agent_range!r} for core {core_version}"
        )
    for label, bridge_requirement in (
        ("graph", graph_bridge_requirement),
        ("keys", keys_bridge_requirement),
    ):
        if bridge_requirement != expected_agent_range:
            issues.append(
                f"legacy {label} bridge range is {bridge_requirement!r}, "
                f"expected {expected_agent_range!r} for core {core_version}"
            )
    expected_shim_range = _compatibility_range(core_version)
    if shim_requirement != expected_shim_range:
        issues.append(
            f"compatibility shim dependency range is {shim_requirement!r}, "
            f"expected {expected_shim_range!r} for core {core_version}"
        )
    if (viewer_init.parent / "static").exists():
        issues.append("ifc-console-viewer compatibility shim must not contain static assets")
    expected_shims = {root / relative for relative in CORE_AGENT_SHIMS}
    missing_shims = sorted(
        path.relative_to(root).as_posix() for path in expected_shims if not path.is_file()
    )
    if missing_shims:
        issues.append(f"one-release agent compatibility shims are missing: {missing_shims}")
    legacy_roots = (
        root / "src" / "ifc_console" / "agents",
        root / "src" / "ifc_console" / "chat",
        root / "src" / "ifc_console" / "devkit",
    )
    legacy_files = {
        path
        for legacy_root in legacy_roots
        if legacy_root.exists()
        for path in legacy_root.rglob("*.py")
    }
    legacy_files.update(
        {
            root / "src" / "ifc_console" / "credentials.py",
            root / "src" / "ifc_console" / "integrations" / "langgraph.py",
            root / "src" / "ifc_console" / "mcp" / "tools_skills.py",
            root / "src" / "ifc_console" / "testing.py",
        }
    )
    unexpected_legacy = [
        path.relative_to(root).as_posix()
        for path in sorted(legacy_files - expected_shims)
    ]
    if unexpected_legacy:
        issues.append(
            f"agent implementation exceeds the compatibility-shim allowlist: "
            f"{unexpected_legacy}"
        )
    if (agent_init.parents[1] / "ifc_console").exists():
        issues.append("ifc-console-agents must not install files into the ifc_console namespace")
    if tag is not None and tag != f"v{core_version}":
        issues.append(f"release tag {tag!r} must be exactly 'v{core_version}'")
    if not changelog.is_file():
        issues.append("CHANGELOG.md is missing")
    else:
        heading = re.compile(
            rf"^##\s+\[?{re.escape(core_version)}\]?\s+-\s+([^\r\n]+)$",
            re.MULTILINE,
        )
        match = heading.search(changelog.read_text(encoding="utf-8"))
        if match is None:
            issues.append(f"CHANGELOG.md has no {core_version} release heading")
        elif tag is not None:
            release_date = match.group(1).strip()
            try:
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date) is None:
                    raise ValueError
                date.fromisoformat(release_date)
            except ValueError:
                issues.append(
                    f"CHANGELOG.md must replace {release_date!r} with a YYYY-MM-DD "
                    "release date before publishing"
                )
    return core_version, issues


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="release tag, for example v0.1.4")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        version, issues = release_issues(ROOT, tag=args.tag)
    except (OSError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    if issues:
        for issue in issues:
            print(f"FAIL: {issue}")
        return 1
    suffix = f" for tag {args.tag}" if args.tag else ""
    print(f"ok: release metadata agrees on {version}{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
