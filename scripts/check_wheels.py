"""Release guard: both distributions are complete and correctly split.

The base wheel must stay small and free of the 3D viewer bundle; the viewer
wheel must carry the whole bundle. Wheels and source archives must match the
source version. Run after ``uv build`` and
``uv build --package ifc-console-viewer``.
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
BASE_LIMIT_MB = 1.0
REQUIRED_ASSETS = (
    "index.html",
    "app.css",
    "app.js",
    "measure_math.js",
    "parser.js",
    "themes.css",
    "worker.js",
    "chat.html",
    "chat.js",
    "chat_ai_sdk.js",
    "chat_flow.js",
    "chat_history.js",
    "chat_markdown.js",
    "chat_sidebar.js",
    "chat_studio.js",
    "chat_workspace.js",
    "chat.css",
    # the standalone page boots from this; without it /chat is a blank screen
    "chat-page.js",
    "vendor/OrbitControls.js",
    "vendor/three.core.min.js",
    "vendor/three.module.min.js",
    "vendor/web-ifc-api.js",
    "vendor/web-ifc.wasm",
    "vendor/VENDORED.md",
    "vendor/LICENSE.three.txt",
    "vendor/LICENSE.web-ifc.md",
)
_VIEWER_STATIC_PREFIX = "ifc_console_viewer/static/"
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


def _source_viewer_range() -> str:
    source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(
        r'^\s*"ifc-console-viewer([^"]+)"\s*,?\s*$',
        source,
        flags=re.MULTILINE,
    )
    if match is None:
        raise CheckError("cannot read the source viewer dependency range")
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


def _check_viewer_extra(metadata: Message, expected_range: str, wheel_name: str) -> None:
    extras = {value.casefold() for value in metadata.get_all("Provides-Extra", [])}
    if "viewer" not in extras:
        raise CheckError(f"{wheel_name} does not declare the viewer extra")
    requirements: list[str] = []
    for raw in metadata.get_all("Requires-Dist", []):
        requirement, separator, marker = raw.partition(";")
        normalized_marker = marker.replace(" ", "").replace('"', "'").casefold()
        normalized_name = requirement.strip().replace("_", "-")
        if separator and normalized_marker == "extra=='viewer'":
            requirements.append(normalized_name)
    viewer_requirements = [
        requirement
        for requirement in requirements
        if requirement.casefold().startswith("ifc-console-viewer")
    ]
    if len(viewer_requirements) != 1:
        raise CheckError(
            f"{wheel_name} has {len(viewer_requirements)} viewer package requirements, "
            "expected one"
        )
    match = re.fullmatch(
        r"ifc-console-viewer(.+)", viewer_requirements[0], flags=re.IGNORECASE
    )
    if match is None or _canonical_python_range(match.group(1)) != _canonical_python_range(
        expected_range
    ):
        raise CheckError(
            f"{wheel_name} viewer requirement is {viewer_requirements[0]!r}, "
            f"expected ifc-console-viewer{expected_range}"
        )
    if not any(
        requirement.casefold().startswith("websockets") for requirement in requirements
    ):
        raise CheckError(f"{wheel_name} viewer extra does not declare websockets")


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


def _unexpected_viewer_static(names: list[str]) -> list[str]:
    expected = set(REQUIRED_ASSETS)
    marker = "/" + _VIEWER_STATIC_PREFIX
    unexpected: list[str] = []
    for name in names:
        normalized = name.replace("\\", "/")
        if normalized.endswith("/"):
            continue
        if normalized.startswith(_VIEWER_STATIC_PREFIX):
            relative = normalized.removeprefix(_VIEWER_STATIC_PREFIX)
        elif marker in normalized:
            relative = normalized.split(marker, 1)[1]
        else:
            continue
        if relative not in expected:
            unexpected.append(name)
    return sorted(unexpected)


def _unexpected_base_browser_assets(names: list[str]) -> list[str]:
    """Return viewer assets or unapproved public files found in the core wheel."""
    unexpected: list[str] = []
    for name in names:
        normalized = name.replace("\\", "/")
        if normalized.endswith("/"):
            continue
        if (
            "/static/" in normalized
            or normalized.endswith(".wasm")
            or normalized.startswith("ifc_console_viewer/")
            or "/ifc_console_viewer/" in normalized
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
        viewer_range = _source_viewer_range()
        base = _one(dist, f"ifc_console-{version}-*.whl", "base wheel")
        viewer = _one(dist, f"ifc_console_viewer-{version}-*.whl", "viewer wheel")
        base_sdist = _one(dist, f"ifc_console-{version}.tar.gz", "base source archive")
        viewer_sdist = _one(
            dist,
            f"ifc_console_viewer-{version}.tar.gz",
            "viewer source archive",
        )
        base_metadata = _check_metadata(
            base,
            name="ifc-console",
            version=version,
            python_range=python_range,
        )
        _check_viewer_extra(base_metadata, viewer_range, base.name)
        _check_metadata(
            viewer,
            name="ifc-console-viewer",
            version=version,
            python_range=python_range,
        )

        base_names = _zip_names(base)
        stray = _unexpected_base_browser_assets(base_names)
        if stray:
            raise CheckError(f"viewer assets leaked into {base.name}: {stray[:5]}")
        if not _has_suffix(base_names, "ifc_console/py.typed"):
            raise CheckError(f"{base.name} is missing the PEP 561 py.typed marker")
        if not _has_suffix(base_names, ".dist-info/licenses/LICENSE"):
            raise CheckError(f"{base.name} is missing the Apache-2.0 license")
        size_mb = base.stat().st_size / 1e6
        if size_mb > BASE_LIMIT_MB:
            raise CheckError(
                f"{base.name} is {size_mb:.2f} MB, over the {BASE_LIMIT_MB} MB budget"
            )

        viewer_names = _zip_names(viewer)
        missing = [
            asset
            for asset in REQUIRED_ASSETS
            if not _has_suffix(viewer_names, f"static/{asset}")
        ]
        if missing:
            raise CheckError(f"{viewer.name} is missing viewer assets: {missing}")
        unexpected_static = _unexpected_viewer_static(viewer_names)
        if unexpected_static:
            raise CheckError(
                f"{viewer.name} contains unexpected public static files: {unexpected_static[:5]}"
            )
        if not _has_suffix(viewer_names, ".dist-info/licenses/LICENSE"):
            raise CheckError(f"{viewer.name} is missing the Apache-2.0 license")

        source_names = _tar_names(base_sdist)
        required_source = ("CHANGELOG.md", "SECURITY.md", "src/ifc_console/py.typed")
        missing_source = [
            name for name in required_source if not _has_suffix(source_names, name)
        ]
        if missing_source:
            raise CheckError(f"{base_sdist.name} is missing {missing_source}")
        leaked_source = [name for name in source_names if _source_entry_is_excluded(name)]
        if leaked_source:
            raise CheckError(
                f"excluded files leaked into {base_sdist.name}: {leaked_source[:5]}"
            )

        viewer_source_names = _tar_names(viewer_sdist)
        if not _has_suffix(viewer_source_names, "LICENSE"):
            raise CheckError(f"{viewer_sdist.name} is missing the Apache-2.0 license")
        missing_viewer_source = [
            asset
            for asset in REQUIRED_ASSETS
            if not _has_suffix(viewer_source_names, f"static/{asset}")
        ]
        if missing_viewer_source:
            raise CheckError(f"{viewer_sdist.name} is missing {missing_viewer_source}")
        unexpected_viewer_source = _unexpected_viewer_static(viewer_source_names)
        if unexpected_viewer_source:
            raise CheckError(
                f"{viewer_sdist.name} contains unexpected public static files: "
                f"{unexpected_viewer_source[:5]}"
            )
        if args.stage_dir is not None:
            stage = args.stage_dir.resolve()
            if stage.exists():
                raise CheckError(f"publish staging directory already exists: {stage}")
            core_stage = stage / "core"
            viewer_stage = stage / "viewer"
            core_stage.mkdir(parents=True)
            viewer_stage.mkdir(parents=True)
            for artifact in (base, base_sdist):
                shutil.copy2(artifact, core_stage / artifact.name)
            for artifact in (viewer, viewer_sdist):
                shutil.copy2(artifact, viewer_stage / artifact.name)
    except (CheckError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"FAIL: {exc}")
        return 1

    print(
        f"ok: {base.name} {size_mb:.2f} MB is viewer-free; "
        f"{viewer.name} carries the browser bundle; both source archives are complete"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
