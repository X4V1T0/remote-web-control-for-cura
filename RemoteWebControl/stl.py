"""Minimal STL reader (binary and ASCII) for validation and preview. Pure Python + numpy.

Slicing never uses this: Cura loads the original file with its own reader. The result is in
the file's own coordinates, which are printer coordinates (Z up, mm).
"""

import re
import struct

import numpy

from .errors import ApiError

_BINARY_HEADER_SIZE = 84
_BINARY_RECORD = numpy.dtype([
    ("normal", "<f4", (3,)),
    ("vertices", "<f4", (3, 3)),
    ("attributes", "<u2"),
])
_FLOAT = rb"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_VERTEX_RE = re.compile(rb"vertex\s+(" + _FLOAT + rb")\s+(" + _FLOAT + rb")\s+(" + _FLOAT + rb")", re.IGNORECASE)


def read_stl(data: bytes) -> numpy.ndarray:
    """Parses an STL file. Returns float32 triangles with shape (n, 3, 3).

    Raises ApiError(422, "invalid_stl") if the data is not a usable STL.
    """
    if _looks_binary(data):
        triangles = _read_binary(data)
    else:
        triangles = _read_ascii(data)
    if len(triangles) == 0:
        raise ApiError(422, "invalid_stl", "The STL file contains no triangles.")
    if not numpy.isfinite(triangles).all():
        raise ApiError(422, "invalid_stl", "The STL file contains invalid (NaN or infinite) coordinates.")
    extent = triangles.reshape(-1, 3).max(axis = 0) - triangles.reshape(-1, 3).min(axis = 0)
    if (extent <= 0).sum() > 1:
        raise ApiError(422, "invalid_stl", "The STL model has no volume (all vertices on a line or a point).")
    return triangles


def _looks_binary(data: bytes) -> bool:
    if len(data) < _BINARY_HEADER_SIZE:
        return False
    count = struct.unpack_from("<I", data, 80)[0]
    # Some exporters write binary files whose header starts with "solid", so the size is what decides.
    return len(data) == _BINARY_HEADER_SIZE + count * _BINARY_RECORD.itemsize


def _read_binary(data: bytes) -> numpy.ndarray:
    count = struct.unpack_from("<I", data, 80)[0]
    records = numpy.frombuffer(data, dtype = _BINARY_RECORD, count = count, offset = _BINARY_HEADER_SIZE)
    return numpy.ascontiguousarray(records["vertices"], dtype = numpy.float32)


def _read_ascii(data: bytes) -> numpy.ndarray:
    head = data[:1024].lstrip().lower()
    if not head.startswith(b"solid"):
        raise ApiError(422, "invalid_stl", "The file is neither a binary STL nor an ASCII STL.")
    values = _VERTEX_RE.findall(data)
    if len(values) % 3 != 0:
        raise ApiError(422, "invalid_stl", "The ASCII STL has a facet without exactly 3 vertices.")
    vertices = numpy.array(values, dtype = numpy.float64).astype(numpy.float32)
    return vertices.reshape(-1, 3, 3)
