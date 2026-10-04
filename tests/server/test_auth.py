"""Auth: token file, bearer check, Origin pinning."""

from phathom.config import settings
from phathom.server import auth


def test_token_created_once_and_verified():
    t1 = auth.get_or_create_token()
    assert len(t1) >= 32
    assert auth.get_or_create_token() == t1  # stable
    assert auth.verify_token(t1) is True
    assert auth.verify_token("wrong") is False
    assert auth.verify_token(None) is False
    assert auth.bearer_from_header("Bearer abc") == "abc"
    assert auth.bearer_from_header("bearer abc") == "abc"
    assert auth.bearer_from_header("Token abc") is None
    assert auth.bearer_from_header(None) is None


def test_token_file_mode_600():
    import os
    auth.get_or_create_token()
    assert oct(auth.TOKEN_PATH.stat().st_mode & 0o777) == "0o600"


def test_origin_rules(monkeypatch):
    monkeypatch.setattr(settings, "EXTENSION_ID", "")
    assert auth.origin_allowed(None) is True  # non-browser clients
    assert auth.origin_allowed("https://evil.com") is False
    assert auth.origin_allowed("http://127.0.0.1:8765") is False
    # first valid extension origin pins the id
    assert auth.origin_allowed("chrome-extension://abcdef") is True
    assert auth.origin_allowed("chrome-extension://abcdef") is True
    assert auth.origin_allowed("chrome-extension://other") is False


def test_configured_extension_id(monkeypatch):
    monkeypatch.setattr(settings, "EXTENSION_ID", "fixed-id")
    assert auth.origin_allowed("chrome-extension://fixed-id") is True
    assert auth.origin_allowed("chrome-extension://nope") is False
