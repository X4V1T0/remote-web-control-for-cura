"""Binary preview mesh format "CRM1". Pure Python + numpy.

Little-endian:
    magic   4 bytes  b"CRM1"
    flags   uint32   bit0 = decimated
    count   uint32   number of triangles
    data    float32[count * 9]  (x, y, z) x 3 per triangle, printer coordinates of the
                                untransformed model (Z up, mm)
"""

import struct
from typing import Tuple

import numpy

MAGIC = b"CRM1"
FLAG_DECIMATED = 1
_HEADER = struct.Struct("<4sII")


def encode(triangles: numpy.ndarray, decimated: bool) -> bytes:
    data = numpy.ascontiguousarray(triangles, dtype = "<f4").reshape(-1, 9)
    header = _HEADER.pack(MAGIC, FLAG_DECIMATED if decimated else 0, len(data))
    return header + data.tobytes()


def decode(payload: bytes) -> Tuple[numpy.ndarray, int]:
    """Returns (triangles (n, 3, 3) float32, flags). Used by tests and tools."""
    magic, flags, count = _HEADER.unpack_from(payload, 0)
    if magic != MAGIC:
        raise ValueError("Not a CRM1 mesh")
    data = numpy.frombuffer(payload, dtype = "<f4", count = count * 9, offset = _HEADER.size)
    return data.reshape(count, 3, 3), flags
