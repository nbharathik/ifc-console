"""Release guard: the distribution includes code, types, and viewer assets.

The wheel and source archive must match the source version and carry the exact
reviewed browser bundle. Run after `uv build`.
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
WHEEL_LIMIT_MB = 10.0
REQUIRED_ASSETS = (
    "index.html",
    "app.css",
    "app.js",
    "parser.js",
    "worker.js",
    "chat.html",
    "chat.js",
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
_VIEWER_STATIC_PREFIX = "ifc_console/viewer/static/"
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
        wheel = _one(dist, f"ifc_console-{version}-*.whl", "wheel")
        sdist = _one(dist, f"ifc_console-{version}.tar.gz", "source archive")
        metadata = _check_metadata(
            wheel,
            name="ifc-console",
            version=version,
            python_range=python_range,
        )
        extras = {value.casefold() for value in metadata.get_all("Provides-Extra", [])}
        if "viewer" in extras:
            raise CheckError(f"{wheel.name} still declares the removed viewer extra")
        if any(
            (raw.partition(";")[0].strip().replace("_", "-").casefold()).startswith(
                "ifc-console-viewer"
            )
            for raw in metadata.get_all("Requires-Dist", [])
        ):
            raise CheckError(f"{wheel.name} still depends on ifc-console-viewer")

        wheel_names = _zip_names(wheel)
        if not _has_suffix(wheel_names, "ifc_console/py.typed"):
            raise CheckError(f"{wheel.name} is missing the PEP 561 py.typed marker")
        if not _has_suffix(wheel_names, ".dist-info/licenses/LICENSE"):
            raise CheckError(f"{wheel.name} is missing the Apache-2.0 license")
        missing = [
            asset
            for asset in REQUIRED_ASSETS
            if not _has_suffix(wheel_names, f"ifc_console/viewer/static/{asset}")
        ]
        if missing:
            raise CheckError(f"{wheel.name} is missing viewer assets: {missing}")
        unexpected_static = _unexpected_viewer_static(wheel_names)
        if unexpected_static:
            raise CheckError(
                f"{wheel.name} contains unexpected public static files: {unexpected_static[:5]}"
            )
        size_mb = wheel.stat().st_size / 1e6
        if size_mb > WHEEL_LIMIT_MB:
            raise CheckError(
                f"{wheel.name} is {size_mb:.2f} MB, over the {WHEEL_LIMIT_MB} MB budget"
            )

        source_names = _tar_names(sdist)
        required_source = ("CHANGELOG.md", "SECURITY.md", "src/ifc_console/py.typed")
        missing_source = [
            name for name in required_source if not _has_suffix(source_names, name)
        ]
        if missing_source:
            raise CheckError(f"{sdist.name} is missing {missing_source}")
        missing_source_assets = [
            asset
            for asset in REQUIRED_ASSETS
            if not _has_suffix(source_names, f"src/ifc_console/viewer/static/{asset}")
        ]
        if missing_source_assets:
            raise CheckError(f"{sdist.name} is missing viewer assets: {missing_source_assets}")
        unexpected_source_static = _unexpected_viewer_static(source_names)
        if unexpected_source_static:
            raise CheckError(
                f"{sdist.name} contains unexpected public static files: "
                f"{unexpected_source_static[:5]}"
            )
        leaked_source = [name for name in source_names if _source_entry_is_excluded(name)]
        if leaked_source:
            raise CheckError(f"excluded files leaked into {sdist.name}: {leaked_source[:5]}")
        if args.stage_dir is not None:
            stage = args.stage_dir.resolve()
            if stage.exists():
                raise CheckError(f"publish staging directory already exists: {stage}")
            stage.mkdir(parents=True)
            for artifact in (wheel, sdist):
                shutil.copy2(artifact, stage / artifact.name)
    except (CheckError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"FAIL: {exc}")
        return 1

    print(f"ok: {wheel.name} {size_mb:.2f} MB includes the viewer; source archive is complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
