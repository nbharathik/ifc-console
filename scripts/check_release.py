"""Verify source metadata before building or publishing a release."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _match(path: Path, pattern: str, label: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    if match is None:
        raise ValueError(f"could not read {label} from {path}")
    return match.group(1)


def _viewer_compatibility_range(version: str) -> str:
    match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", version)
    if match is None:
        raise ValueError(f"cannot derive viewer compatibility from version {version!r}")
    major, minor = (int(part) for part in match.groups())
    return f">={major}.{minor},<{major}.{minor + 1}"


def release_issues(root: Path, *, tag: str | None = None) -> tuple[str, list[str]]:
    core_init = root / "src" / "ifc_console" / "__init__.py"
    viewer_project = root / "packages" / "ifc-console-viewer" / "pyproject.toml"
    viewer_init = (
        root / "packages" / "ifc-console-viewer" / "src" / "ifc_console_viewer" / "__init__.py"
    )
    core_project = root / "pyproject.toml"
    changelog = root / "CHANGELOG.md"

    core_version = _match(core_init, r'^__version__\s*=\s*"([^"]+)"', "core version")
    viewer_version = _match(viewer_project, r'^version\s*=\s*"([^"]+)"', "viewer version")
    viewer_runtime_version = _match(
        viewer_init,
        r'^__version__\s*=\s*"([^"]+)"',
        "viewer runtime version",
    )
    core_python = _match(core_project, r'^requires-python\s*=\s*"([^"]+)"', "Python range")
    viewer_python = _match(viewer_project, r'^requires-python\s*=\s*"([^"]+)"', "Python range")
    viewer_requirement = _match(
        core_project,
        r'^\s*"ifc-console-viewer([^"]+)"\s*,?\s*$',
        "viewer dependency range",
    ).replace(" ", "")

    issues: list[str] = []
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
    expected_viewer_range = _viewer_compatibility_range(core_version)
    if viewer_requirement != expected_viewer_range:
        issues.append(
            f"viewer dependency range is {viewer_requirement!r}, "
            f"expected {expected_viewer_range!r} for core {core_version}"
        )
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
