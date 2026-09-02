#!/usr/bin/env python3
"""Print the magic-link login URL for the Jazzuu viewer.

Reads RECORDINGS_MAGIC_SECRET from the environment or from ./.env.
Usage: python3 gen_magic_link.py [host]
"""
import base64
import hashlib
import hmac
import ipaddress
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit


def load_secret() -> str:
    sec = os.environ.get("RECORDINGS_MAGIC_SECRET")
    if sec:
        return sec
    envf = Path(__file__).parent / ".env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            line = line.strip()
            if line.startswith("RECORDINGS_MAGIC_SECRET="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def build_login_url(host: str, token: str) -> str:
    base = host if "://" in host else f"https://{host}"
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("host must be an HTTP(S) origin")
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("host must be an origin without credentials, path, query, or fragment")
    if parsed.scheme == "http":
        try:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            local = parsed.hostname == "localhost"
        if not local:
            raise ValueError("plain HTTP is allowed only for localhost development")
    return f"{base.rstrip('/')}/login?t={token}"


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "localhost:62370"
    secret = load_secret()
    if not secret:
        print("ERROR: RECORDINGS_MAGIC_SECRET not set (env or .env)", file=sys.stderr)
        sys.exit(1)
    digest = hmac.new(secret.encode(), b"jazzuu-viewer", hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    try:
        print(build_login_url(host, token))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
