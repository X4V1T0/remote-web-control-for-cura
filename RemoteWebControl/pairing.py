"""Pairing a phone with a short PIN. Pure Python.

On iOS a web app added to the Home Screen has its own storage, separate from Safari, so a token
handed to Safari (e.g. through a QR code) does not reach it. The pairing page on the PC shows a
6-digit PIN; the phone exchanges it for the API token. The PIN expires and is invalidated after a
few wrong attempts, so it cannot be brute-forced over the LAN.
"""

import hmac
import secrets
import threading
import time
from typing import Callable, Dict, Optional

from .errors import ApiError

PIN_TTL_S = 600.0
MAX_ATTEMPTS = 10


class Pairing:
    def __init__(self, get_token: Callable[[], str], clock: Callable[[], float] = time.monotonic,
                 ttl: float = PIN_TTL_S, max_attempts: int = MAX_ATTEMPTS) -> None:
        self._get_token = get_token
        self._clock = clock
        self._ttl = ttl
        self._max_attempts = max_attempts
        self._lock = threading.Lock()
        self._pin = None  # type: Optional[str]
        self._expires = 0.0
        self._attempts = 0

    def start(self) -> Dict[str, object]:
        """A new PIN (the previous one stops working)."""
        with self._lock:
            self._pin = "{0:06d}".format(secrets.randbelow(1000000))
            self._expires = self._clock() + self._ttl
            self._attempts = 0
            return {"pin": self._pin, "expires_in": int(self._ttl)}

    def claim(self, pin: object) -> str:
        """Returns the API token for a valid PIN. A PIN can be used by several devices until it expires."""
        if not isinstance(pin, str) or not pin.strip().isdigit():
            raise ApiError(400, "invalid_pin", "The PIN must be 6 digits.")
        with self._lock:
            if self._pin is None:
                raise ApiError(403, "no_pairing", "Open \"Remote Web Control > Pair a phone\" in Cura to get a PIN.")
            if self._clock() > self._expires:
                self._pin = None
                raise ApiError(403, "pin_expired", "The PIN has expired; open the pairing page in Cura again.")
            if not hmac.compare_digest(pin.strip(), self._pin):
                self._attempts += 1
                if self._attempts >= self._max_attempts:
                    self._pin = None
                    raise ApiError(403, "pin_blocked", "Too many wrong PINs; open the pairing page in Cura again.")
                raise ApiError(403, "invalid_pin", "Wrong PIN.")
            return self._get_token()
