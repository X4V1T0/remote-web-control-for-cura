"""multipart/form-data parser. Pure Python.

Written on top of bytes.find() instead of the email package because uploads can be hundreds
of MB and the email parser makes several copies of the body.
"""

from dataclasses import dataclass
from email.message import Message
from email.utils import collapse_rfc2231_value
from typing import Dict, Optional

from .errors import ApiError


@dataclass
class Part:
    name: str
    filename: Optional[str]
    content_type: Optional[str]
    data: bytes

    def text(self) -> str:
        try:
            return self.data.decode("utf-8")
        except UnicodeDecodeError:
            raise ApiError(400, "invalid_form", "Field '{0}' is not valid UTF-8 text.".format(self.name))


def _parse_header_params(value: str) -> Message:
    message = Message()
    message["Content-Type"] = value
    return message


def _param(message: Message, name: str) -> Optional[str]:
    """RFC 2231 values (filename*=UTF-8''...) come back as tuples and, per RFC 6266, win over
    the plain parameter when both are present."""
    plain = None
    for key, value in message.get_params(header = "content-disposition") or []:
        if key.lower() != name:
            continue
        if isinstance(value, tuple):
            return collapse_rfc2231_value(value)
        if plain is None:
            plain = collapse_rfc2231_value(value)
    return plain


def parse_form_data(content_type: str, body: bytes) -> Dict[str, Part]:
    """Parses a multipart/form-data body. Returns the parts by field name (the last one wins)."""
    header = _parse_header_params(content_type or "")
    if header.get_content_type() != "multipart/form-data":
        raise ApiError(415, "unsupported_media_type", "Expected a multipart/form-data request.")
    boundary = header.get_param("boundary")
    if not boundary or not isinstance(boundary, str):
        raise ApiError(400, "invalid_form", "multipart/form-data without boundary.")

    delimiter = b"--" + boundary.encode("latin-1")
    parts = {}  # type: Dict[str, Part]
    position = body.find(delimiter)
    if position < 0:
        raise ApiError(400, "invalid_form", "Malformed multipart body (boundary not found).")
    position += len(delimiter)

    while True:
        if body.startswith(b"--", position):
            break  # Closing delimiter.
        if not body.startswith(b"\r\n", position):
            raise ApiError(400, "invalid_form", "Malformed multipart body.")
        position += 2
        headers_end = body.find(b"\r\n\r\n", position)
        if headers_end < 0:
            raise ApiError(400, "invalid_form", "Malformed multipart body (part headers).")
        next_delimiter = body.find(b"\r\n" + delimiter, headers_end + 4)
        if next_delimiter < 0:
            raise ApiError(400, "invalid_form", "Malformed multipart body (unterminated part).")

        part_headers = Message()
        for line in body[position:headers_end].decode("utf-8", "replace").split("\r\n"):
            name, sep, value = line.partition(":")
            if sep:
                part_headers[name.strip()] = value.strip()
        disposition = part_headers.get("Content-Disposition", "")
        disposition_message = Message()
        disposition_message["Content-Disposition"] = disposition
        field_name = _param(disposition_message, "name")
        filename = _param(disposition_message, "filename")
        if field_name:
            parts[str(field_name)] = Part(
                name = str(field_name),
                filename = str(filename) if filename is not None else None,
                content_type = part_headers.get("Content-Type"),
                data = body[headers_end + 4:next_delimiter],
            )
        position = next_delimiter + 2 + len(delimiter)
    return parts
