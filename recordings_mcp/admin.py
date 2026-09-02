"""Private, atomic grant administration for the owner Recordings MCP.

The CLI is intentionally not an HTTP surface. It is run through authenticated
host/container administration. Credentials are generated here, never accepted
on argv, and are either printed in the create/rotate response exactly once or
written to a new mode-0600 handoff file.
"""
from __future__ import annotations
import argparse
import json
import os
import secrets
import sys
from recordings_mcp.grants import GrantError, GrantRegistry


def _credential_parser(subs, command):
    parser = subs.add_parser(command, allow_abbrev=False)
    parser.add_argument("--caller-id", required=True)
    parser.add_argument(
        "--token-out",
        help="write the one-time credential to a new mode-0600 file instead of stdout",
    )
    return parser


def _write_once(path, credential):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(credential + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise GrantError("credential output file already exists") from None
    except OSError as exc:
        raise GrantError("credential output file is unavailable") from exc


def _provision(registry, args):
    credential = secrets.token_urlsafe(32)
    wrote = False
    try:
        if args.token_out:
            _write_once(args.token_out, credential)
            wrote = True
        if args.command == "create":
            result = registry.create(
                caller_id=args.caller_id, token=credential, scopes=args.scope,
                recordings=args.recording, expires_at=args.expires_at,
            )
        else:
            result = registry.rotate(caller_id=args.caller_id, token=credential)
    except Exception:
        if wrote:
            try:
                os.unlink(args.token_out)
            except OSError:
                pass
        raise
    if not args.token_out:
        result["token"] = credential
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(prog="recordings-mcp-admin", allow_abbrev=False)
    parser.add_argument("--state", required=True, help="writable grant/audit SQLite path")
    subs = parser.add_subparsers(dest="command", required=True)
    create = _credential_parser(subs, "create")
    create.add_argument("--scope", action="append", default=[])
    create.add_argument("--recording", action="append", default=None)
    create.add_argument("--expires-at")
    subs.add_parser("list")
    revoke = subs.add_parser("revoke")
    revoke.add_argument("--caller-id", required=True)
    _credential_parser(subs, "rotate")
    args = parser.parse_args(argv)
    try:
        registry = GrantRegistry(args.state)
        if args.command in {"create", "rotate"}:
            result = _provision(registry, args)
        elif args.command == "list":
            result = registry.list()
        else:
            result = registry.revoke(caller_id=args.caller_id)
    except GrantError as exc:
        print(f"recordings-mcp-admin: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
