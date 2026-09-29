import numpy
import pytest

from RemoteWebControl import decimate, mesh_format
from RemoteWebControl.errors import ApiError
from RemoteWebControl.stl import read_stl
from meshes import ascii_stl, binary_stl, box_triangles, sphere_triangles


def test_binary_and_ascii_give_the_same_triangles():
    tris = box_triangles()
    numpy.testing.assert_allclose(read_stl(binary_stl(tris)), tris)
    numpy.testing.assert_allclose(read_stl(ascii_stl(tris)), tris, rtol = 1e-6)


def test_binary_with_solid_header_is_read_as_binary():
    tris = box_triangles()
    numpy.testing.assert_allclose(read_stl(binary_stl(tris, header = b"solid exported by some CAD")), tris)


@pytest.mark.parametrize("data", [
    b"",
    b"hello world" * 20,
    b"solid empty\nendsolid empty\n",
    binary_stl(numpy.zeros((0, 3, 3))),
    binary_stl(numpy.array([[[0, 0, 0], [0, 0, 0], [0, 0, 0]]])),                   # a point
    binary_stl(numpy.array([[[0, 0, 0], [1, 0, 0], [float("nan"), 0, 0]]])),       # NaN
    b"solid x\n facet normal 0 0 1\n outer loop\n vertex 0 0 0\n vertex 1 0 0\n endloop\n endfacet\nendsolid",
])
def test_invalid_stl(data):
    with pytest.raises(ApiError) as info:
        read_stl(data)
    assert (info.value.status, info.value.code) == (422, "invalid_stl")


def test_mesh_format_round_trip():
    tris = box_triangles()
    payload = mesh_format.encode(tris, decimated = True)
    assert payload[:4] == b"CRM1"
    assert len(payload) == 12 + len(tris) * 36
    decoded, flags = mesh_format.decode(payload)
    assert flags == mesh_format.FLAG_DECIMATED
    numpy.testing.assert_array_equal(decoded, tris)
    assert mesh_format.decode(mesh_format.encode(tris, decimated = False))[1] == 0


def test_decimate_keeps_small_meshes_untouched():
    tris = box_triangles()
    assert decimate.decimate(tris, 100) is tris


def test_decimate_reduces_below_limit_and_keeps_shape():
    tris = sphere_triangles(120)  # ~28,700 triangles
    assert len(tris) > 20000
    result = decimate.decimate(tris, 2000)
    assert 200 < len(result) <= 2000
    # The simplified sphere keeps its size: all vertices stay close to the radius.
    radii = numpy.linalg.norm(result.reshape(-1, 3), axis = 1)
    assert radii.min() > 16 and radii.max() < 20.5
    lo, hi = result.reshape(-1, 3).min(axis = 0), result.reshape(-1, 3).max(axis = 0)
    numpy.testing.assert_allclose(lo, [-20, -20, -20], atol = 3)
    numpy.testing.assert_allclose(hi, [20, 20, 20], atol = 3)


def test_cluster_drops_degenerate_and_duplicate_triangles():
    tris = numpy.array([
        [[0, 0, 0], [10, 0, 0], [0, 10, 0]],
        [[0, 0, 0], [10, 0, 0], [0, 10, 0]],        # duplicate
        [[0, 0, 0], [0.01, 0, 0], [0, 10, 0]],     # collapses at cell size 1
    ], dtype = numpy.float32)
    result = decimate.cluster(tris, 1.0)
    assert len(result) == 1
