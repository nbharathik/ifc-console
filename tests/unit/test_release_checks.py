import json
import re
from pathlib import Path

import pytest

from scripts.check_release import release_issues
from scripts.check_wheels import (
    REQUIRED_ASSETS,
    _canonical_python_range,
    _source_entry_is_excluded,
    _unexpected_viewer_static,
    _unsafe_archive_name,
)

ROOT = Path(__file__).resolve().parents[2]


def _project(
    root: Path,
    *,
    core: str = "0.1.4",
    release_label: str = "2026-08-09",
) -> None:
    core_dir = root / "src" / "ifc_console"
    core_dir.mkdir(parents=True)
    (core_dir / "__init__.py").write_text(f'__version__ = "{core}"\n', encoding="utf-8")
    (root / "CHANGELOG.md").write_text(f"## [{core}] - {release_label}\n", encoding="utf-8")


def test_release_metadata_accepts_matching_versions_and_tag(tmp_path: Path) -> None:
    _project(tmp_path)

    version, issues = release_issues(tmp_path, tag="v0.1.4")

    assert version == "0.1.4"
    assert issues == []


def test_release_metadata_reports_tag_and_changelog_problems(tmp_path: Path) -> None:
    _project(tmp_path, core="0.1.4")
    (tmp_path / "CHANGELOG.md").write_text("## [0.1.4]\n", encoding="utf-8")

    _, issues = release_issues(tmp_path, tag="v0.1")

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


def test_tool_reference_covers_the_public_operation_contract() -> None:
    contract = json.loads(
        (ROOT / "tests" / "golden" / "api_contract.json").read_text(encoding="utf-8")
    )
    reference = (ROOT / "docs" / "tools.md").read_text(encoding="utf-8")

    missing = [
        tool["name"]
        for tool in contract["tools"]
        if re.search(rf"^### {re.escape(tool['name'])}$", reference, re.MULTILINE) is None
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
    expected = [f"ifc_console/viewer/static/{asset}" for asset in REQUIRED_ASSETS]
    expected_source = [
        f"ifc_console-0.1.4/src/ifc_console/viewer/static/{asset}"
        for asset in REQUIRED_ASSETS
    ]

    assert _unexpected_viewer_static(expected) == []
    assert _unexpected_viewer_static(expected_source) == []
    assert _unexpected_viewer_static([*expected, "ifc_console/viewer/static/.env"]) == [
        "ifc_console/viewer/static/.env"
    ]
    assert _unexpected_viewer_static(
        [
            *expected_source,
            "ifc_console-0.1.4/src/ifc_console/viewer/static/secrets.json",
        ]
    ) == ["ifc_console-0.1.4/src/ifc_console/viewer/static/secrets.json"]
    assert _unexpected_viewer_static([*expected, "ifc_console/viewer/not-public.txt"]) == []


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
