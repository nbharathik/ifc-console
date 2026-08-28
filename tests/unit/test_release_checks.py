import json
from pathlib import Path

import pytest

from scripts.check_release import release_issues
from scripts.check_wheels import (
    AGENT_STATIC_LIMIT_MB,
    REQUIRED_AGENT_ASSETS,
    REQUIRED_ASSETS,
    STATIC_LIMIT_MB,
    _canonical_python_range,
    _source_entry_is_excluded,
    _unexpected_agent_browser_assets,
    _unexpected_agent_files,
    _unexpected_agent_static,
    _unexpected_main_agent_files,
    _unexpected_main_browser_assets,
    _unexpected_shim_assets,
    _unexpected_shim_package_files,
    _unexpected_viewer_static,
    _unsafe_archive_name,
)

ROOT = Path(__file__).resolve().parents[2]


def test_main_package_does_not_install_an_agent_framework() -> None:
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dependencies = metadata.split("[project.optional-dependencies]", 1)[0].casefold()

    for agent_dependency in (
        "ifc-console-agents",
        "keyring",
        "langchain",
        "langgraph",
        "pymupdf",
        "pypdf",
    ):
        assert agent_dependency not in dependencies


def test_document_dependencies_are_optional_in_the_main_package() -> None:
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dependencies, extras = metadata.split("[project.optional-dependencies]", 1)
    dependencies = dependencies.casefold()
    extras = extras.casefold()
    documents = extras.split("documents = [", 1)[1].split("]", 1)[0]
    pdf = extras.split("pdf = [", 1)[1].split("]", 1)[0]

    assert '"pypdf>=4"' not in dependencies
    assert '"pymupdf>=1.24,<2"' not in dependencies
    assert '"pypdf>=4"' in documents
    assert '"pymupdf>=1.24,<2"' in documents
    assert '"pypdf>=4"' in pdf
    assert '"pymupdf>=1.24,<2"' in pdf


def test_main_package_does_not_install_agent_credential_storage() -> None:
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dependencies, extras = metadata.split("[project.optional-dependencies]", 1)
    dependencies = dependencies.casefold()
    extras = extras.casefold()

    assert '"keyring>=24"' not in dependencies
    assert 'graph = ["ifc-console-agents>=0.1.4,<0.2"]' in extras
    assert 'keys = ["ifc-console-agents>=0.1.4,<0.2"]' in extras


def test_main_package_bundles_the_viewer_transport_and_keeps_the_old_extra_empty() -> None:
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dependencies, extras = metadata.split("[project.optional-dependencies]", 1)

    assert '"websockets>=12"' in dependencies
    assert "viewer = []" in extras
    assert '"ifc-console-viewer' not in dependencies


def test_viewer_compatibility_package_depends_on_core_and_contains_no_assets() -> None:
    shim = ROOT / "packages" / "ifc-console-viewer"
    if not shim.is_dir():
        pytest.skip("the compatibility workspace package is not in this source archive")
    metadata = (shim / "pyproject.toml").read_text(encoding="utf-8")

    assert 'dependencies = ["ifc-console>=0.1,<0.2"]' in metadata
    assert not (shim / "src" / "ifc_console_viewer" / "static").exists()


def test_agent_package_owns_agent_dependencies_and_a_separate_namespace() -> None:
    project = ROOT / "packages" / "ifc-console-agents"
    if not project.is_dir():
        pytest.skip("the agents workspace package is not in this source archive")
    metadata = (project / "pyproject.toml").read_text(encoding="utf-8").casefold()
    dependencies = metadata.split("[project.urls]", 1)[0]

    assert '"ifc-console>=0.1.4,<0.2"' in dependencies
    assert '"keyring>=24"' in dependencies
    assert '"pypdf>=4"' in dependencies
    assert '"pymupdf>=1.24,<2"' in dependencies
    assert '"langgraph>=1,<2"' in dependencies
    assert '"langgraph-checkpoint-sqlite>=3,<4"' in dependencies
    assert "[project.optional-dependencies]" not in metadata
    assert not (project / "src" / "ifc_console").exists()


def _project(
    root: Path,
    *,
    core: str = "0.1.4",
    agent: str = "0.1.4",
    viewer: str = "0.1.4",
    viewer_runtime: str | None = None,
    core_requirement: str = ">=0.1,<0.2",
    agent_requirement: str = ">=0.1.4,<0.2",
    release_label: str = "2026-08-09",
) -> None:
    core_dir = root / "src" / "ifc_console"
    agent_dir = root / "packages" / "ifc-console-agents"
    agent_package = agent_dir / "src" / "ifc_console_agents"
    viewer_dir = root / "packages" / "ifc-console-viewer"
    viewer_package = viewer_dir / "src" / "ifc_console_viewer"
    core_dir.mkdir(parents=True)
    agent_package.mkdir(parents=True)
    viewer_package.mkdir(parents=True)
    (core_dir / "__init__.py").write_text(f'__version__ = "{core}"\n', encoding="utf-8")
    (agent_package / "__init__.py").write_text(
        f'__version__ = "{agent}"\n', encoding="utf-8"
    )
    for relative in (
        "agents/__init__.py",
        "chat/__init__.py",
        "credentials.py",
        "devkit/__init__.py",
        "integrations/langgraph.py",
        "mcp/tools_skills.py",
        "testing.py",
    ):
        shim = core_dir / relative
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text("# compatibility shim\n", encoding="utf-8")
    (viewer_package / "__init__.py").write_text(
        f'__version__ = "{viewer_runtime or viewer}"\n', encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        'requires-python = ">=3.10,<3.15"\n'
        f'graph = ["ifc-console-agents{agent_requirement}"]\n'
        "viewer = []\n"
        f'keys = ["ifc-console-agents{agent_requirement}"]\n',
        encoding="utf-8",
    )
    (viewer_dir / "pyproject.toml").write_text(
        f'version = "{viewer}"\nrequires-python = ">=3.10,<3.15"\n'
        f'dependencies = ["ifc-console{core_requirement}"]\n',
        encoding="utf-8",
    )
    (agent_dir / "pyproject.toml").write_text(
        'requires-python = ">=3.10,<3.15"\n'
        f'dependencies = ["ifc-console{agent_requirement}"]\n',
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(f"## [{core}] - {release_label}\n", encoding="utf-8")


def test_release_metadata_accepts_matching_versions_and_tag(tmp_path: Path) -> None:
    _project(tmp_path)

    version, issues = release_issues(tmp_path, tag="v0.1.4")

    assert version == "0.1.4"
    assert issues == []


def test_release_metadata_reports_every_consistency_problem(tmp_path: Path) -> None:
    _project(
        tmp_path,
        core="0.1.4",
        agent="0.2.0",
        viewer="0.3.0",
        viewer_runtime="0.4.0",
    )
    (tmp_path / "packages" / "ifc-console-viewer" / "pyproject.toml").write_text(
        'version = "0.3.0"\nrequires-python = ">=3.11"\n'
        'dependencies = ["ifc-console>=0.3,<0.4"]\n',
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text("## [0.1.4]\n", encoding="utf-8")
    (tmp_path / "packages" / "ifc-console-agents" / "pyproject.toml").write_text(
        'requires-python = ">=3.12"\n'
        'dependencies = ["ifc-console>=0.2,<0.3"]\n',
        encoding="utf-8",
    )

    _, issues = release_issues(tmp_path, tag="v0.1")

    assert any("package versions differ" in issue for issue in issues)
    assert any("ifc-console-agents" in issue for issue in issues)
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


def test_release_rejects_a_shim_range_that_crosses_minor_versions(tmp_path: Path) -> None:
    _project(tmp_path, core_requirement=">=0.1,<1")

    _, issues = release_issues(tmp_path)

    assert any("compatibility shim dependency range" in issue for issue in issues)


def test_release_rejects_an_agent_range_that_accepts_older_core_patches(
    tmp_path: Path,
) -> None:
    _project(tmp_path, agent_requirement=">=0.1,<0.2")

    _, issues = release_issues(tmp_path)

    assert any("agent core dependency range" in issue for issue in issues)


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


def test_viewer_static_allowlist_matches_the_shipped_tree() -> None:
    static = ROOT / "src" / "ifc_console" / "viewer" / "static"
    shipped = {path.relative_to(static).as_posix() for path in static.rglob("*") if path.is_file()}

    assert shipped == set(REQUIRED_ASSETS)
    installed_mb = sum(path.stat().st_size for path in static.rglob("*") if path.is_file()) / 1e6
    assert installed_mb <= STATIC_LIMIT_MB


def test_main_wheel_allows_browser_assets_only_in_the_reviewed_tree() -> None:
    expected = [f"ifc_console/viewer/static/{asset}" for asset in REQUIRED_ASSETS]
    assert _unexpected_main_browser_assets(expected) == []
    assert _unexpected_main_browser_assets(
        [
            *expected,
            "ifc_console/examples/demo/static/config.json",
            "ifc_console/examples/demo/static/app.js",
            "ifc_console_viewer/static/app.js",
            "ifc_console/vendor/web-ifc.wasm",
        ]
    ) == [
        "ifc_console/examples/demo/static/app.js",
        "ifc_console/examples/demo/static/config.json",
        "ifc_console/vendor/web-ifc.wasm",
        "ifc_console_viewer/static/app.js",
    ]


def test_agent_static_allowlist_matches_the_separate_package() -> None:
    static = (
        ROOT
        / "packages"
        / "ifc-console-agents"
        / "src"
        / "ifc_console_agents"
        / "static"
    )
    if not static.is_dir():
        pytest.skip("the agents workspace package is not in this source archive")
    shipped = {path.relative_to(static).as_posix() for path in static.rglob("*") if path.is_file()}

    assert shipped == set(REQUIRED_AGENT_ASSETS)
    installed_mb = sum(path.stat().st_size for path in static.rglob("*") if path.is_file()) / 1e6
    assert installed_mb <= AGENT_STATIC_LIMIT_MB


def test_agent_static_allowlist_rejects_unexpected_public_files() -> None:
    expected = [f"ifc_console_agents/static/{asset}" for asset in REQUIRED_AGENT_ASSETS]
    expected_source = [
        f"ifc_console_agents-0.1.4/src/ifc_console_agents/static/{asset}"
        for asset in REQUIRED_AGENT_ASSETS
    ]

    assert _unexpected_agent_static(expected) == []
    assert _unexpected_agent_static(expected_source) == []
    assert _unexpected_agent_static(
        [*expected, "ifc_console_agents/static/.env"]
    ) == ["ifc_console_agents/static/.env"]
    assert _unexpected_agent_static(
        [*expected_source, "ifc_console_agents-0.1.4/src/ifc_console_agents/static/secrets.json"]
    ) == ["ifc_console_agents-0.1.4/src/ifc_console_agents/static/secrets.json"]


def test_agent_distribution_cannot_overlap_core_or_escape_its_static_tree() -> None:
    expected = [f"ifc_console_agents/static/{asset}" for asset in REQUIRED_AGENT_ASSETS]

    assert _unexpected_agent_browser_assets(expected) == []
    assert _unexpected_agent_browser_assets(
        [*expected, "ifc_console_agents/demo/static/app.js"]
    ) == ["ifc_console_agents/demo/static/app.js"]
    assert _unexpected_agent_files(["ifc_console_agents/agent.py"]) == []
    assert _unexpected_agent_files(
        ["ifc_console/agents/agent.py", "pkg/src/ifc_console/chat/routes.py"]
    ) == ["ifc_console/agents/agent.py", "pkg/src/ifc_console/chat/routes.py"]


def test_main_distribution_allows_only_the_one_release_agent_shims() -> None:
    assert _unexpected_main_agent_files(
        [
            "ifc_console/extensions.py",
            "ifc_console/agents/__init__.py",
            "ifc_console/chat/__init__.py",
            "ifc_console/credentials.py",
            "ifc_console/devkit/__init__.py",
            "ifc_console/integrations/langgraph.py",
            "ifc_console/mcp/tools_skills.py",
            "ifc_console/testing.py",
        ]
    ) == []
    assert _unexpected_main_agent_files(
        [
            "ifc_console/agents/agent.py",
            "ifc_console/chat/routes.py",
            "ifc_console/devkit/serve.py",
            "ifc_console_agents/agent.py",
        ]
    ) == [
        "ifc_console/agents/agent.py",
        "ifc_console/chat/routes.py",
        "ifc_console/devkit/serve.py",
        "ifc_console_agents/agent.py",
    ]


def test_compatibility_shim_rejects_browser_assets() -> None:
    assert _unexpected_shim_assets(["ifc_console_viewer/__init__.py"]) == []
    assert _unexpected_shim_assets(
        ["ifc_console_viewer/static/app.js", "ifc_console_viewer/vendor/web-ifc.wasm"]
    ) == ["ifc_console_viewer/static/app.js", "ifc_console_viewer/vendor/web-ifc.wasm"]


def test_compatibility_shim_contains_only_the_forwarding_module() -> None:
    assert _unexpected_shim_package_files(["ifc_console_viewer/__init__.py"]) == []
    assert _unexpected_shim_package_files(
        [
            "ifc_console_viewer/__init__.py",
            "ifc_console_viewer/assets.py",
            "ifc_console_viewer-0.1.4/src/ifc_console_viewer/routes.py",
        ]
    ) == [
        "ifc_console_viewer-0.1.4/src/ifc_console_viewer/routes.py",
        "ifc_console_viewer/assets.py",
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
