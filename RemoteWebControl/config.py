"""Plugin configuration: defaults, parsing and validation. Pure Python (no Cura imports).

The values are stored as Cura preferences under the "remotewebcontrol/" prefix (cura.cfg).
"""

import ipaddress
import secrets
from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Tuple

PREFERENCE_PREFIX = "remotewebcontrol/"

DEFAULTS = {
    "bind_address": "0.0.0.0",
    "port": 8765,
    "token": "",
    "cors_origins": "",
    "max_upload_mb": 200,
    "job_retention_days": 7,
}


@dataclass(frozen=True)
class Config:
    bind_address: str
    port: int
    token: str
    cors_origins: Tuple[str, ...]
    max_upload_mb: int
    job_retention_days: int

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def is_origin_allowed(self, origin: str) -> bool:
        if not origin:
            return False
        return "*" in self.cors_origins or origin in self.cors_origins


def preference_key(name: str) -> str:
    return PREFERENCE_PREFIX + name


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def parse_origins(value: Any) -> Tuple[str, ...]:
    """Accepts a comma/whitespace separated string or a list. Trailing slashes are removed
    because browsers never send them in the Origin header."""
    if value is None:
        return ()
    items = value if isinstance(value, (list, tuple)) else str(value).replace(",", " ").split()
    result = []
    for item in items:
        origin = str(item).strip().rstrip("/")
        if origin and origin not in result:
            result.append(origin)
    return tuple(result)


def _parse_int(value: Any, minimum: int, maximum: int) -> int:
    number = int(str(value).strip())
    if not minimum <= number <= maximum:
        raise ValueError("{0} is outside [{1}, {2}]".format(number, minimum, maximum))
    return number


def _parse_address(value: Any) -> str:
    address = str(value).strip()
    ipaddress.ip_address(address)  # Raises ValueError for anything that is not a literal IP.
    return address


def parse_config(raw: Mapping[str, Any]) -> Tuple[Config, List[str]]:
    """Builds a Config from raw preference values.

    Invalid values fall back to their default. Returns the config and a list of warnings.
    """
    warnings = []  # type: List[str]

    def get(name: str, parser: Callable[[Any], Any]) -> Any:
        value = raw.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            value = DEFAULTS[name]  # Missing or left empty in cura.cfg: the default, silently.
        try:
            return parser(value)
        except (TypeError, ValueError) as e:
            warnings.append("Invalid value for {0} ({1!r}): {2}. Using default {3!r}.".format(
                preference_key(name), value, e, DEFAULTS[name]))
            return parser(DEFAULTS[name])

    config = Config(
        bind_address = get("bind_address", _parse_address),
        port = get("port", lambda v: _parse_int(v, 1, 65535)),
        token = str(raw.get("token") or "").strip(),
        cors_origins = parse_origins(raw.get("cors_origins", DEFAULTS["cors_origins"])),
        max_upload_mb = get("max_upload_mb", lambda v: _parse_int(v, 1, 4096)),
        job_retention_days = get("job_retention_days", lambda v: _parse_int(v, 0, 3650)),
    )
    return config, warnings
