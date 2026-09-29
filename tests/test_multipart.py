import pytest

from RemoteWebControl.errors import ApiError
from RemoteWebControl.multipart import parse_form_data

BOUNDARY = "----RemoteWebControlBoundary7MA4YWxkTrZu0gW"
CONTENT_TYPE = "multipart/form-data; boundary=" + BOUNDARY


def build(fields):
    """fields: list of (name, value_bytes, filename_or_None, extra_disposition)."""
    body = b""
    for name, value, filename, disposition in fields:
        body += b"--" + BOUNDARY.encode() + b"\r\n"
        header = 'Content-Disposition: form-data; name="{0}"'.format(name)
        if filename is not None:
            header += '; filename="{0}"'.format(filename)
        header += disposition
        body += header.encode("utf-8") + b"\r\n"
        if filename is not None:
            body += b"Content-Type: application/octet-stream\r\n"
        body += b"\r\n" + value + b"\r\n"
    return body + b"--" + BOUNDARY.encode() + b"--\r\n"


def test_fields_and_binary_file():
    data = bytes(range(256)) * 10 + b"\r\n--not-the-boundary\r\n"
    parts = parse_form_data(CONTENT_TYPE, build([
        ("file", data, "pieza.stl", ""),
        ("printer_id", "Creality Ender-3 Pro #2".encode(), None, ""),
        ("profile", b'{"quality": "standard"}', None, ""),
    ]))
    assert parts["file"].data == data
    assert parts["file"].filename == "pieza.stl"
    assert parts["printer_id"].text() == "Creality Ender-3 Pro #2"
    assert parts["profile"].filename is None


def test_rfc2231_filename():
    parts = parse_form_data(CONTENT_TYPE, build([("file", b"x", "fallback.stl", "; filename*=UTF-8''pi%C3%B1a%20verde.stl")]))
    assert parts["file"].filename == "piña verde.stl"


def test_quoted_boundary_and_empty_value():
    parts = parse_form_data('multipart/form-data; boundary="' + BOUNDARY + '"', build([("profile", b"", None, "")]))
    assert parts["profile"].data == b""


@pytest.mark.parametrize("content_type,body,status", [
    ("application/json", b"{}", 415),
    ("multipart/form-data", b"", 400),
    (CONTENT_TYPE, b"garbage", 400),
    (CONTENT_TYPE, b"--" + BOUNDARY.encode() + b"\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nno end", 400),
])
def test_malformed(content_type, body, status):
    with pytest.raises(ApiError) as info:
        parse_form_data(content_type, body)
    assert info.value.status == status
