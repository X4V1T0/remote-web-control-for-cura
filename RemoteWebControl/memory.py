"""Gives memory back to the operating system after a job. Pure Python (stdlib).

After loading and slicing a model, Python and glibc keep the freed memory for reuse, so Cura's
idle memory grows with every job (measured in Docker: from ~375 MB to ~460 MB after three
slices). A garbage collection followed by glibc's malloc_trim(0) returns the free pages.
Only on Linux with glibc; elsewhere it only runs the garbage collector.
"""

import ctypes
import gc
import sys
from typing import Optional

_malloc_trim = None  # type: Optional[object]
_resolved = False


def _find_malloc_trim() -> Optional[object]:
    global _malloc_trim, _resolved
    if not _resolved:
        _resolved = True
        if sys.platform.startswith("linux"):
            try:
                libc = ctypes.CDLL("libc.so.6")  # glibc; ctypes.util may not be in Cura's frozen build.
                _malloc_trim = libc.malloc_trim
                _malloc_trim.argtypes = [ctypes.c_size_t]
                _malloc_trim.restype = ctypes.c_int
            except (OSError, AttributeError):
                _malloc_trim = None  # Not glibc (e.g. musl).
    return _malloc_trim


def release_memory() -> bool:
    """Returns True if memory was handed back to the OS."""
    gc.collect()
    trim = _find_malloc_trim()
    if trim is None:
        return False
    return bool(trim(0))
