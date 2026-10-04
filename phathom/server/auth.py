"""Token auth + Origin check between the extension and the server (EXTENSION_SPEC §5.6).

- On first run a random 32-byte token is created at ``data/server_token`` (mode 600).
- The user pastes it once into the extension (``phathom token`` prints it).
- Every REST call sends ``Authorization: Bearer <token>``; the WS sends ``?token=``.
- Any request with a wrong token, or with an ``Origin`` other than the pinned
  ``chrome-extension://<id>``, is rejected. The id comes from ``EXTENSION_ID``
  in .env, otherwise it is learned from the first valid connection and pinned
  to ``data/extension_id``.
- The server binds to 127.0.0.1 only (see app.run).
"""

from __future__ import annotations

import hmac
import logging
import secrets
from pathlib import Path

log = logging.getLogger("phathom.server.auth")

TOKEN_PATH = Path("data/server_token")
EXT_ID_PATH = Path("data/extension_id")


def get_or_create_token() -> str:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = TOKEN_PATH.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    token = secrets.token_hex(32)
    TOKEN_PATH.write_text(token + "\n")
    try:
        TOKEN_PATH.chmod(0o600)
    except OSError:
        pass
    log.info("generated new server token at %s", TOKEN_PATH)
    return token


def verify_token(provided: str | None) -> bool:
    if not provided:
        return False
    expected = get_or_create_token()
    return hmac.compare_digest(provided, expected)


def bearer_from_header(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def pinned_extension_id() -> str | None:
    """Configured id, else the pinned id learned from the first valid connection."""
    from phathom.config import settings
    if settings.EXTENSION_ID:
        return settings.EXTENSION_ID
    try:
        pinned = EXT_ID_PATH.read_text().strip()
        return pinned or None
    except OSError:
        return None


def pin_extension_id(ext_id: str) -> None:
    EXT_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXT_ID_PATH.write_text(ext_id.strip() + "\n")
    log.info("pinned extension id %s", ext_id)


def extension_id_from_origin(origin: str | None) -> str | None:
    if not origin:
        return None
    prefix = "chrome-extension://"
    if not origin.startswith(prefix):
        return None
    return origin[len(prefix):].rstrip("/")


def origin_allowed(origin: str | None) -> bool:
    """Missing Origin (curl, non-browser) is allowed; a present one must match."""
    if not origin:
        return True
    ext_id = extension_id_from_origin(origin)
    if ext_id is None:
        return False
    pinned = pinned_extension_id()
    if pinned is None:
        pin_extension_id(ext_id)
        return True
    return hmac.compare_digest(ext_id, pinned)
