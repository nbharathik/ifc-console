"""Resource caps for the worker process, on whichever OS this is.

Two halves, because the useful primitives live on different sides:

- The worker caps itself (POSIX rlimits) before it imports anything heavy.
  Lowering the hard limit too makes it irreversible, so user code cannot
  raise it later.
- The parent puts the worker in a Windows job object, which caps memory,
  forbids child processes outright, and kills the worker if the console
  itself dies. No orphaned sandboxes.

Every limit degrades to a no-op with a recorded reason rather than failing
the run; the audit hook is the boundary, these are the blast radius.
"""

from __future__ import annotations

import contextlib
import os
import sys

_MB = 1024 * 1024


def apply_self_limits(memory_mb: int) -> list[str]:
    """Called by the worker on itself. Returns the limits that took effect."""
    applied: list[str] = []
    with contextlib.suppress(Exception):
        if hasattr(os, "setsid"):
            os.setsid()
            applied.append("process-group")
    try:
        import resource
    except ImportError:
        return applied  # Windows: the parent's job object covers this
    if memory_mb > 0:
        for name in ("RLIMIT_AS", "RLIMIT_DATA"):
            limit = getattr(resource, name, None)
            if limit is None:
                continue
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(limit, (memory_mb * _MB, memory_mb * _MB))
                applied.append(f"{name.lower()}={memory_mb}mb")
                break
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        applied.append("no-core-dumps")
    return applied


class ProcessJail:
    """Parent-side containment for one worker process.

    On Windows this is a real job object. Elsewhere it is a thin wrapper that
    kills the worker's process group, which the worker created for itself.
    """

    def __init__(self, memory_mb: int, *, single_process: bool = True) -> None:
        self.memory_mb = memory_mb
        self.single_process = single_process
        self.controls: list[str] = []
        self._handle = None
        self._pid: int | None = None

    def attach(self, pid: int) -> None:
        self._pid = pid
        if sys.platform != "win32":
            self.controls.append("process-group-kill")
            return
        with contextlib.suppress(Exception):
            self._attach_windows(pid)

    def _attach_windows(self, pid: int) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_void_p),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        limit_process_memory = 0x00000100
        limit_job_memory = 0x00000200
        limit_active_process = 0x00000008
        limit_kill_on_job_close = 0x00002000
        limit_die_on_unhandled_exception = 0x00000400
        extended_limit_information = 9
        process_set_quota = 0x0100
        process_terminate = 0x0001

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        flags = limit_kill_on_job_close | limit_die_on_unhandled_exception
        if self.single_process:
            # One process per job: the worker cannot spawn anything, whatever
            # it manages to call.
            info.BasicLimitInformation.ActiveProcessLimit = 1
            flags |= limit_active_process
        if self.memory_mb > 0:
            info.ProcessMemoryLimit = self.memory_mb * _MB
            info.JobMemoryLimit = self.memory_mb * _MB
            flags |= limit_process_memory | limit_job_memory
        info.BasicLimitInformation.LimitFlags = flags

        if not kernel32.SetInformationJobObject(
            job, extended_limit_information, ctypes.byref(info), ctypes.sizeof(info)
        ):
            kernel32.CloseHandle(job)
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

        handle = kernel32.OpenProcess(process_set_quota | process_terminate, False, pid)
        if not handle:
            kernel32.CloseHandle(job)
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not kernel32.AssignProcessToJobObject(job, handle):
                kernel32.CloseHandle(job)
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        finally:
            kernel32.CloseHandle(handle)

        self._handle = job
        self._kernel32 = kernel32
        self.controls.append("job-object")
        if self.single_process:
            self.controls.append("no-child-processes")
        if self.memory_mb > 0:
            self.controls.append(f"memory={self.memory_mb}mb")
        self.controls.append("killed-with-console")

    def kill(self) -> None:
        """Stop the worker and anything it managed to start."""
        if sys.platform != "win32" and self._pid is not None and hasattr(os, "killpg"):
            import signal

            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(os.getpgid(self._pid), signal.SIGKILL)
        self.close()

    def close(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(Exception):
                self._kernel32.CloseHandle(self._handle)
            self._handle = None
