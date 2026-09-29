"""Test mesh helpers."""

import struct

import numpy


def box_triangles(lo=(0, 0, 0), hi=(10, 20, 30)):
    """Closed axis-aligned box, 12 triangles, outward facing."""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    v = numpy.array([
        [x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
        [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1],
    ], dtype = numpy.float32)
    faces = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (2, 3, 7), (2, 7, 6), (1, 2, 6), (1, 6, 5), (3, 0, 4), (3, 4, 7),
    ]
    return v[numpy.array(faces)]


def sphere_triangles(subdivisions = 60, radius = 20.0):
    """UV sphere with 2 * subdivisions^2 triangles (roughly)."""
    tris = []
    for i in range(subdivisions):
        t0, t1 = numpy.pi * i / subdivisions, numpy.pi * (i + 1) / subdivisions
        for j in range(subdivisions):
            p0, p1 = 2 * numpy.pi * j / subdivisions, 2 * numpy.pi * (j + 1) / subdivisions

            def point(t, p):
                return [radius * numpy.sin(t) * numpy.cos(p), radius * numpy.sin(t) * numpy.sin(p), radius * numpy.cos(t)]
            a, b, c, d = point(t0, p0), point(t1, p0), point(t1, p1), point(t0, p1)
            if i != 0:
                tris.append([a, b, d])
            if i != subdivisions - 1:
                tris.append([b, c, d])
    return numpy.array(tris, dtype = numpy.float32)


def binary_stl(triangles, header = b"binary header"):
    data = bytearray(header.ljust(80, b" "))
    data += struct.pack("<I", len(triangles))
    for tri in triangles:
        data += struct.pack("<3f", 0, 0, 0)
        data += numpy.asarray(tri, dtype = "<f4").tobytes()
        data += b"\x00\x00"
    return bytes(data)


def ascii_stl(triangles):
    lines = ["solid test"]
    for tri in triangles:
        lines.append("  facet normal 0 0 0")
        lines.append("    outer loop")
        for vx in tri:
            lines.append("      vertex {0:e} {1:e} {2:e}".format(*vx))
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid test")
    return "\n".join(lines).encode("ascii")
