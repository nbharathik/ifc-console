import json
from pathlib import Path

import pytest

from scripts.check_release import release_issues
from scripts.check_wheels import (
    REQUIRED_ASSETS,
    SDK_CHAT_ASSETS,
    _canonical_python_range,
    _source_entry_is_excluded,
    _unexpected_base_browser_assets,
    _unexpected_viewer_static,
    _unsafe_archive_name,
)

ROOT = Path(__file__).resolve().parents[2]


def _project(
    root: Path,
    *,
    core: str = "0.1.4",
    viewer: str = "0.1.4",
    viewer_runtime: str | None = None,
    viewer_requirement: str = ">=0.1,<0.2",
    release_label: str = "2026-08-09",
) -> None:
    core_dir = root / "src" / "ifc_console"
    viewer_dir = root / "packages" / "ifc-console-viewer"
    viewer_package = viewer_dir / "src" / "ifc_console_viewer"
    core_dir.mkdir(parents=True)
    viewer_package.mkdir(parents=True)
    (core_dir / "__init__.py").write_text(f'__version__ = "{core}"\n', encoding="utf-8")
    (viewer_package / "__init__.py").write_text(
        f'__version__ = "{viewer_runtime or viewer}"\n', encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        f'requires-python = ">=3.10,<3.15"\n"ifc-console-viewer{viewer_requirement}",\n',
        encoding="utf-8",
    )
    (viewer_dir / "pyproject.toml").write_text(
        f'version = "{viewer}"\nrequires-python = ">=3.10,<3.15"\n', encoding="utf-8"
    )
    (root / "CHANGELOG.md").write_text(f"## [{core}] - {release_label}\n", encoding="utf-8")


def test_release_metadata_accepts_matching_versions_and_tag(tmp_path: Path) -> None:
    _project(tmp_path)

    version, issues = release_issues(tmp_path, tag="v0.1.4")

    assert version == "0.1.4"
    assert issues == []


def test_release_metadata_reports_every_consistency_problem(tmp_path: Path) -> None:
    _project(tmp_path, core="0.1.4", viewer="0.3.0", viewer_runtime="0.4.0")
    (tmp_path / "packages" / "ifc-console-viewer" / "pyproject.toml").write_text(
        'version = "0.3.0"\nrequires-python = ">=3.11"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text("## [0.1.4]\n", encoding="utf-8")

    _, issues = release_issues(tmp_path, tag="v0.1")

    assert any("package versions differ" in issue for issue in issues)
    assert any("viewer versions differ" in issue for issue in issues)
    assert any("Python ranges differ" in issue for issue in issues)
    assert any("release tag" in issue for issue in issues)
    assert any("release heading" in issue for issue in issues)


def test_tagged_release_rejects_an_unreleased_changelog(tmp_path: Path) -> None:
    _project(tmp_path, release_label="Unreleased")

    assert release_issues(tmp_path)[1] == []
    _, tagged_issues = release_issues(tmp_path, tag="v0.1.4")

    assert any("release date" in issue for issue in tagged_issues)


@pytest.mark.parametrize("release_label", ["20260809", "2026-W32-7", "2026-02-30"])
def test_tagged_release_requires_a_real_calendar_date(tmp_path: Path, release_label: str) -> None:
    _project(tmp_path, release_label=release_label)

    _, issues = release_issues(tmp_path, tag="v0.1.4")

    assert any("release date" in issue for issue in issues)


def test_release_rejects_a_viewer_range_that_crosses_minor_versions(tmp_path: Path) -> None:
    _project(tmp_path, viewer_requirement=">=0.1,<1")

    _, issues = release_issues(tmp_path)

    assert any("viewer dependency range" in issue for issue in issues)


def test_tool_reference_covers_the_public_operation_contract() -> None:
    contract = json.loads(
        (ROOT / "tests" / "golden" / "api_contract.json").read_text(encoding="utf-8")
    )
    reference = (ROOT / "docs" / "tools.md").read_text(encoding="utf-8")

    missing = [
        tool["name"]
        for tool in contract["tools"]
        if f"`{tool['name']}`" not in reference
    ]

    assert missing == [], f"public operations missing from docs/tools.md: {missing}"


def test_tool_reference_covers_the_error_code_registry() -> None:
    contract = json.loads(
        (ROOT / "tests" / "golden" / "api_contract.json").read_text(encoding="utf-8")
    )
    reference = (ROOT / "docs" / "tools.md").read_text(encoding="utf-8")

    missing = [code for code in contract["error_codes"] if f"`{code}`" not in reference]

    assert missing == [], f"public error codes missing from docs/tools.md: {missing}"


def test_release_archive_paths_reject_traversal_and_platform_absolute_names() -> None:
    for name in (
        "../outside",
        "pkg/../../outside",
        "/absolute",
        r"\rooted",
        r"C:\absolute",
        "pkg/file:stream",
        "pkg/NUL.txt",
        "pkg/trailing. ",
    ):
        assert _unsafe_archive_name(name)

    assert not _unsafe_archive_name("ifc_console-0.1.4/src/ifc_console/__init__.py")


def test_release_python_range_comparison_ignores_metadata_order() -> None:
    assert _canonical_python_range(">=3.10,<3.15") == _canonical_python_range("<3.15, >=3.10")


def test_viewer_static_allowlist_rejects_unexpected_public_files() -> None:
    expected = [f"ifc_console_viewer/static/{asset}" for asset in REQUIRED_ASSETS]
    expected_source = [
        f"ifc_console_viewer-0.1.4/src/ifc_console_viewer/static/{asset}"
        for asset in REQUIRED_ASSETS
    ]

    assert _unexpected_viewer_static(expected) == []
    assert _unexpected_viewer_static(expected_source) == []
    assert _unexpected_viewer_static([*expected, "ifc_console_viewer/static/.env"]) == [
        "ifc_console_viewer/static/.env"
    ]
    assert _unexpected_viewer_static(
        [
            *expected_source,
            "ifc_console_viewer-0.1.4/src/ifc_console_viewer/static/secrets.json",
        ]
    ) == ["ifc_console_viewer-0.1.4/src/ifc_console_viewer/static/secrets.json"]
    assert _unexpected_viewer_static([*expected, "ifc_console_viewer/not-public.txt"]) == []


def test_core_wheel_allows_only_the_small_sdk_chat_assets() -> None:
    expected = [
        f"ifc_console/examples/agent_chat/static/{asset}" for asset in SDK_CHAT_ASSETS
    ]

    assert _unexpected_base_browser_assets(expected) == []
    assert _unexpected_base_browser_assets(
        [
            *expected,
            "ifc_console/examples/agent_chat/static/config.json",
            "ifc_console_viewer/static/app.js",
            "ifc_console/vendor/web-ifc.wasm",
        ]
    ) == [
        "ifc_console/examples/agent_chat/static/config.json",
        "ifc_console/vendor/web-ifc.wasm",
        "ifc_console_viewer/static/app.js",
    ]


@pytest.mark.parametrize(
    "name",
    [
        "ifc_console-0.1.4/.tmp/cache.bin",
        "ifc_console-0.1.4/.vscode/settings.json",
        "ifc_console-0.1.4/uv.lock",
        "ifc_console-0.1.4/docs/assets/brand/console.png",
        r"ifc_console-0.1.4\.tmp\cache.bin",
    ],
)
def test_source_archive_exclusion_recognizes_sensitive_entries(name: str) -> None:
    assert _source_entry_is_excluded(name)


@pytest.mark.parametrize(
    "name",
    [
        "ifc_console-0.1.4/src/ifc_console/__init__.py",
        "ifc_console-0.1.4/docs/assets/brand/console.svg",
        "ifc_console-0.1.4/docs/uv.lock",
    ],
)
def test_source_archive_exclusion_allows_intended_entries(name: str) -> None:
    assert not _source_entry_is_excluded(name)
