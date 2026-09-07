"""Model session: the loaded IFC file, its worker, saves, and backups."""

from ifc_console.session.model import ModelSession
from ifc_console.session.working_copy import WorkingCopy, WorkingCopyStore

__all__ = ["ModelSession", "WorkingCopy", "WorkingCopyStore"]
