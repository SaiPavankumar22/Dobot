"""Small, dependency-free hardware and local-model suitability probe.

The probe is intentionally conservative.  It does not claim that a model can run merely because
the machine has enough total RAM; it supplies a safe profile for the setup wizard and leaves the
actual model manifest/runtime to a later install step.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class HardwareProfile:
    platform: str
    architecture: str
    cpu_threads: int
    memory_bytes: int
    free_disk_bytes: int
    recommended_mode: str
    local_model_class: str
    detail: str


def _memory_bytes() -> int:
    if sys.platform == "win32":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_uint32),
                ("memory_load", ctypes.c_uint32),
                ("total_phys", ctypes.c_uint64),
                ("avail_phys", ctypes.c_uint64),
                ("total_page", ctypes.c_uint64),
                ("avail_page", ctypes.c_uint64),
                ("total_virtual", ctypes.c_uint64),
                ("avail_virtual", ctypes.c_uint64),
                ("avail_extended", ctypes.c_uint64),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total_phys)
    if hasattr(os, "sysconf"):
        try:
            return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
        except (OSError, ValueError):
            pass
    return 0


def probe_hardware(*, root: str = ".") -> dict[str, object]:
    memory = _memory_bytes()
    threads = os.cpu_count() or 1
    memory_gib = memory / (1024**3) if memory else 0
    if memory and memory_gib < 8:
        mode, model_class, detail = (
            "cloud",
            "none",
            "This machine is below the safe memory budget for local model inference.",
        )
    elif memory and memory_gib < 12:
        mode, model_class, detail = (
            "hybrid",
            "small-quantized",
            "Use local tools and a small quantized CPU model only; cloud reasoning is recommended.",
        )
    elif memory and memory_gib < 24:
        mode, model_class, detail = (
            "hybrid",
            "medium-quantized",
            "A quantized local model may fit, but cloud reasoning remains the safer default.",
        )
    else:
        mode, model_class, detail = (
            "local-or-hybrid",
            "model-dependent",
            "Model choice still requires a manifest with an explicit RAM/VRAM budget.",
        )
    return asdict(
        HardwareProfile(
            platform=platform.system().lower(),
            architecture=platform.machine().lower(),
            cpu_threads=threads,
            memory_bytes=memory,
            free_disk_bytes=shutil.disk_usage(root).free,
            recommended_mode=mode,
            local_model_class=model_class,
            detail=detail,
        )
    )
