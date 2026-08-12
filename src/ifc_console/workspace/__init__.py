"""Workspace: the folder-scoped file index and the resident-model registry.

The single active model stays the default. This package adds the optional
second mode: index a folder, show what is there, and let extra models and
companion files (IDS, BCF) be attached read-only alongside the active model.
"""

from ifc_console.workspace.index import FileEntry, WorkspaceIndex
from ifc_console.workspace.kinds import KINDS, FileKind, describe_file, detect_kind
from ifc_console.workspace.registry import Attachment, ModelRegistry

__all__ = [
    "KINDS",
    "Attachment",
    "FileEntry",
    "FileKind",
    "ModelRegistry",
    "WorkspaceIndex",
    "describe_file",
    "detect_kind",
]
