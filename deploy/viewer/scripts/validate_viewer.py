#!/usr/bin/env python3
"""Static least-privilege validation for an independently deployed viewer."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_COMPOSE = ROOT / "viewer" / "docker-compose.yml"
DEFAULT_DOCKERFILE = ROOT / "viewer" / "Dockerfile"


def check_compose(path: Path) -> list[str]:
    text = path.read_text()
    errors = []
    required = (
        (r"container_name:\s*\$\{VIEWER_CONTAINER_NAME:\?", "parameterized viewer container name"),
        (r'"\$\{VIEWER_BIND_ADDR:-127\.0\.0\.1\}:\$\{VIEWER_PORT:\?[^}]*\}:8000"', "loopback-default viewer bind and parameterized port"),
        (r"RECORDINGS_CONTROL_TOKEN_FILE:\s*/run/secrets/recordings-control-token", "control token runtime path"),
        (r"\$\{TENANT_CONTROL_TOKEN_FILE:\?[^}]*\}:/run/secrets/recordings-control-token:ro", "read-only control token file mount"),
        (r"user:\s*[\"']?10000:10000", "non-root UID/GID 10000 runtime"),
    )
    for pattern, label in required:
        if not re.search(pattern, text):
            errors.append(f"[compose] missing {label}")
    if re.search(r"\$\{TENANT_CREDS_DIR[^}]*\}:/creds(?::ro)?", text):
        errors.append("[compose] whole credential directory mount is forbidden; mount only recordings-control-token")
    if re.search(r"\$\{TENANT_CREDS_DIR[^}]*\}/recordings-control-token:/creds/recordings-control-token:ro", text):
        errors.append("[compose] control token file must use TENANT_CONTROL_TOKEN_FILE at /run/secrets/recordings-control-token")
    if re.search(r"(?m)^\s*ports\s*:\s*$\n\s*-\s*[\"']?(?:\d|100\.)", text):
        errors.append("[compose] fixed viewer bind/port is forbidden for tenant coexistence")
    return errors


def check_dockerfile(path: Path) -> list[str]:
    text = path.read_text()
    errors = []
    if not re.search(r"useradd\s+.*--uid\s+10000", text) or not re.search(r"(?m)^USER\s+recordings\s*$", text):
        errors.append("[Dockerfile] viewer must run as non-root UID/GID 10000")
    if not re.search(r"(?:mkdir\s+-p\s+/cache|install\s+-d).*?(?:chown\s+-R\s+10000:10000|--owner=10000:10000)", text, re.S):
        errors.append("[Dockerfile] cache/app runtime ownership for UID 10000 is missing")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose", type=Path, default=DEFAULT_COMPOSE)
    parser.add_argument("--dockerfile", type=Path, default=DEFAULT_DOCKERFILE)
    args = parser.parse_args()
    errors = check_compose(args.compose) + check_dockerfile(args.dockerfile)
    if errors:
        print("VALIDATION FAILED")
        print("\n".join(f"  - {error}" for error in errors))
        return 1
    print("VALIDATION PASSED")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
