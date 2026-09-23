"""Dashboard authentication: a per-run session token plus single-use login codes.

The dashboard is bound to 127.0.0.1, but loopback is shared by every account
and every process on the Mac, so API calls also need a token.

* The sampler generates a fresh random session token at each start and writes
  it to a 0600 file. Only processes running as you can read it.
* To open the dashboard, the CLI mints a short-lived, single-use login code
  (HMAC-signed with the token) and opens http://127.0.0.1:PORT/#login=CODE.
  The page swaps the code for the token via POST /api/session and keeps it in
  sessionStorage.
* Why not put the token in the URL? The URL passes through the argv of
  `open`, and argv is readable by every local user via the setuid /bin/ps.
  A code that is used within seconds and dies after 2 minutes leaks nothing
  useful. The fragment (#...) is never sent to the server or logged.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path

CODE_TTL_S = 120


def new_token() -> str:
    return secrets.token_urlsafe(32)


def write_token(path: Path, token: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token)
    os.chmod(path, 0o600)


def read_token(path: Path) -> str | None:
    try:
        token = path.read_text().strip()
    except OSError:
        return None
    return token or None


def _sign(token: str, payload: str) -> str:
    return hmac.new(token.encode(), payload.encode(), hashlib.sha256).hexdigest()


def mint_code(token: str, now: float | None = None) -> str:
    expires = int((time.time() if now is None else now) + CODE_TTL_S)
    payload = f"{expires}.{secrets.token_urlsafe(12)}"
    return f"{payload}.{_sign(token, payload)}"


class CodeVerifier:
    """Accepts each valid, unexpired login code exactly once."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._used: dict[str, int] = {}  # nonce -> expiry
        self._lock = threading.Lock()

    def redeem(self, code: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        parts = code.split(".")
        if len(parts) != 3 or not parts[0].isdigit() or len(code) > 200:
            return False
        expires, nonce, mac = int(parts[0]), parts[1], parts[2]
        if not hmac.compare_digest(mac, _sign(self._token, f"{parts[0]}.{nonce}")):
            return False
        if not (now <= expires <= now + CODE_TTL_S + 5):
            return False
        with self._lock:
            self._used = {n: e for n, e in self._used.items() if e >= now}
            if nonce in self._used:
                return False
            self._used[nonce] = expires
        return True

    def check_token(self, presented: str) -> bool:
        return hmac.compare_digest(presented.encode(), self._token.encode())
