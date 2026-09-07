"""Where the agent panel keeps its state: always under the console home.

Nothing the panel writes lands in the project folder, so a repository stays
clean and the same layout works for every user and OS. Shared state (library,
skills, custom agents, workflows, content access) lives directly under
`~/.ifc-console/agents/`; state tied to one project (its uploads, its recorded
skills, its conversations) lives under `~/.ifc-console/agents/projects/<key>/`
where the key is a hash of the project path.
"""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from typing import Any

AGENTS_DIRNAME = "agents"


def project_key(project_dir: str | Path) -> str:
    """Stable, non-identifying key for machine-local state for one project."""
    canonical = os.path.normcase(str(Path(project_dir).expanduser().resolve()))
    return sha256(canonical.encode("utf-8")).hexdigest()[:24]


def agents_dir(home: str | Path) -> Path:
    return Path(home).expanduser() / AGENTS_DIRNAME


def project_state_dir(home: str | Path, project_dir: str | Path) -> Path:
    return agents_dir(home) / "projects" / project_key(project_dir)


def reference_store(core: Any) -> Any:
    """Project-scoped uploads, kept under the home in the project's state folder."""
    from ifc_console_agents.files import AgentReferenceStore

    return AgentReferenceStore.for_project(core.store.home, core.store.project_dir)


def library_store(core: Any) -> Any:
    from ifc_console_agents.files import AgentReferenceStore

    return AgentReferenceStore.for_library(core.store.home)


def skill_store(core: Any) -> Any:
    from ifc_console_agents.skills import AgentSkillStore

    return AgentSkillStore(
        core.store.project_dir,
        user_dir=core.store.home,
        project_skills_dir=project_state_dir(core.store.home, core.store.project_dir) / "skills",
    )


def content_access_store(core: Any) -> Any:
    from ifc_console_agents.content import AgentContentAccessStore

    return AgentContentAccessStore(
        core.store.project_dir, path=agents_dir(core.store.home) / "content-access.json"
    )


def workflow_registry(core: Any) -> Any:
    from ifc_console_agents.workflows import WorkflowRegistry

    return WorkflowRegistry(
        core.store.project_dir, directory=agents_dir(core.store.home) / "workflows"
    )


def blueprints_dir(home: str | Path) -> Path:
    return agents_dir(home) / "custom"


__all__ = [
    "AGENTS_DIRNAME",
    "agents_dir",
    "blueprints_dir",
    "content_access_store",
    "library_store",
    "project_key",
    "project_state_dir",
    "reference_store",
    "skill_store",
    "workflow_registry",
]
