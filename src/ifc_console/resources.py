"""Process and system memory, best effort and dependency free.

The browser panel shows this beside its own heap so a person can see when a
run is pushing the machine, and the agent panel uses it to decide when to
release caches. Every reader here returns ``None`` rather than raising: a
missing number must never take the status route down with it.
"""

from __future__ import annotations

import os
import sys
from typing import Any


def _read_proc_kib(path: str, keys: tuple[str, ...]) -> dict[str, int]:
    found: dict[str, int] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                name, _, rest = line.partition(":")
                if name in keys:
                    parts = rest.split()
                    if parts and parts[0].isdigit():
                        found[name] = int(parts[0]) * 1024
    except OSError:
        return found
    return found


def _windows_process() -> tuple[int | None, int | None]:
    import ctypes
    from ctypes import wintypes

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # The pseudo handle is a full pointer; the default int return truncates it.
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    handle = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return None, None
    return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize)


def _windows_system() -> tuple[int | None, int | None]:
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None, None
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def _posix_process() -> tuple[int | None, int | None]:
    if sys.platform.startswith("linux"):
        found = _read_proc_kib("/proc/self/status", ("VmRSS", "VmHWM"))
        return found.get("VmRSS"), found.get("VmHWM")
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
    except (ImportError, OSError, ValueError):
        return None, None
    # macOS reports bytes, the BSDs and Linux report kibibytes.
    scale = 1 if sys.platform == "darwin" else 1024
    peak = int(usage.ru_maxrss) * scale
    return None, peak


def _posix_system() -> tuple[int | None, int | None]:
    if sys.platform.startswith("linux"):
        found = _read_proc_kib("/proc/meminfo", ("MemTotal", "MemAvailable"))
        return found.get("MemTotal"), found.get("MemAvailable")
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None, None
    if pages <= 0 or page_size <= 0:
        return None, None
    return int(pages) * int(page_size), None


def process_memory() -> dict[str, Any]:
    """Resident size of this process plus what the machine has left."""
    rss: int | None = None
    peak: int | None = None
    total: int | None = None
    available: int | None = None
    try:
        if sys.platform == "win32":
            rss, peak = _windows_process()
            total, available = _windows_system()
        else:
            rss, peak = _posix_process()
            total, available = _posix_system()
    except Exception:  # noqa: BLE001 - a platform quirk is not a status failure
        pass
    return {
        "rss_bytes": rss,
        "peak_rss_bytes": peak,
        "total_bytes": total,
        "available_bytes": available,
    }


__all__ = ["process_memory"]
