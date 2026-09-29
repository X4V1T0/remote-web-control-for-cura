"""Vertex clustering simplification for the preview mesh. Pure Python + numpy.

Only the preview uses this; slicing always uses the original file.
"""

import numpy

MAX_ITERATIONS = 30
GROWTH = 1.25


def cluster(triangles: numpy.ndarray, cell_size: float) -> numpy.ndarray:
    """Snaps vertices to a grid of cell_size, merges each cell into the mean of its vertices and
    drops the triangles that collapse. Duplicated triangles are removed (orientation of the first
    occurrence is kept). Returns float32 triangles (m, 3, 3)."""
    vertices = triangles.reshape(-1, 3).astype(numpy.float64)
    origin = vertices.min(axis = 0)
    cells = numpy.floor((vertices - origin) / cell_size).astype(numpy.int64)
    inverse, cell_count = _unique_rows(cells)[1:]

    counts = numpy.bincount(inverse, minlength = cell_count).astype(numpy.float64)
    representatives = numpy.zeros((cell_count, 3))
    for axis in range(3):
        representatives[:, axis] = numpy.bincount(inverse, weights = vertices[:, axis], minlength = cell_count) / counts

    faces = inverse.reshape(-1, 3)
    keep = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
    faces = faces[keep]
    if len(faces):
        first = _unique_rows(numpy.sort(faces, axis = 1))[0]
        faces = faces[numpy.sort(first)]
    return representatives[faces].astype(numpy.float32)


def _unique_rows(rows: numpy.ndarray):
    """Unique rows of an (n, 3) int array without numpy.unique(axis=0), which is very slow.

    Returns (index of the first occurrence of each unique row, inverse (n,), number of unique rows).
    """
    order = numpy.lexsort((rows[:, 2], rows[:, 1], rows[:, 0]))
    sorted_rows = rows[order]
    is_new = numpy.ones(len(rows), dtype = bool)
    is_new[1:] = numpy.any(sorted_rows[1:] != sorted_rows[:-1], axis = 1)
    group = numpy.cumsum(is_new) - 1
    inverse = numpy.empty(len(rows), dtype = numpy.int64)
    inverse[order] = group
    # lexsort is stable, so the first element of each group is its first occurrence.
    first = order[is_new]
    return first, inverse, int(group[-1]) + 1 if len(rows) else 0


def _surface_area(triangles: numpy.ndarray) -> float:
    t = triangles.astype(numpy.float64)
    return float(numpy.linalg.norm(numpy.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis = 1).sum() / 2)


def decimate(triangles: numpy.ndarray, max_triangles: int) -> numpy.ndarray:
    """Returns at most max_triangles triangles approximating the input (unchanged if already small)."""
    if len(triangles) <= max_triangles:
        return triangles
    vertices = triangles.reshape(-1, 3)
    diagonal = float(numpy.linalg.norm(vertices.max(axis = 0) - vertices.min(axis = 0)))
    area = _surface_area(triangles)
    # A regular grid over a surface of area A with cells of size s yields about 2*A/s^2 triangles.
    # 10% margin so that one pass is usually enough (each pass takes seconds on multi-million meshes).
    cell = 1.1 * numpy.sqrt(2 * area / max_triangles) if area > 0 else diagonal / 1000
    cell = max(cell, diagonal * 1e-6)
    for _ in range(MAX_ITERATIONS):
        result = cluster(triangles, cell)
        if len(result) <= max_triangles:
            return result
        cell *= GROWTH
    return cluster(triangles, diagonal)  # Degenerate fallback: a handful of triangles.
