import pytest
from starlette.requests import Request

from viewer.app import main
from viewer import gen_magic_link


def _request_with_cookie(token: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/recordings",
        "headers": [(b"cookie", f"{main.COOKIE_NAME}={token}".encode())],
    })


def test_previous_magic_link_and_cookie_remain_valid(monkeypatch):
    monkeypatch.setattr(main, "SECRET", "permanent-secret")
    monkeypatch.setattr(main, "LEGACY_MAGIC_TOKENS", ("previous-link-token",))

    assert main.valid_magic_token("previous-link-token")
    assert main.authed(_request_with_cookie("previous-link-token"))


def test_unknown_magic_token_remains_invalid(monkeypatch):
    monkeypatch.setattr(main, "SECRET", "permanent-secret")
    monkeypatch.setattr(main, "LEGACY_MAGIC_TOKENS", ("previous-link-token",))

    assert not main.valid_magic_token("attacker-token")
    assert not main.authed(_request_with_cookie("attacker-token"))


def test_magic_link_defaults_to_https():
    assert gen_magic_link.build_login_url("viewer.example", "token") == (
        "https://viewer.example/login?t=token"
    )


def test_magic_link_allows_explicit_http_only_for_localhost():
    assert gen_magic_link.build_login_url("http://localhost:8000", "token") == (
        "http://localhost:8000/login?t=token"
    )
    with pytest.raises(ValueError, match="localhost"):
        gen_magic_link.build_login_url("http://viewer.example", "token")


def test_viewer_auth_cookie_is_secure_by_default(monkeypatch):
    monkeypatch.setattr(main, "SECRET", "secret")
    monkeypatch.delenv("VIEWER_DEVELOPMENT_INSECURE_COOKIE", raising=False)

    cookie = main.login(main.expected_token()).headers["set-cookie"]

    assert "Secure" in cookie


def test_viewer_auth_cookie_has_exact_development_opt_out(monkeypatch):
    monkeypatch.setattr(main, "SECRET", "secret")
    monkeypatch.setenv("VIEWER_DEVELOPMENT_INSECURE_COOKIE", "1")
    assert "Secure" not in main.login(main.expected_token()).headers["set-cookie"]

    monkeypatch.setenv("VIEWER_DEVELOPMENT_INSECURE_COOKIE", "true")
    assert "Secure" in main.login(main.expected_token()).headers["set-cookie"]