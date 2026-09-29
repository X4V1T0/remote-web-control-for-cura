"""Conversion between printer coordinates and Uranium scene coordinates. Pure Python + numpy.

This is the ONLY place where the axis convention is translated (see docs/DESIGN.md, section 9).

Printer coordinates (used everywhere in the API): Z up, millimetres, origin at the centre of
the build plate (Cura's internal convention, independent of machine_center_is_zero).

Uranium scene coordinates: Y up, origin at the centre of the build plate, and
    scene (x, y, z) = printer (x, z, -y)
The STL reader applies exactly this mapping and StartSliceJob applies its inverse when
sending vertices to CuraEngine.

Matrices are 4x4, applied to column vectors (p' = M . p), translation in the last column.
On the wire they are 16 floats in row-major order. This matches UM.Math.Matrix.getData().

Job matrices map the vertices of the ORIGINAL STL file (printer coordinates) to their final
position on the build plate (printer coordinates). In the scene, Cura stores the mesh centred
on its bounding box centre `c` (MeshData.getCenterPosition(), scene coordinates) and the node
has a world transformation W, so:

    scene position = W . T(-c) . A . v        (A = printer -> scene axis change)
    job matrix     M = A^-1 . W . T(-c) . A
    node matrix    W = A . M . A^-1 . T(c)

2D build plate polygons (machine_disallowed_areas, convex hulls) live in the scene
(x, z) plane, i.e. printer (x, -y).
"""

import math
from typing import Any, Dict, List, Sequence, Tuple

import numpy

from .errors import ApiError

# Printer -> scene axis change: (x, y, z) -> (x, z, -y).
PRINTER_TO_SCENE = numpy.array([
    [1.0, 0.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
    [0.0, -1.0, 0.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
])
SCENE_TO_PRINTER = PRINTER_TO_SCENE.T.copy()  # Pure rotation: the inverse is the transpose.

IDENTITY = numpy.identity(4)


def _neg(value: float) -> float:
    # Avoid emitting -0.0 in JSON.
    return -value if value != 0 else 0.0


# ------------------------------------------------------------------ 2D plane

def scene_plane_to_printer_xy(points: Sequence[Sequence[float]]) -> List[List[float]]:
    """Converts a 2D polygon from the scene (x, z) plane to printer (x, y)."""
    return [[float(x), _neg(float(z))] for x, z in points]


def printer_xy_to_scene_plane(points: Sequence[Sequence[float]]) -> List[List[float]]:
    """Converts a 2D polygon from printer (x, y) to the scene (x, z) plane."""
    return [[float(x), _neg(float(y))] for x, y in points]


# ------------------------------------------------------------------ 3D points and boxes

def printer_points_to_scene(points: numpy.ndarray) -> numpy.ndarray:
    """(n, 3) printer coordinates -> (n, 3) scene coordinates."""
    points = numpy.asarray(points, dtype = numpy.float64)
    return numpy.stack([points[:, 0], points[:, 2], -points[:, 1]], axis = 1)


def scene_points_to_printer(points: numpy.ndarray) -> numpy.ndarray:
    """(n, 3) scene coordinates -> (n, 3) printer coordinates."""
    points = numpy.asarray(points, dtype = numpy.float64)
    return numpy.stack([points[:, 0], -points[:, 2], points[:, 1]], axis = 1)


def scene_box_to_printer(scene_min: Sequence[float], scene_max: Sequence[float]) -> Dict[str, List[float]]:
    """Axis aligned box in scene coordinates -> {"min": [x, y, z], "max": [x, y, z]} in printer coordinates."""
    corners = scene_points_to_printer(numpy.array([scene_min, scene_max], dtype = numpy.float64))
    return {
        "min": [_clean(v) for v in corners.min(axis = 0)],
        "max": [_clean(v) for v in corners.max(axis = 0)],
    }


def points_bbox(points: numpy.ndarray) -> Dict[str, List[float]]:
    points = numpy.asarray(points, dtype = numpy.float64).reshape(-1, 3)
    return {"min": [_clean(v) for v in points.min(axis = 0)], "max": [_clean(v) for v in points.max(axis = 0)]}


def _clean(value: float, decimals: int = 4) -> float:
    rounded = round(float(value), decimals)
    return 0.0 if rounded == 0 else rounded


# ------------------------------------------------------------------ 4x4 matrices

def translation(offset: Sequence[float]) -> numpy.ndarray:
    matrix = numpy.identity(4)
    matrix[:3, 3] = offset
    return matrix


def apply(matrix: numpy.ndarray, points: numpy.ndarray) -> numpy.ndarray:
    """Applies a 4x4 affine matrix to (n, 3) points."""
    points = numpy.asarray(points, dtype = numpy.float64).reshape(-1, 3)
    return points @ matrix[:3, :3].T + matrix[:3, 3]


def printer_matrix_from_scene(world: numpy.ndarray, center: Sequence[float]) -> numpy.ndarray:
    """Job matrix M from the node's world transformation W and the mesh centre c (scene)."""
    return SCENE_TO_PRINTER @ numpy.asarray(world, dtype = numpy.float64) @ translation(-numpy.asarray(center, dtype = numpy.float64)) @ PRINTER_TO_SCENE


def scene_matrix_from_printer(matrix: numpy.ndarray, center: Sequence[float]) -> numpy.ndarray:
    """Node world transformation W from a job matrix M and the mesh centre c (scene)."""
    return PRINTER_TO_SCENE @ numpy.asarray(matrix, dtype = numpy.float64) @ SCENE_TO_PRINTER @ translation(numpy.asarray(center, dtype = numpy.float64))


def matrix_to_list(matrix: numpy.ndarray, decimals: int = 9) -> List[float]:
    return [_clean(v, decimals) for v in numpy.asarray(matrix, dtype = numpy.float64).reshape(16)]


def matrix_from_list(values: Any) -> numpy.ndarray:
    """Validates a 16-float row-major affine matrix coming from a client."""
    if not isinstance(values, (list, tuple)) or len(values) != 16:
        raise ApiError(400, "invalid_matrix", "matrix must be a list of 16 numbers (4x4, row-major).")
    try:
        numbers = [float(v) for v in values]
    except (TypeError, ValueError):
        raise ApiError(400, "invalid_matrix", "matrix must only contain numbers.")
    if isinstance(values[0], bool) or any(isinstance(v, bool) for v in values) or not all(math.isfinite(v) for v in numbers):
        raise ApiError(400, "invalid_matrix", "matrix must only contain finite numbers.")
    matrix = numpy.array(numbers, dtype = numpy.float64).reshape(4, 4)
    if not numpy.allclose(matrix[3], [0.0, 0.0, 0.0, 1.0], atol = 1e-9):
        raise ApiError(400, "invalid_matrix", "The last row of the matrix must be [0, 0, 0, 1] (affine transform).")
    if abs(numpy.linalg.det(matrix[:3, :3])) < 1e-9:
        raise ApiError(400, "invalid_matrix", "The matrix is singular (it would flatten the model).")
    return matrix


def describe_linear_part(matrix: numpy.ndarray) -> Tuple[bool, bool]:
    """Returns (is_rigid, is_mirrored) for the 3x3 part of an affine matrix."""
    linear = numpy.asarray(matrix, dtype = numpy.float64)[:3, :3]
    is_rigid = bool(numpy.allclose(linear.T @ linear, numpy.identity(3), atol = 1e-6))
    is_mirrored = bool(numpy.linalg.det(linear) < 0)
    return is_rigid, is_mirrored
