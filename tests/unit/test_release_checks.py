import json
import re
from pathlib import Path

import pytest

from scripts.check_release import EXTRAS, RETIRED_PATHS, release_issues
from scripts.check_wheels import (
    AGENT_STATIC_LIMIT_MB,
    OPTIONAL_ONLY,
    REQUIRED_AGENT_ASSETS,
    REQUIRED_ASSETS,
    STATIC_LIMIT_MB,
    _canonical_python_range,
    _retired_files,
    _source_entry_is_excluded,
    _unexpected_agent_static,
    _unexpected_browser_assets,
    _unexpected_viewer_static,
    _unsafe_archive_name,
)

ROOT = Path(__file__).resolve().parents[2]


def _sections() -> tuple[str, str]:
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dependencies, extras = metadata.split("[project.optional-dependencies]", 1)
    return dependencies.casefold(), extras.split("\n[", 1)[0].casefold()


def test_base_install_leaves_optional_packages_to_the_extras() -> None:
    dependencies, _ = _sections()
    listed = set(re.findall(r'^\s*"([A-Za-z0-9_.-]+)', dependencies, flags=re.MULTILINE))

    assert not {name.casefold() for name in listed} & OPTIONAL_ONLY


def test_extras_are_exactly_agents_validation_and_all() -> None:
    _, extras = _sections()
    names = set(re.findall(r"^([a-z0-9_-]+) = \[", extras, flags=re.MULTILINE))

    assert names == EXTRAS


def test_the_agents_extra_adds_only_the_outward_facing_packages() -> None:
    _, extras = _sections()
    agents = extras.split("agents = [", 1)[1].split("]", 1)[0]
    everything = extras.split("all = [", 1)[1].split("]", 1)[0]

    for package in ("agent-client-protocol", "keyring", "pypdfium2"):
        assert f'"{package}' in agents
        assert f'"{package}' in everything
    assert "ifctester" not in agents
    assert "ifctester" in everything
    assert "ifctester" in extras.split("validation = [", 1)[1].split("]", 1)[0]


def test_contributor_tools_are_dependency_groups_not_extras() -> None:
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    groups = metadata.split("[dependency-groups]", 1)[1].split("\n[", 1)[0]

    assert "dev = [" in groups
    assert "docs = [" in groups
    assert '"pytest' in groups
    assert '"mkdocs' in groups


def test_trimesh_is_a_base_dependency() -> None:
    dependencies, _ = _sections()

    assert '"trimesh>=5.0.0,<5.1"' in dependencies


def test_main_package_bundles_the_viewer_transport() -> None:
    dependencies, _ = _sections()

    assert '"websockets>=12"' in dependencies
    assert "ifc-console-viewer" not in dependencies


def test_there_is_one_package_and_no_redirect_project() -> None:
    assert not (ROOT / "packages" / "ifc-console-agents").exists()
    assert not (ROOT / "packages" / "ifc-console-viewer").exists()


def _project(
    root: Path,
    *,
    core: str = "0.2.0",
    extras: tuple[str, ...] = ("agents", "validation", "all"),
    release_label: str = "2026-09-29",
) -> None:
    (root / "src" / "ifc_console").mkdir(parents=True)
    (root / "src" / "ifc_console" / "__init__.py").write_text(
        f'__version__ = "{core}"\n', encoding="utf-8"
    )
    listing = "".join(f'{name} = ["x"]\n' for name in extras)
    (root / "pyproject.toml").write_text(
        f'[project.optional-dependencies]\n{listing}\n[project.urls]\nHomepage = "x"\n',
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(f"## [{core}] - {release_label}\n", encoding="utf-8")


def test_release_metadata_accepts_matching_versions_and_tag(tmp_path: Path) -> None:
    _project(tmp_path)

    version, issues = release_issues(tmp_path, tag="v0.2.0")

    assert version == "0.2.0"
    assert issues == []


def test_release_metadata_reports_every_consistency_problem(tmp_path: Path) -> None:
    _project(tmp_path, extras=("agents", "pdf"))
    (tmp_path / "CHANGELOG.md").write_text("## [0.2.0]\n", encoding="utf-8")
    (tmp_path / "packages" / "ifc-console-viewer").mkdir(parents=True)

    _, issues = release_issues(tmp_path, tag="v0.2")

    assert any("extras are" in issue for issue in issues)
    assert any("retired path is back" in issue for issue in issues)
    assert any("release tag" in issue for issue in issues)
    assert any("release heading" in issue for issue in issues)


@pytest.mark.parametrize("relative", RETIRED_PATHS)
def test_every_retired_path_is_reported_when_it_returns(tmp_path: Path, relative: str) -> None:
    _project(tmp_path)
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix:
        target.write_text("", encoding="utf-8")
    else:
        target.mkdir()

    _, issues = release_issues(tmp_path)

    assert any(relative in issue for issue in issues)


def test_tagged_release_rejects_an_unreleased_changelog(tmp_path: Path) -> None:
    _project(tmp_path, release_label="Unreleased")

    assert release_issues(tmp_path)[1] == []
    _, tagged_issues = release_issues(tmp_path, tag="v0.2.0")

    assert any("release date" in issue for issue in tagged_issues)


@pytest.mark.parametrize("release_label", ["20260929", "2026-W39-7", "2026-02-30"])
def test_tagged_release_requires_a_real_calendar_date(tmp_path: Path, release_label: str) -> None:
    _project(tmp_path, release_label=release_label)

    _, issues = release_issues(tmp_path, tag="v0.2.0")

    assert any("release date" in issue for issue in issues)


def test_tool_reference_covers_the_public_operation_contract() -> None:
    contract = json.loads(
        (ROOT / "tests" / "golden" / "api_contract.json").read_text(encoding="utf-8")
    )
    reference = (ROOT / "docs" / "tools.md").read_text(encoding="utf-8")

    missing = [tool["name"] for tool in contract["tools"] if f"`{tool['name']}`" not in reference]

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

    assert not _unsafe_archive_name("ifc_console-0.2.0/src/ifc_console/__init__.py")


def test_release_python_range_comparison_ignores_metadata_order() -> None:
    assert _canonical_python_range(">=3.10,<3.15") == _canonical_python_range("<3.15, >=3.10")


def test_viewer_static_allowlist_rejects_unexpected_public_files() -> None:
    expected = [f"ifc_console/viewer/static/{asset}" for asset in REQUIRED_ASSETS]
    expected_source = [
        f"ifc_console-0.2.0/src/ifc_console/viewer/static/{asset}" for asset in REQUIRED_ASSETS
    ]

    assert _unexpected_viewer_static(expected) == []
    assert _unexpected_viewer_static(expected_source) == []
    assert _unexpected_viewer_static([*expected, "ifc_console/viewer/static/.env"]) == [
        "ifc_console/viewer/static/.env"
    ]
    assert _unexpected_viewer_static(
        [*expected_source, "ifc_console-0.2.0/src/ifc_console/viewer/static/secrets.json"]
    ) == ["ifc_console-0.2.0/src/ifc_console/viewer/static/secrets.json"]
    assert _unexpected_viewer_static([*expected, "ifc_console/viewer/not-public.txt"]) == []


def test_viewer_static_allowlist_matches_the_shipped_tree() -> None:
    static = ROOT / "src" / "ifc_console" / "viewer" / "static"
    shipped = {path.relative_to(static).as_posix() for path in static.rglob("*") if path.is_file()}

    assert shipped == set(REQUIRED_ASSETS)
    installed_mb = sum(path.stat().st_size for path in static.rglob("*") if path.is_file()) / 1e6
    assert installed_mb <= STATIC_LIMIT_MB


def test_agent_static_allowlist_matches_the_shipped_tree() -> None:
    static = ROOT / "src" / "ifc_console" / "agents" / "static"
    shipped = {path.relative_to(static).as_posix() for path in static.rglob("*") if path.is_file()}

    assert shipped == set(REQUIRED_AGENT_ASSETS)
    installed_mb = sum(path.stat().st_size for path in static.rglob("*") if path.is_file()) / 1e6
    assert installed_mb <= AGENT_STATIC_LIMIT_MB


def test_agent_static_allowlist_rejects_unexpected_public_files() -> None:
    expected = [f"ifc_console/agents/static/{asset}" for asset in REQUIRED_AGENT_ASSETS]
    expected_source = [
        f"ifc_console-0.2.0/src/ifc_console/agents/static/{asset}"
        for asset in REQUIRED_AGENT_ASSETS
    ]

    assert _unexpected_agent_static(expected) == []
    assert _unexpected_agent_static(expected_source) == []
    assert _unexpected_agent_static([*expected, "ifc_console/agents/static/.env"]) == [
        "ifc_console/agents/static/.env"
    ]


def test_browser_assets_are_allowed_only_in_the_two_reviewed_trees() -> None:
    expected = [f"ifc_console/viewer/static/{asset}" for asset in REQUIRED_ASSETS]
    expected += [f"ifc_console/agents/static/{asset}" for asset in REQUIRED_AGENT_ASSETS]

    assert _unexpected_browser_assets(expected) == []
    assert _unexpected_browser_assets(
        [
            *expected,
            "ifc_console/examples/demo/static/app.js",
            "ifc_console/agents/demo/static/app.js",
            "ifc_console/vendor/web-ifc.wasm",
        ]
    ) == [
        "ifc_console/agents/demo/static/app.js",
        "ifc_console/examples/demo/static/app.js",
        "ifc_console/vendor/web-ifc.wasm",
    ]


def test_wheels_reject_the_files_retired_in_0_2_0() -> None:
    assert _retired_files(
        [
            "ifc_console/extensions.py",
            "ifc_console/agents/devkit/serve.py",
            "ifc_console/agents/chat/agent.py",
        ]
    ) == []
    assert _retired_files(
        [
            "ifc_console/chat/routes.py",
            "ifc_console/devkit/serve.py",
            "ifc_console/credentials.py",
            "ifc_console_agents/agent.py",
            "ifc_console_viewer/__init__.py",
            "ifc_console-0.2.0/src/ifc_console/testing.py",
        ]
    ) == [
        "ifc_console-0.2.0/src/ifc_console/testing.py",
        "ifc_console/chat/routes.py",
        "ifc_console/credentials.py",
        "ifc_console/devkit/serve.py",
        "ifc_console_agents/agent.py",
        "ifc_console_viewer/__init__.py",
    ]


@pytest.mark.parametrize(
    "name",
    [
        "ifc_console-0.2.0/.tmp/cache.bin",
        "ifc_console-0.2.0/.vscode/settings.json",
        "ifc_console-0.2.0/uv.lock",
        "ifc_console-0.2.0/docs/assets/brand/console.png",
        r"ifc_console-0.2.0\.tmp\cache.bin",
    ],
)
def test_source_archive_exclusion_recognizes_sensitive_entries(name: str) -> None:
    assert _source_entry_is_excluded(name)


@pytest.mark.parametrize(
    "name",
    [
        "ifc_console-0.2.0/src/ifc_console/__init__.py",
        "ifc_console-0.2.0/docs/assets/brand/console.svg",
        "ifc_console-0.2.0/docs/uv.lock",
    ],
)
def test_source_archive_exclusion_allows_intended_entries(name: str) -> None:
    assert not _source_entry_is_excluded(name)
