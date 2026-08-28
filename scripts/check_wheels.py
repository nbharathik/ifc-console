"""Release guard for core, agents, and the temporary viewer shim.

The main wheel must carry the complete reviewed browser bundle while staying
within its release budget. The agents wheel owns its SDK and browser panel in a
separate namespace. ``ifc-console-viewer`` is a one-release forwarding shim and
must contain no browser assets. Wheels and source archives must match the source
version. Run after building all three workspace packages.
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
MAIN_LIMIT_MB = 2.5
STATIC_LIMIT_MB = 9.5
AGENTS_LIMIT_MB = 1.0
AGENT_STATIC_LIMIT_MB = 1.0
COMBINED_LIMIT_MB = 3.0
MAIN_SDIST_LIMIT_MB = 3.0
AGENTS_SDIST_LIMIT_MB = 1.5
SHIM_LIMIT_MB = 0.05
REQUIRED_ASSETS = (
    "index.html",
    "app.css",
    "app.js",
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
    "chat_ai_sdk.js",
    "chat_flow.js",
    "chat_history.js",
    "chat_markdown.js",
    "chat_sidebar.js",
    "chat_studio.js",
    "chat_workspace.js",
)
CORE_AGENT_SHIMS = (
    "ifc_console/agents/__init__.py",
    "ifc_console/chat/__init__.py",
    "ifc_console/credentials.py",
    "ifc_console/devkit/__init__.py",
    "ifc_console/integrations/langgraph.py",
    "ifc_console/mcp/tools_skills.py",
    "ifc_console/testing.py",
)
_VIEWER_STATIC_PREFIX = "ifc_console/viewer/static/"
_AGENT_STATIC_PREFIX = "ifc_console_agents/static/"
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


def _source_agent_range() -> str:
    source = (ROOT / "packages" / "ifc-console-agents" / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r'"ifc-console((?:[<>=!~][^"]*)+)"',
        source,
        flags=re.MULTILINE,
    )
    if match is None:
        raise CheckError("cannot read the agents package core dependency range")
    return match.group(1).replace(" ", "")


def _source_shim_range() -> str:
    source = (ROOT / "packages" / "ifc-console-viewer" / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r'^\s*dependencies\s*=\s*\[\s*"ifc-console([^"]+)"\s*\]\s*$',
        source,
        flags=re.MULTILINE,
    )
    if match is None:
        raise CheckError("cannot read the compatibility shim dependency range")
    return match.group(1).replace(" ", "")


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


def _check_bundled_viewer_metadata(metadata: Message, wheel_name: str) -> None:
    extras = {value.casefold() for value in metadata.get_all("Provides-Extra", [])}
    if "viewer" not in extras:
        raise CheckError(f"{wheel_name} does not retain the compatibility viewer extra")
    all_requirements: list[tuple[str, str]] = []
    for raw in metadata.get_all("Requires-Dist", []):
        requirement, separator, marker = raw.partition(";")
        normalized_marker = marker.replace(" ", "").replace('"', "'").casefold()
        normalized_name = requirement.strip().replace("_", "-")
        all_requirements.append((normalized_name, normalized_marker if separator else ""))
    companion = [
        requirement
        for requirement, _marker in all_requirements
        if requirement.casefold().startswith("ifc-console-viewer")
    ]
    if companion:
        raise CheckError(
            f"{wheel_name} still depends on the retired viewer asset wheel: {companion}"
        )
    viewer_requirements = [
        requirement
        for requirement, marker in all_requirements
        if marker == "extra=='viewer'"
    ]
    if viewer_requirements:
        raise CheckError(
            f"{wheel_name} viewer compatibility extra must be empty: {viewer_requirements}"
        )
    if not any(
        requirement.casefold().startswith("websockets") and not marker
        for requirement, marker in all_requirements
    ):
        raise CheckError(f"{wheel_name} does not declare websockets as a base dependency")
    forbidden_base = {
        "ifc-console-agents",
        "ifc-console-viewer",
        "keyring",
        "langchain",
        "langgraph",
        "pymupdf",
        "pypdf",
    }
    leaked = sorted(
        requirement
        for requirement, marker in all_requirements
        if "extra==" not in marker and _requirement_name(requirement) in forbidden_base
    )
    if leaked:
        raise CheckError(f"{wheel_name} contains agent-only base dependencies: {leaked}")


def _requirement_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
    if match is None:
        return ""
    return match.group(1).replace("_", "-").casefold()


def _check_agent_metadata(metadata: Message, expected_range: str, wheel_name: str) -> None:
    raw_requirements = metadata.get_all("Requires-Dist", [])
    requirements = [raw.partition(";")[0].strip() for raw in raw_requirements]
    core_requirements = [
        requirement
        for requirement in requirements
        if _requirement_name(requirement) == "ifc-console"
    ]
    if len(core_requirements) != 1:
        raise CheckError(
            f"{wheel_name} must declare exactly one compatible ifc-console requirement: "
            f"{core_requirements}"
        )
    core_raw = [
        raw
        for raw in raw_requirements
        if _requirement_name(raw.partition(";")[0]) == "ifc-console"
    ][0]
    if core_raw.partition(";")[1]:
        raise CheckError(f"{wheel_name} core requirement must be unconditional")
    match = re.fullmatch(r"ifc[-_]console(.+)", core_requirements[0], flags=re.IGNORECASE)
    if match is None or _canonical_python_range(match.group(1)) != _canonical_python_range(
        expected_range
    ):
        raise CheckError(
            f"{wheel_name} core requirement is {core_requirements[0]!r}, "
            f"expected ifc-console{expected_range}"
        )
    retired = sorted(
        requirement
        for requirement in requirements
        if _requirement_name(requirement) == "ifc-console-viewer"
    )
    if retired:
        raise CheckError(f"{wheel_name} depends on the retired viewer package: {retired}")


def _check_shim_requirement(metadata: Message, expected_range: str, wheel_name: str) -> None:
    raw_requirements = metadata.get_all("Requires-Dist", [])
    requirements = [
        raw.partition(";")[0].strip().replace("_", "-")
        for raw in raw_requirements
    ]
    core_requirements = [
        requirement
        for requirement in requirements
        if re.match(r"^ifc-console(?=[<>=!~])", requirement, flags=re.IGNORECASE)
    ]
    if len(requirements) != 1 or len(core_requirements) != 1:
        raise CheckError(
            f"{wheel_name} must depend only on one compatible ifc-console: {requirements}"
        )
    if raw_requirements[0].partition(";")[1]:
        raise CheckError(f"{wheel_name} core requirement must be unconditional")
    match = re.fullmatch(r"ifc-console(.+)", core_requirements[0], flags=re.IGNORECASE)
    if match is None or _canonical_python_range(match.group(1)) != _canonical_python_range(
        expected_range
    ):
        raise CheckError(
            f"{wheel_name} core requirement is {core_requirements[0]!r}, "
            f"expected ifc-console{expected_range}"
        )


def _zip_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
    if any(_unsafe_archive_name(name) for name in names):
        raise CheckError(f"{path.name} contains an unsafe archive path")
    if any(stat.S_ISLNK(entry.external_attr >> 16) for entry in entries):
        raise CheckError(f"{path.name} contains a symbolic link")
    return names


def _zip_member_text(path: Path, suffix: str) -> str:
    with zipfile.ZipFile(path) as archive:
        matches = [name for name in archive.namelist() if name.endswith(suffix)]
        if len(matches) != 1:
            raise CheckError(
                f"{path.name} has {len(matches)} files ending in {suffix!r}"
            )
        return archive.read(matches[0]).decode("utf-8")


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


def _import_paths(names: list[str]) -> set[str]:
    return {
        name.replace("\\", "/")
        for name in names
        if ".dist-info/" not in name.replace("\\", "/")
        and not name.replace("\\", "/").endswith("/")
    }


def _viewer_static_relative(name: str) -> str | None:
    marker = "/" + _VIEWER_STATIC_PREFIX
    normalized = name.replace("\\", "/")
    if normalized.startswith(_VIEWER_STATIC_PREFIX):
        return normalized.removeprefix(_VIEWER_STATIC_PREFIX)
    if marker in normalized:
        return normalized.split(marker, 1)[1]
    return None


def _agent_static_relative(name: str) -> str | None:
    marker = "/" + _AGENT_STATIC_PREFIX
    normalized = name.replace("\\", "/")
    if normalized.startswith(_AGENT_STATIC_PREFIX):
        return normalized.removeprefix(_AGENT_STATIC_PREFIX)
    if marker in normalized:
        return normalized.split(marker, 1)[1]
    return None


def _unexpected_viewer_static(names: list[str]) -> list[str]:
    expected = set(REQUIRED_ASSETS)
    unexpected: list[str] = []
    for name in names:
        normalized = name.replace("\\", "/")
        if normalized.endswith("/"):
            continue
        relative = _viewer_static_relative(normalized)
        if relative is None:
            continue
        if relative not in expected:
            unexpected.append(name)
    return sorted(unexpected)


def _unexpected_agent_static(names: list[str]) -> list[str]:
    expected = set(REQUIRED_AGENT_ASSETS)
    unexpected: list[str] = []
    for name in names:
        normalized = name.replace("\\", "/")
        if normalized.endswith("/"):
            continue
        relative = _agent_static_relative(normalized)
        if relative is not None and relative not in expected:
            unexpected.append(name)
    return sorted(unexpected)


def _unexpected_agent_browser_assets(names: list[str]) -> list[str]:
    unexpected: list[str] = []
    for name in names:
        normalized = name.replace("\\", "/")
        if normalized.endswith("/") or _agent_static_relative(normalized) is not None:
            continue
        if "/static/" in normalized or normalized.endswith(".wasm"):
            unexpected.append(name)
    return sorted(unexpected)


def _unexpected_main_browser_assets(names: list[str]) -> list[str]:
    """Return browser files outside the main package's reviewed static tree."""
    unexpected: list[str] = []
    for name in names:
        normalized = name.replace("\\", "/")
        if normalized.endswith("/"):
            continue
        if _viewer_static_relative(normalized) is not None:
            continue
        if (
            "/static/" in normalized
            or normalized.endswith(".wasm")
            or normalized.startswith("ifc_console_viewer/")
            or "/ifc_console_viewer/" in normalized
        ):
            unexpected.append(name)
    return sorted(unexpected)


def _unexpected_main_agent_files(names: list[str]) -> list[str]:
    retired_prefixes = (
        "ifc_console/agents/",
        "ifc_console/chat/",
        "ifc_console/devkit/",
    )
    retired_files = {
        "ifc_console/credentials.py",
        "ifc_console/integrations/langgraph.py",
        "ifc_console/mcp/tools_skills.py",
        "ifc_console/testing.py",
    }
    allowed = set(CORE_AGENT_SHIMS)
    unexpected: list[str] = []
    for name in names:
        normalized = name.replace("\\", "/")
        package_path = normalized
        marker = "/src/ifc_console/"
        if marker in normalized:
            package_path = "ifc_console/" + normalized.split(marker, 1)[1]
        is_legacy = package_path.startswith(retired_prefixes) or package_path in retired_files
        if package_path.startswith("ifc_console_agents/") or (
            is_legacy and package_path not in allowed
        ):
            unexpected.append(name)
    return sorted(unexpected)


def _unexpected_agent_files(names: list[str]) -> list[str]:
    """Agents must never write into core's import namespace."""
    unexpected: list[str] = []
    for name in names:
        normalized = name.replace("\\", "/")
        if normalized.startswith("ifc_console/") or "/src/ifc_console/" in normalized:
            unexpected.append(name)
    return sorted(unexpected)


def _viewer_static_size(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        return sum(
            entry.file_size
            for entry in archive.infolist()
            if not entry.is_dir() and _viewer_static_relative(entry.filename) is not None
        )


def _agent_static_size(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        return sum(
            entry.file_size
            for entry in archive.infolist()
            if not entry.is_dir() and _agent_static_relative(entry.filename) is not None
        )


def _unexpected_shim_assets(names: list[str]) -> list[str]:
    unexpected: list[str] = []
    for name in names:
        normalized = name.replace("\\", "/")
        if normalized.endswith("/"):
            continue
        if "/static/" in normalized or normalized.endswith(".wasm"):
            unexpected.append(name)
    return sorted(unexpected)


def _unexpected_shim_package_files(names: list[str]) -> list[str]:
    unexpected: list[str] = []
    for name in names:
        normalized = name.replace("\\", "/")
        package_path = normalized
        marker = "/src/ifc_console_viewer/"
        if marker in normalized:
            package_path = "ifc_console_viewer/" + normalized.split(marker, 1)[1]
        if (
            package_path.startswith("ifc_console_viewer/")
            and package_path != "ifc_console_viewer/__init__.py"
            and not package_path.endswith("/")
        ):
            unexpected.append(name)
    return sorted(unexpected)


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
        agent_range = _source_agent_range()
        shim_range = _source_shim_range()
        main_wheel = _one(dist, f"ifc_console-{version}-*.whl", "main wheel")
        agent_wheel = _one(
            dist, f"ifc_console_agents-{version}-*.whl", "agents wheel"
        )
        shim_wheel = _one(
            dist, f"ifc_console_viewer-{version}-*.whl", "viewer compatibility wheel"
        )
        main_sdist = _one(dist, f"ifc_console-{version}.tar.gz", "main source archive")
        agent_sdist = _one(
            dist,
            f"ifc_console_agents-{version}.tar.gz",
            "agents source archive",
        )
        shim_sdist = _one(
            dist,
            f"ifc_console_viewer-{version}.tar.gz",
            "viewer compatibility source archive",
        )
        main_metadata = _check_metadata(
            main_wheel,
            name="ifc-console",
            version=version,
            python_range=python_range,
        )
        _check_bundled_viewer_metadata(main_metadata, main_wheel.name)
        agent_metadata = _check_metadata(
            agent_wheel,
            name="ifc-console-agents",
            version=version,
            python_range=python_range,
        )
        _check_agent_metadata(agent_metadata, agent_range, agent_wheel.name)
        shim_metadata = _check_metadata(
            shim_wheel,
            name="ifc-console-viewer",
            version=version,
            python_range=python_range,
        )
        _check_shim_requirement(shim_metadata, shim_range, shim_wheel.name)

        main_names = _zip_names(main_wheel)
        missing = [
            asset
            for asset in REQUIRED_ASSETS
            if not _has_suffix(main_names, f"ifc_console/viewer/static/{asset}")
        ]
        if missing:
            raise CheckError(f"{main_wheel.name} is missing viewer assets: {missing}")
        unexpected_static = _unexpected_viewer_static(main_names)
        if unexpected_static:
            raise CheckError(
                f"{main_wheel.name} contains unexpected public static files: "
                f"{unexpected_static[:5]}"
            )
        stray = _unexpected_main_browser_assets(main_names)
        if stray:
            raise CheckError(
                f"browser assets escaped the reviewed tree in {main_wheel.name}: {stray[:5]}"
            )
        leaked_agents = _unexpected_main_agent_files(main_names)
        if leaked_agents:
            raise CheckError(
                f"agent implementation leaked into {main_wheel.name}: {leaked_agents[:5]}"
            )
        missing_agent_shims = [
            path for path in CORE_AGENT_SHIMS if not _has_suffix(main_names, path)
        ]
        if missing_agent_shims:
            raise CheckError(
                f"{main_wheel.name} is missing one-release agent compatibility shims: "
                f"{missing_agent_shims}"
            )
        if not _has_suffix(main_names, "ifc_console/py.typed"):
            raise CheckError(f"{main_wheel.name} is missing the PEP 561 py.typed marker")
        if not _has_suffix(main_names, ".dist-info/licenses/LICENSE"):
            raise CheckError(f"{main_wheel.name} is missing the Apache-2.0 license")
        main_size_mb = main_wheel.stat().st_size / 1e6
        if main_size_mb > MAIN_LIMIT_MB:
            raise CheckError(
                f"{main_wheel.name} is {main_size_mb:.2f} MB, "
                f"over the {MAIN_LIMIT_MB} MB budget"
            )
        static_size_mb = _viewer_static_size(main_wheel) / 1e6
        if static_size_mb > STATIC_LIMIT_MB:
            raise CheckError(
                f"{main_wheel.name} installs {static_size_mb:.2f} MB of browser assets, "
                f"over the {STATIC_LIMIT_MB} MB budget"
            )

        agent_names = _zip_names(agent_wheel)
        missing_agent_assets = [
            asset
            for asset in REQUIRED_AGENT_ASSETS
            if not _has_suffix(agent_names, f"ifc_console_agents/static/{asset}")
        ]
        if missing_agent_assets:
            raise CheckError(
                f"{agent_wheel.name} is missing browser panel assets: "
                f"{missing_agent_assets}"
            )
        unexpected_agent_static = _unexpected_agent_static(agent_names)
        if unexpected_agent_static:
            raise CheckError(
                f"{agent_wheel.name} contains unexpected public static files: "
                f"{unexpected_agent_static[:5]}"
            )
        stray_agent_assets = _unexpected_agent_browser_assets(agent_names)
        if stray_agent_assets:
            raise CheckError(
                f"browser assets escaped the reviewed tree in {agent_wheel.name}: "
                f"{stray_agent_assets[:5]}"
            )
        leaked_core = _unexpected_agent_files(agent_names)
        if leaked_core:
            raise CheckError(
                f"{agent_wheel.name} writes into the core namespace: {leaked_core[:5]}"
            )
        overlap = sorted(_import_paths(main_names) & _import_paths(agent_names))
        if overlap:
            raise CheckError(
                f"{main_wheel.name} and {agent_wheel.name} overlap package paths: "
                f"{overlap[:5]}"
            )
        if not _has_suffix(agent_names, "ifc_console_agents/py.typed"):
            raise CheckError(f"{agent_wheel.name} is missing the PEP 561 py.typed marker")
        if not _has_suffix(agent_names, ".dist-info/licenses/LICENSE"):
            raise CheckError(f"{agent_wheel.name} is missing the Apache-2.0 license")
        entry_points = _zip_member_text(agent_wheel, ".dist-info/entry_points.txt")
        if (
            "[ifc_console.extensions]" not in entry_points
            or "agents = ifc_console_agents.extension:AgentExtension" not in entry_points
        ):
            raise CheckError(
                f"{agent_wheel.name} does not register its IFC Console extension"
            )
        agent_size_mb = agent_wheel.stat().st_size / 1e6
        if agent_size_mb > AGENTS_LIMIT_MB:
            raise CheckError(
                f"{agent_wheel.name} is {agent_size_mb:.2f} MB, "
                f"over the {AGENTS_LIMIT_MB} MB budget"
            )
        agent_static_size_mb = _agent_static_size(agent_wheel) / 1e6
        if agent_static_size_mb > AGENT_STATIC_LIMIT_MB:
            raise CheckError(
                f"{agent_wheel.name} installs {agent_static_size_mb:.2f} MB of panel assets, "
                f"over the {AGENT_STATIC_LIMIT_MB} MB budget"
            )
        combined_size_mb = main_size_mb + agent_size_mb
        if combined_size_mb > COMBINED_LIMIT_MB:
            raise CheckError(
                f"active wheels total {combined_size_mb:.2f} MB, "
                f"over the {COMBINED_LIMIT_MB} MB combined budget"
            )

        shim_names = _zip_names(shim_wheel)
        shim_assets = _unexpected_shim_assets(shim_names)
        if shim_assets:
            raise CheckError(
                f"{shim_wheel.name} must not contain browser assets: {shim_assets[:5]}"
            )
        unexpected_shim_files = _unexpected_shim_package_files(shim_names)
        if unexpected_shim_files:
            raise CheckError(
                f"{shim_wheel.name} must contain only its forwarding module: "
                f"{unexpected_shim_files[:5]}"
            )
        if not _has_suffix(shim_names, "ifc_console_viewer/__init__.py"):
            raise CheckError(f"{shim_wheel.name} is missing its forwarding module")
        if not _has_suffix(shim_names, ".dist-info/licenses/LICENSE"):
            raise CheckError(f"{shim_wheel.name} is missing the Apache-2.0 license")
        shim_size_mb = shim_wheel.stat().st_size / 1e6
        if shim_size_mb > SHIM_LIMIT_MB:
            raise CheckError(
                f"{shim_wheel.name} is {shim_size_mb:.2f} MB, "
                f"over the {SHIM_LIMIT_MB} MB compatibility-shim budget"
            )

        source_names = _tar_names(main_sdist)
        required_source = ("CHANGELOG.md", "SECURITY.md", "src/ifc_console/py.typed")
        missing_source = [
            name for name in required_source if not _has_suffix(source_names, name)
        ]
        if missing_source:
            raise CheckError(f"{main_sdist.name} is missing {missing_source}")
        missing_static_source = [
            asset
            for asset in REQUIRED_ASSETS
            if not _has_suffix(source_names, f"src/ifc_console/viewer/static/{asset}")
        ]
        if missing_static_source:
            raise CheckError(f"{main_sdist.name} is missing {missing_static_source}")
        unexpected_static_source = _unexpected_viewer_static(source_names)
        if unexpected_static_source:
            raise CheckError(
                f"{main_sdist.name} contains unexpected public static files: "
                f"{unexpected_static_source[:5]}"
            )
        leaked_agent_source = _unexpected_main_agent_files(source_names)
        if leaked_agent_source:
            raise CheckError(
                f"agent implementation leaked into {main_sdist.name}: "
                f"{leaked_agent_source[:5]}"
            )
        missing_agent_shim_source = [
            path
            for path in CORE_AGENT_SHIMS
            if not _has_suffix(source_names, f"src/{path}")
        ]
        if missing_agent_shim_source:
            raise CheckError(
                f"{main_sdist.name} is missing agent compatibility shims: "
                f"{missing_agent_shim_source}"
            )
        leaked_source = [name for name in source_names if _source_entry_is_excluded(name)]
        if leaked_source:
            raise CheckError(
                f"excluded files leaked into {main_sdist.name}: {leaked_source[:5]}"
            )
        main_sdist_size_mb = main_sdist.stat().st_size / 1e6
        if main_sdist_size_mb > MAIN_SDIST_LIMIT_MB:
            raise CheckError(
                f"{main_sdist.name} is {main_sdist_size_mb:.2f} MB, "
                f"over the {MAIN_SDIST_LIMIT_MB} MB budget"
            )

        agent_source_names = _tar_names(agent_sdist)
        for required in ("LICENSE", "README.md", "src/ifc_console_agents/py.typed"):
            if not _has_suffix(agent_source_names, required):
                raise CheckError(f"{agent_sdist.name} is missing {required}")
        missing_agent_static_source = [
            asset
            for asset in REQUIRED_AGENT_ASSETS
            if not _has_suffix(
                agent_source_names, f"src/ifc_console_agents/static/{asset}"
            )
        ]
        if missing_agent_static_source:
            raise CheckError(
                f"{agent_sdist.name} is missing {missing_agent_static_source}"
            )
        unexpected_agent_static_source = _unexpected_agent_static(agent_source_names)
        if unexpected_agent_static_source:
            raise CheckError(
                f"{agent_sdist.name} contains unexpected public static files: "
                f"{unexpected_agent_static_source[:5]}"
            )
        stray_agent_source = _unexpected_agent_browser_assets(agent_source_names)
        if stray_agent_source:
            raise CheckError(
                f"browser assets escaped the reviewed tree in {agent_sdist.name}: "
                f"{stray_agent_source[:5]}"
            )
        leaked_agent_source = _unexpected_agent_files(agent_source_names)
        if leaked_agent_source:
            raise CheckError(
                f"{agent_sdist.name} writes into the core namespace: "
                f"{leaked_agent_source[:5]}"
            )
        agent_sdist_size_mb = agent_sdist.stat().st_size / 1e6
        if agent_sdist_size_mb > AGENTS_SDIST_LIMIT_MB:
            raise CheckError(
                f"{agent_sdist.name} is {agent_sdist_size_mb:.2f} MB, "
                f"over the {AGENTS_SDIST_LIMIT_MB} MB budget"
            )

        shim_source_names = _tar_names(shim_sdist)
        if not _has_suffix(shim_source_names, "LICENSE"):
            raise CheckError(f"{shim_sdist.name} is missing the Apache-2.0 license")
        if not _has_suffix(shim_source_names, "src/ifc_console_viewer/__init__.py"):
            raise CheckError(f"{shim_sdist.name} is missing its forwarding module")
        unexpected_shim_source = _unexpected_shim_assets(shim_source_names)
        if unexpected_shim_source:
            raise CheckError(
                f"{shim_sdist.name} must not contain browser assets: "
                f"{unexpected_shim_source[:5]}"
            )
        unexpected_shim_source_files = _unexpected_shim_package_files(shim_source_names)
        if unexpected_shim_source_files:
            raise CheckError(
                f"{shim_sdist.name} must contain only its forwarding module: "
                f"{unexpected_shim_source_files[:5]}"
            )
        if args.stage_dir is not None:
            stage = args.stage_dir.resolve()
            if stage.exists():
                raise CheckError(f"publish staging directory already exists: {stage}")
            core_stage = stage / "core"
            agent_stage = stage / "agents"
            shim_stage = stage / "compat-viewer"
            core_stage.mkdir(parents=True)
            agent_stage.mkdir(parents=True)
            shim_stage.mkdir(parents=True)
            for artifact in (main_wheel, main_sdist):
                shutil.copy2(artifact, core_stage / artifact.name)
            for artifact in (agent_wheel, agent_sdist):
                shutil.copy2(artifact, agent_stage / artifact.name)
            for artifact in (shim_wheel, shim_sdist):
                shutil.copy2(artifact, shim_stage / artifact.name)
    except (CheckError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"FAIL: {exc}")
        return 1

    print(
        f"ok: {main_wheel.name} {main_size_mb:.2f} MB carries "
        f"{static_size_mb:.2f} MB of reviewed browser assets; "
        f"{agent_wheel.name} {agent_size_mb:.2f} MB carries "
        f"{agent_static_size_mb:.2f} MB of reviewed panel assets; "
        f"{shim_wheel.name} is an asset-free compatibility shim; "
        "all source archives are complete"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
