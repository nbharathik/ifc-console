"""Verify source metadata before building or publishing a release."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRAS = {"agents", "validation", "all"}
# Removed in 0.2.0. Their reappearance means an old branch was merged back.
RETIRED_PATHS = (
    "packages/ifc-console-viewer",
    "packages/ifc-console-agents",
    "src/ifc_console/chat",
    "src/ifc_console/devkit",
    "src/ifc_console/credentials.py",
    "src/ifc_console/testing.py",
    "src/ifc_console/mcp/tools_skills.py",
    "src/ifc_console/integrations/langgraph.py",
)


def _match(path: Path, pattern: str, label: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    if match is None:
        raise ValueError(f"could not read {label} from {path}")
    return match.group(1)


def _extras(pyproject: Path) -> set[str]:
    text = pyproject.read_text(encoding="utf-8")
    section = re.search(
        r"^\[project\.optional-dependencies\]\s*$(.*?)(?=^\[)", text, flags=re.MULTILINE | re.DOTALL
    )
    if section is None:
        return set()
    return set(re.findall(r"^([A-Za-z0-9_-]+)\s*=\s*\[", section.group(1), flags=re.MULTILINE))


def release_issues(root: Path, *, tag: str | None = None) -> tuple[str, list[str]]:
    core_init = root / "src" / "ifc_console" / "__init__.py"
    core_project = root / "pyproject.toml"
    changelog = root / "CHANGELOG.md"

    core_version = _match(core_init, r'^__version__\s*=\s*"([^"]+)"', "core version")

    issues: list[str] = []
    extras = _extras(core_project)
    if extras != EXTRAS:
        issues.append(f"extras are {sorted(extras)}, expected exactly {sorted(EXTRAS)}")
    for relative in RETIRED_PATHS:
        if (root / relative).exists():
            issues.append(f"retired path is back: {relative}")
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
    parser.add_argument("--tag", help="release tag, for example v0.2.0")
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
