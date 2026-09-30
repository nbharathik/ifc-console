"""Release guard for the ifc-console wheel and source archive.

The main wheel must carry the complete reviewed browser bundles and the agent
code while staying within its release budget. The wheel and source archive must
match the source version. Run after building the package.
"""

from __future__ import annotations

import argparse
import re
import shutil
import stat
import sys
import tarfile
import zipfile
from email.message import Message
from email.parser import Parser
from pathlib import Path, PurePosixPath, PureWindowsPath

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
MAIN_LIMIT_MB = 3.0
STATIC_LIMIT_MB = 9.5
AGENT_STATIC_LIMIT_MB = 1.0
MAIN_SDIST_LIMIT_MB = 4.0
EXTRAS = {"agents", "validation", "all"}
REQUIRED_ASSETS = (
    "index.html",
    "app.css",
    "app.js",
    "batching.js",
    "dom.js",
    "file_actions.js",
    "frames.js",
    "measure_view.js",
    "model_tree.js",
    "parsed_cache.js",
    "parser_bridge.js",
    "properties_view.js",
    "saved_views.js",
    "scene_parts.js",
    "search_panel.js",
    "session.js",
    "ui_state.js",
    "workspace_layout.js",
    "viewer_component.js",
    "delta.js",
    "measure_math.js",
    "parser.js",
    "themes.css",
    "worker.js",
    "vendor/OrbitControls.js",
    "vendor/three.core.min.js",
    "vendor/three.module.min.js",
    "vendor/web-ifc-api.js",
    "vendor/web-ifc.wasm",
    "vendor/VENDORED.md",
    "vendor/LICENSE.three.txt",
    "vendor/LICENSE.web-ifc.md",
)
REQUIRED_AGENT_ASSETS = (
    "chat-page.js",
    "chat.css",
    "chat.html",
    "chat.js",
    "chat_flow.js",
    "chat_history.js",
    "chat_markdown.js",
    "chat_memory.js",
    "chat_sidebar.js",
    "chat_workspace.js",
    "sse.js",
    "workflows-page.js",
    "workflows.css",
    "workflows.html",
    "workflows.js",
    "workflows_model.js",
)
# Packages that must stay behind an extra, so the base install stays small.
OPTIONAL_ONLY = {
    "agent-client-protocol",
    "ifctester",
    "keyring",
    "langchain",
    "langgraph",
    "pymupdf",
    "pypdf",
    "pypdfium2",
}
# Removed in 0.2.0; a wheel that ships any of them was built from a stale tree.
RETIRED_PREFIXES = (
    "ifc_console/chat/",
    "ifc_console/devkit/",
    "ifc_console_agents/",
    "ifc_console_viewer/",
)
RETIRED_FILES = {
    "ifc_console/credentials.py",
    "ifc_console/testing.py",
    "ifc_console/mcp/tools_skills.py",
    "ifc_console/integrations/langgraph.py",
}
_VIEWER_STATIC_PREFIX = "ifc_console/viewer/static/"
_AGENT_STATIC_PREFIX = "ifc_console/agents/static/"
_EXCLUDED_SOURCE_PARTS = frozenset(
    {"dev", "dist", ".github", ".tmp", ".vscode", "packages", "site"}
)
_WINDOWS_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class CheckError(RuntimeError):
    pass


def _source_version() -> str:
    source = (ROOT / "src" / "ifc_console" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', source, flags=re.MULTILINE)
    if match is None:
        raise CheckError("cannot read the source package version")
    return match.group(1)


def _source_python_range() -> str:
    source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^requires-python\s*=\s*"([^"]+)"', source, flags=re.MULTILINE)
    if match is None:
        raise CheckError("cannot read the source Python range")
    return match.group(1)


def _canonical_python_range(value: str) -> tuple[str, ...]:
    return tuple(sorted(part.strip().replace(" ", "") for part in value.split(",") if part))


def _one(dist: Path, pattern: str, label: str) -> Path:
    matches = sorted(dist.glob(pattern))
    if len(matches) != 1:
        names = ", ".join(path.name for path in matches) or "none"
        raise CheckError(f"expected one {label} matching {pattern}; found {names}")
    return matches[0]


def _wheel_metadata(path: Path) -> Message:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise CheckError(f"{path.name} has {len(names)} METADATA files")
        raw = archive.read(names[0]).decode("utf-8")
    return Parser().parsestr(raw)


def _check_metadata(
    path: Path,
    *,
    name: str,
    version: str,
    python_range: str,
) -> Message:
    metadata = _wheel_metadata(path)
    if metadata.get("Name") != name:
        raise CheckError(
            f"{path.name} metadata name is {metadata.get('Name')!r}, expected {name!r}"
        )
    if metadata.get("Version") != version:
        raise CheckError(
            f"{path.name} metadata version is {metadata.get('Version')!r}, expected {version!r}"
        )
    built_python_range = metadata.get("Requires-Python") or ""
    if _canonical_python_range(built_python_range) != _canonical_python_range(python_range):
        raise CheckError(
            f"{path.name} Python range is {built_python_range!r}, expected {python_range!r}"
        )
    return metadata


def _requirement_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
    if match is None:
        return ""
    return match.group(1).replace("_", "-").casefold()


def _check_main_metadata(metadata: Message, wheel_name: str) -> None:
    extras = {value.casefold() for value in metadata.get_all("Provides-Extra", [])}
    if extras != EXTRAS:
        raise CheckError(f"{wheel_name} extras are {sorted(extras)}, expected {sorted(EXTRAS)}")
    base: set[str] = set()
    for raw in metadata.get_all("Requires-Dist", []):
        requirement, separator, _marker = raw.partition(";")
        if not separator:
            base.add(_requirement_name(requirement))
    for needed in ("websockets", "trimesh", "numpy"):
        if needed not in base:
            raise CheckError(f"{wheel_name} does not declare {needed} as a base dependency")
    leaked = sorted(base & OPTIONAL_ONLY)
    if leaked:
        raise CheckError(f"{wheel_name} lists optional packages as base dependencies: {leaked}")


def _zip_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
    if any(_unsafe_archive_name(name) for name in names):
        raise CheckError(f"{path.name} contains an unsafe archive path")
    if any(stat.S_ISLNK(entry.external_attr >> 16) for entry in entries):
        raise CheckError(f"{path.name} contains a symbolic link")
    return names


def _tar_names(path: Path) -> list[str]:
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
    if any(_unsafe_archive_name(name) for name in names):
        raise CheckError(f"{path.name} contains an unsafe archive path")
    if any(member.issym() or member.islnk() for member in members):
        raise CheckError(f"{path.name} contains a link entry")
    return names


def _unsafe_archive_name(name: str) -> bool:
    posix = PurePosixPath(name)
    windows = PureWindowsPath(name)
    windows_components = tuple(
        part for part in windows.parts if part not in {windows.anchor, "\\", "/"}
    )
    unsafe_windows_component = any(
        ":" in part
        or part != part.rstrip(" .")
        or part.rstrip(" .").split(".", 1)[0].upper() in _WINDOWS_DEVICES
        for part in windows_components
    )
    return (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or bool(windows.root)
        or ".." in posix.parts
        or ".." in windows.parts
        or unsafe_windows_component
    )


def _has_suffix(names: list[str], suffix: str) -> bool:
    return any(name.endswith(suffix) for name in names)


def _relative(name: str, prefix: str) -> str | None:
    marker = "/" + prefix
    normalized = name.replace("\\", "/")
    if normalized.startswith(prefix):
        return normalized.removeprefix(prefix)
    if marker in normalized:
        return normalized.split(marker, 1)[1]
    return None


def _viewer_static_relative(name: str) -> str | None:
    return _relative(name, _VIEWER_STATIC_PREFIX)


def _agent_static_relative(name: str) -> str | None:
    return _relative(name, _AGENT_STATIC_PREFIX)


def _unexpected_static(names: list[str], relative_of, expected: tuple[str, ...]) -> list[str]:
    allowed = set(expected)
    unexpected: list[str] = []
    for name in names:
        if name.replace("\\", "/").endswith("/"):
            continue
        relative = relative_of(name)
        if relative is not None and relative not in allowed:
            unexpected.append(name)
    return sorted(unexpected)


def _unexpected_viewer_static(names: list[str]) -> list[str]:
    return _unexpected_static(names, _viewer_static_relative, REQUIRED_ASSETS)


def _unexpected_agent_static(names: list[str]) -> list[str]:
    return _unexpected_static(names, _agent_static_relative, REQUIRED_AGENT_ASSETS)


def _unexpected_browser_assets(names: list[str]) -> list[str]:
    """Return browser files outside the reviewed static trees."""
    unexpected: list[str] = []
    for name in names:
        normalized = name.replace("\\", "/")
        if normalized.endswith("/"):
            continue
        if _viewer_static_relative(normalized) is not None:
            continue
        if _agent_static_relative(normalized) is not None:
            continue
        if "/static/" in normalized or normalized.endswith(".wasm"):
            unexpected.append(name)
    return sorted(unexpected)


def _retired_files(names: list[str]) -> list[str]:
    retired: list[str] = []
    for name in names:
        normalized = name.replace("\\", "/")
        package_path = normalized
        marker = "/src/"
        if marker in normalized:
            package_path = normalized.split(marker, 1)[1]
        if package_path.startswith(RETIRED_PREFIXES) or package_path in RETIRED_FILES:
            retired.append(name)
    return sorted(retired)


def _static_size(path: Path, relative_of) -> int:
    with zipfile.ZipFile(path) as archive:
        return sum(
            entry.file_size
            for entry in archive.infolist()
            if not entry.is_dir() and relative_of(entry.filename) is not None
        )


def _source_entry_is_excluded(name: str) -> bool:
    for path in (PurePosixPath(name), PureWindowsPath(name)):
        parts = tuple(part.casefold() for part in path.parts[1:])
        if _EXCLUDED_SOURCE_PARTS.intersection(parts):
            return True
        if parts == ("uv.lock",):
            return True
        if (
            len(parts) >= 4
            and parts[:3] == ("docs", "assets", "brand")
            and parts[-1].endswith(".png")
        ):
            return True
    return False


def _check_main(main_wheel: Path, main_sdist: Path, version: str, python_range: str) -> float:
    metadata = _check_metadata(
        main_wheel, name="ifc-console", version=version, python_range=python_range
    )
    _check_main_metadata(metadata, main_wheel.name)

    names = _zip_names(main_wheel)
    for required, label, prefix in (
        (REQUIRED_ASSETS, "viewer assets", "ifc_console/viewer/static/"),
        (REQUIRED_AGENT_ASSETS, "Agent workspace assets", "ifc_console/agents/static/"),
    ):
        missing = [asset for asset in required if not _has_suffix(names, f"{prefix}{asset}")]
        if missing:
            raise CheckError(f"{main_wheel.name} is missing {label}: {missing}")
    for check, label in (
        (_unexpected_viewer_static, "viewer static files"),
        (_unexpected_agent_static, "Agent workspace static files"),
        (_unexpected_browser_assets, "browser assets outside the reviewed trees"),
        (_retired_files, "files retired in 0.2.0"),
    ):
        unexpected = check(names)
        if unexpected:
            raise CheckError(f"{main_wheel.name} contains {label}: {unexpected[:5]}")
    if not _has_suffix(names, "ifc_console/py.typed"):
        raise CheckError(f"{main_wheel.name} is missing the PEP 561 py.typed marker")
    if not _has_suffix(names, ".dist-info/licenses/LICENSE"):
        raise CheckError(f"{main_wheel.name} is missing the Apache-2.0 license")
    size_mb = main_wheel.stat().st_size / 1e6
    if size_mb > MAIN_LIMIT_MB:
        raise CheckError(
            f"{main_wheel.name} is {size_mb:.2f} MB, over the {MAIN_LIMIT_MB} MB budget"
        )
    static_mb = _static_size(main_wheel, _viewer_static_relative) / 1e6
    if static_mb > STATIC_LIMIT_MB:
        raise CheckError(
            f"{main_wheel.name} installs {static_mb:.2f} MB of viewer assets, "
            f"over the {STATIC_LIMIT_MB} MB budget"
        )
    agent_static_mb = _static_size(main_wheel, _agent_static_relative) / 1e6
    if agent_static_mb > AGENT_STATIC_LIMIT_MB:
        raise CheckError(
            f"{main_wheel.name} installs {agent_static_mb:.2f} MB of panel assets, "
            f"over the {AGENT_STATIC_LIMIT_MB} MB budget"
        )

    source_names = _tar_names(main_sdist)
    for required in ("CHANGELOG.md", "SECURITY.md", "src/ifc_console/py.typed"):
        if not _has_suffix(source_names, required):
            raise CheckError(f"{main_sdist.name} is missing {required}")
    for prefix, assets, label in (
        ("src/ifc_console/viewer/static/", REQUIRED_ASSETS, "viewer assets"),
        ("src/ifc_console/agents/static/", REQUIRED_AGENT_ASSETS, "Agent workspace assets"),
    ):
        missing = [asset for asset in assets if not _has_suffix(source_names, f"{prefix}{asset}")]
        if missing:
            raise CheckError(f"{main_sdist.name} is missing {label}: {missing}")
    for check, label in (
        (_unexpected_viewer_static, "viewer static files"),
        (_unexpected_agent_static, "Agent workspace static files"),
        (_retired_files, "files retired in 0.2.0"),
    ):
        unexpected = check(source_names)
        if unexpected:
            raise CheckError(f"{main_sdist.name} contains {label}: {unexpected[:5]}")
    leaked = [name for name in source_names if _source_entry_is_excluded(name)]
    if leaked:
        raise CheckError(f"excluded files leaked into {main_sdist.name}: {leaked[:5]}")
    sdist_mb = main_sdist.stat().st_size / 1e6
    if sdist_mb > MAIN_SDIST_LIMIT_MB:
        raise CheckError(
            f"{main_sdist.name} is {sdist_mb:.2f} MB, over the {MAIN_SDIST_LIMIT_MB} MB budget"
        )
    return size_mb


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=DIST)
    parser.add_argument(
        "--stage-dir",
        type=Path,
        help="copy only the verified current-version artifacts here for publishing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dist = args.dist_dir.resolve()
    try:
        version = _source_version()
        python_range = _source_python_range()
        main_wheel = _one(dist, f"ifc_console-{version}-*.whl", "main wheel")
        main_sdist = _one(dist, f"ifc_console-{version}.tar.gz", "main source archive")
        size_mb = _check_main(main_wheel, main_sdist, version, python_range)
        if args.stage_dir is not None:
            stage = args.stage_dir.resolve()
            if stage.exists():
                raise CheckError(f"publish staging directory already exists: {stage}")
            target = stage / "core"
            target.mkdir(parents=True)
            for artifact in (main_wheel, main_sdist):
                shutil.copy2(artifact, target / artifact.name)
    except (CheckError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"FAIL: {exc}")
        return 1

    print(
        f"ok: {main_wheel.name} {size_mb:.2f} MB carries the viewer and the Agent workspace; "
        "the source archive is complete"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
