"""Tests of the printer <-> scene conversion.

To avoid testing coords.py against itself, the Cura/Uranium steps are re-implemented here
literally from their source (5.13.0):
  - STLReader._loadWithNumpySTL: negate column 1, then swap columns 1 and 2.
  - MeshFileHandler.readerRead: centre the mesh on its bounding box centre.
  - StartSliceJob: world transform, then swap columns 1 and 2, then negate column 1.
"""

import itertools

import numpy
import pytest

from RemoteWebControl import coords
from RemoteWebControl.errors import ApiError


# ------------------------------------------------------------------ Cura re-implementations

def uranium_stl_reader(vertices):
    """U/plugins/FileHandlers/STLReader/STLReader.py:75-85"""
    v = numpy.array(vertices, dtype = numpy.float64)
    v[:, 1] *= -1
    v[:, [1, 2]] = v[:, [2, 1]]
    return v


def uranium_center(scene_vertices):
    """U/UM/Mesh/MeshFileHandler.py:42-47 + SceneNode.setCenterPosition"""
    center = (scene_vertices.min(axis = 0) + scene_vertices.max(axis = 0)) / 2
    return scene_vertices - center, center


def start_slice_job(world, mesh_vertices):
    """C/plugins/CuraEngineBackend/StartSliceJob.py:485-495"""
    rot_scale = world.T[0:3, 0:3]
    translate = world[:3, 3]
    verts = mesh_vertices.dot(rot_scale)
    verts += translate
    verts[:, [1, 2]] = verts[:, [2, 1]]
    verts[:, 1] *= -1
    return verts


# ------------------------------------------------------------------ test mesh

def l_shape():
    """Asymmetric "L": a 10x10x20 column standing on a 30x10x5 foot, offset from the origin
    so that the bounding box centre is not zero. Returns the corners of both boxes."""
    def box(lo, hi):
        return [list(c) for c in itertools.product(*zip(lo, hi))]
    points = box((0, 0, 0), (30, 10, 5)) + box((0, 0, 0), (10, 10, 20))
    return numpy.array(points, dtype = numpy.float64) + numpy.array([5.0, 7.0, 3.0])


def rotation(axis, degrees):
    """Right-handed rotation in printer coordinates, written out by hand."""
    c = round(numpy.cos(numpy.radians(degrees)))
    s = round(numpy.sin(numpy.radians(degrees)))
    m = numpy.identity(4)
    if axis == "x":
        m[1:3, 1:3] = [[c, -s], [s, c]]
    elif axis == "y":
        m[0, 0], m[0, 2], m[2, 0], m[2, 2] = c, s, -s, c
    else:
        m[0:2, 0:2] = [[c, -s], [s, c]]
    return m


ROTATIONS = [(axis, angle) for axis in "xyz" for angle in (90, -90)]


# ------------------------------------------------------------------ tests

@pytest.mark.parametrize("axis,angle", ROTATIONS)
def test_l_shape_rotation_bbox_in_scene(axis, angle):
    stl = l_shape()
    mesh, center = uranium_center(uranium_stl_reader(stl))
    job_matrix = rotation(axis, angle)

    world = coords.scene_matrix_from_printer(job_matrix, center)
    scene_positions = coords.apply(world, mesh)

    # Expected: rotate in printer space, then convert to the scene exactly like the STL reader.
    expected_scene = uranium_stl_reader(coords.apply(job_matrix, stl))
    numpy.testing.assert_allclose(scene_positions.min(axis = 0), expected_scene.min(axis = 0), atol = 1e-9)
    numpy.testing.assert_allclose(scene_positions.max(axis = 0), expected_scene.max(axis = 0), atol = 1e-9)

    # And what CuraEngine receives is exactly M . v in printer coordinates.
    numpy.testing.assert_allclose(start_slice_job(world, mesh), coords.apply(job_matrix, stl), atol = 1e-9)


# Bounding boxes worked out by hand for the L (x 5..35, y 7..17, z 3..23).
HAND_BBOXES = {
    ("x", 90): ([5, -23, 7], [35, -3, 17]),    # (x, y, z) -> (x, -z, y)
    ("x", -90): ([5, 3, -17], [35, 23, -7]),   # (x, y, z) -> (x, z, -y)
    ("y", 90): ([3, 7, -35], [23, 17, -5]),    # (x, y, z) -> (z, y, -x)
    ("y", -90): ([-23, 7, 5], [-3, 17, 35]),   # (x, y, z) -> (-z, y, x)
    ("z", 90): ([-17, 5, 3], [-7, 35, 23]),    # (x, y, z) -> (-y, x, z)
    ("z", -90): ([7, -35, 3], [17, -5, 23]),   # (x, y, z) -> (y, -x, z)
}


@pytest.mark.parametrize("axis,angle", ROTATIONS)
def test_l_shape_rotation_matches_hand_computed_bbox(axis, angle):
    stl = l_shape()
    mesh, center = uranium_center(uranium_stl_reader(stl))
    world = coords.scene_matrix_from_printer(rotation(axis, angle), center)
    engine = start_slice_job(world, mesh)
    lo, hi = HAND_BBOXES[(axis, angle)]
    assert coords.points_bbox(engine) == {"min": [float(v) for v in lo], "max": [float(v) for v in hi]}


def test_l_shape_is_not_mirrored():
    # A mirror would keep the bbox of a symmetric shape; check one specific vertex instead.
    stl = l_shape()
    mesh, center = uranium_center(uranium_stl_reader(stl))
    job_matrix = rotation("z", 90)
    world = coords.scene_matrix_from_printer(job_matrix, center)
    tip = numpy.array([[35.0, 7.0, 3.0]])  # End of the foot.
    tip_index = int(numpy.argmin(numpy.linalg.norm(stl - tip, axis = 1)))
    numpy.testing.assert_allclose(start_slice_job(world, mesh)[tip_index], [-7.0, 35.0, 3.0], atol = 1e-9)


def test_round_trip_with_arbitrary_world_transform():
    rng = numpy.random.default_rng(1)
    stl = l_shape()
    mesh, center = uranium_center(uranium_stl_reader(stl))
    for _ in range(20):
        q, _ = numpy.linalg.qr(rng.normal(size = (3, 3)))
        world = numpy.identity(4)
        world[:3, :3] = q * rng.uniform(0.5, 2.0)
        world[:3, 3] = rng.uniform(-100, 100, size = 3)

        job_matrix = coords.printer_matrix_from_scene(world, center)
        # The job matrix reproduces what the engine would get from this node...
        numpy.testing.assert_allclose(coords.apply(job_matrix, stl), start_slice_job(world, mesh), atol = 1e-9)
        # ...and converting back gives the same node transformation.
        numpy.testing.assert_allclose(coords.scene_matrix_from_printer(job_matrix, center), world, atol = 1e-9)


def test_point_and_box_conversion():
    printer = numpy.array([[1.0, 2.0, 3.0]])
    numpy.testing.assert_allclose(coords.printer_points_to_scene(printer), [[1.0, 3.0, -2.0]])
    numpy.testing.assert_allclose(coords.scene_points_to_printer(coords.printer_points_to_scene(printer)), printer)
    numpy.testing.assert_allclose(coords.printer_points_to_scene(printer), uranium_stl_reader(printer))
    # Scene box x -10..10, y 0..5 (height), z -20..30  ->  printer y = -z: -30..20, z = 0..5
    assert coords.scene_box_to_printer([-10, 0, -20], [10, 5, 30]) == {"min": [-10.0, -30.0, 0.0], "max": [10.0, 20.0, 5.0]}


def test_scene_plane_to_printer_flips_depth_axis():
    # Scene z grows towards the front of the printer; printer y grows towards the back.
    square = [[-10, -20], [10, -20], [10, 20], [-10, 20]]
    assert coords.scene_plane_to_printer_xy(square) == [[-10.0, 20.0], [10.0, 20.0], [10.0, -20.0], [-10.0, -20.0]]


def test_plane_round_trip_and_no_negative_zero():
    points = [[0, 0], [1.5, -2.25], [-3, 4]]
    assert coords.printer_xy_to_scene_plane(coords.scene_plane_to_printer_xy(points)) == [[0.0, 0.0], [1.5, -2.25], [-3.0, 4.0]]
    assert str(coords.scene_plane_to_printer_xy([[0, 0]])[0][1]) == "0.0"


def test_matrix_list_round_trip():
    m = rotation("x", 90) @ coords.translation([1, 2, 3])
    assert numpy.array_equal(coords.matrix_from_list(coords.matrix_to_list(m)), m)
    assert coords.matrix_to_list(numpy.identity(4)) == [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]


@pytest.mark.parametrize("values", [
    None, [1] * 15, "x" * 16,
    [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 1, 1],            # not affine
    [0] * 15 + [1],                                               # singular
    [float("nan")] + [0] * 14 + [1],                              # not finite
    [True, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],          # booleans are not numbers
    ["a"] + [0] * 15,
])
def test_invalid_matrices(values):
    with pytest.raises(ApiError) as info:
        coords.matrix_from_list(values)
    assert (info.value.status, info.value.code) == (400, "invalid_matrix")


def test_describe_linear_part():
    assert coords.describe_linear_part(rotation("y", 90)) == (True, False)
    assert coords.describe_linear_part(numpy.diag([2.0, 2.0, 2.0, 1.0])) == (False, False)
    assert coords.describe_linear_part(numpy.diag([-1.0, 1.0, 1.0, 1.0])) == (True, True)
