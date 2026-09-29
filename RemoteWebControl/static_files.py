"""Serves the web app (RemoteWebControl/web) at "/". Pure Python.

Files are read from disk on every request, so the app can be edited without restarting Cura.
They hold no secrets and are served without a token; the API under /api still needs it.
"""

import base64
import hashlib
import os
import re
import urllib.parse
from typing import List, Optional

from .errors import ApiError
from .server import Response

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}

# The app only talks to its own origin; blob:/data: are used for downloads and the QR image.
# Inline import maps (needed by three.js) are allowed by their SHA-256, computed when serving.
CONTENT_SECURITY_POLICY = ("default-src 'self'; script-src 'self'{hashes}; img-src 'self' data: blob:; "
                           "style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; "
                           "base-uri 'none'; frame-ancestors 'none'")

_IMPORT_MAP_RE = re.compile(r'<script type="importmap">(.*?)</script>', re.DOTALL)


def inline_script_hashes(html: str) -> List[str]:
    """CSP source expressions for the inline import maps of a page."""
    return ["'sha256-{0}'".format(base64.b64encode(hashlib.sha256(m.encode("utf-8")).digest()).decode("ascii"))
            for m in _IMPORT_MAP_RE.findall(html)]


class StaticFiles:
    def __init__(self, root: str) -> None:
        self._root = os.path.realpath(root)

    def resolve(self, url_path: str) -> Optional[str]:
        """File for a URL path, or None. Never escapes the root directory."""
        path = urllib.parse.unquote(url_path)
        if "\x00" in path or "\\" in path:
            return None
        parts = [p for p in path.split("/") if p not in ("", ".")]
        if any(p == ".." or p.startswith(".") for p in parts):
            return None  # No parent directories and no hidden files.
        candidate = os.path.realpath(os.path.join(self._root, *parts))
        if candidate != self._root and not candidate.startswith(self._root + os.sep):
            return None
        if os.path.isdir(candidate):
            candidate = os.path.join(candidate, "index.html")
        if not os.path.isfile(candidate):
            return None
        if os.path.splitext(candidate)[1].lower() not in CONTENT_TYPES:
            return None
        return candidate

    def response(self, url_path: str) -> Response:
        path = self.resolve(url_path)
        if path is None:
            raise ApiError(404, "not_found", "No such page: {0}".format(url_path))
        extension = os.path.splitext(path)[1].lower()
        headers = {
            "Cache-Control": "no-cache",  # Revalidate: the app is edited in place.
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        }
        if extension == ".html":
            with open(path, encoding = "utf-8") as f:
                hashes = inline_script_hashes(f.read())
            headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY.format(hashes = "".join(" " + h for h in hashes))
            headers["X-Frame-Options"] = "DENY"
        return Response(200, content_type = CONTENT_TYPES[extension], headers = headers, file_path = path)
