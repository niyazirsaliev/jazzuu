#!/usr/bin/env python3
"""
Static validator for the per-tenant PLAUD connector stack.

Deterministic. No network, no Docker daemon, no third-party dependencies.
Exits 0 on success, non-zero listing every violation.

This validator enforces connector isolation without network or Docker access:

  Compose:
    - Credentials arrive as a MOUNTED FILE, never as an env var. A token in
      the environment leaks into `docker inspect`, logs and crash reports.
    - No literal secret material anywhere in the file.
    - No direct PLAUD OAuth configuration. A connector holds no account
      credentials: the tenant's own MCP service owns that lifecycle, and a
      second refresher would revoke its tokens on every rotation.
    - The credential mount is READ-ONLY. Nothing under /creds is written any
      more; rotation is the host replacing a file the container re-reads.
    - No ambient PLAUD_MCP_URL — a URL set here would apply to every tenant.
    - No host ports, no docker.sock, no privileged, no host networking,
      no fixed container_name (collides across tenants).

  Tenant env files (two or more):
    - Required keys present, no secret-looking values.
    - PLAUD_MCP_TENANT_URLS_JSON maps each allowed tenant to one unique endpoint.
    - PLAUD_MCP_EXPECTED_URL, when set, must match that mapping.
    - TENANT_ID, COMPOSE_PROJECT_NAME and selected endpoints differ across
      tenants.
    - TENANT_ARCHIVE_DIR / TENANT_CREDS_DIR / TENANT_CONTROL_TOKEN_FILE
      pairwise disjoint across tenants: two tenants must never share an
      archive, a credential namespace, or a control credential.
    - Each tenant's archive dir is disjoint from its own creds dir.

Usage:
  validate_connector.py
  validate_connector.py --compose <path> --tenant A.env --tenant B.env
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

HERE = Path(__file__).resolve().parent
CONNECTOR_DIR = HERE.parent
REPO_ROOT = CONNECTOR_DIR.parent.parent
DEFAULT_COMPOSE = REPO_ROOT / "archive" / "docker-compose.yml"
DEFAULT_TENANTS = [
    CONNECTOR_DIR / "samples" / "connector-tenant-alpha.env.example",
    CONNECTOR_DIR / "samples" / "connector-tenant-beta.env.example",
]

FORBIDDEN_COMPOSE_PATTERNS: List[Tuple[str, str]] = [
    (r"(?m)^\s*ports\s*:", "host port mapping (`ports:`) is forbidden"),
    (r"/var/run/docker\.sock", "Docker socket mount is forbidden"),
    (r"(?m)^\s*network_mode\s*:\s*[\"']?host[\"']?\s*$",
     "network_mode: host is forbidden"),
    (r"(?m)^\s*privileged\s*:\s*true\b", "privileged: true is forbidden"),
    (r"(?m)^\s*container_name\s*:",
     "fixed container_name is forbidden (collides across tenants)"),
    (r"\bfunnel\b", "Tailscale Funnel is forbidden"),
    # The whole point of the file-based credential design: a token passed as an
    # env var shows up in `docker inspect` and in every crash dump.
    (r"(?m)^\s*(PLAUD_ACCESS_TOKEN|PLAUD_REFRESH_TOKEN|PLAUD_CLIENT_SECRET|"
     r"PLAUD_API_TOKEN|PLAUD_PASSWORD|PLAUD_USERNAME|ASR_MCP_TOKEN|"
     r"PLAUD_MCP_TOKEN)\s*:",
     "credentials must be mounted as a file, never injected as an env var"),
    # Connectors hold no PLAUD account credentials. The tenant's
    # own MCP service owns the OAuth lifecycle; a connector that refreshes too
    # would revoke that service's tokens on every rotation.
    (r"(?m)^\s*(PLAUD_TOKENS_FILE|PLAUD_CLIENT_ID|PLAUD_REFRESH_URL)\s*:",
     "the connector must not carry direct PLAUD OAuth configuration — that "
     "belongs to the tenant's MCP service"),
    # A bare PLAUD_MCP_URL would apply to every tenant started from this file.
    (r"(?m)^\s*PLAUD_MCP_URL\s*:",
     "ambient PLAUD_MCP_URL is forbidden; tenant.py selects from "
     "PLAUD_MCP_TENANT_URLS_JSON"),
]

REQUIRED_COMPOSE_PATTERNS: List[Tuple[str, str]] = [
    (r"\$\{DIARIZATION_MODELS_DIR:-/srv/jazzuu/models/diarization\}:/models:ro",
     "diarization models must use the stable shared host root mounted read-only"),
    (r"RECORDINGS_CONTROL_SOCKET\s*:\s*/run/recordings/control\.sock",
     "control socket path must be explicit and private"),
    (r"RECORDINGS_CONTROL_TOKEN_FILE\s*:\s*/run/secrets/recordings-control-token",
     "control credential must be read from a mounted file, never an env token"),
    (r"(?m)^\s*-\s*\$\{TENANT_CONTROL_TOKEN_FILE:\?TENANT_CONTROL_TOKEN_FILE must be set\}:/run/secrets/recordings-control-token:ro\s*$",
     "control token must be a required read-only exact file mount at /run/secrets/recordings-control-token"),
    (r"PLAUD_MCP_TOKEN_FILE\s*:\s*/creds/plaud-mcp-token",
     "connector must read its PLAUD_MCP_TOKEN_FILE caller token from a "
     "mounted file"),
    (r"PLAUD_MCP_TENANT_URLS_JSON\s*:\s*\$\{PLAUD_MCP_TENANT_URLS_JSON:\?",
     "the reviewed tenant endpoint mapping must be required"),
    (r"PLAUD_MCP_EXPECTED_URL\s*:\s*\$\{PLAUD_MCP_EXPECTED_URL:-\}",
     "the optional endpoint assertion must be passed through"),
    (r"ASR_TOKEN_FILE\s*:\s*\$\{ASR_TOKEN_FILE:\?",
     "ASR_TOKEN_FILE must be explicitly required and come from a mounted file path"),
    (r"ASR_AUDIO_PATH_PREFIX\s*:\s*\$\{ASR_AUDIO_PATH_PREFIX:-\}",
     "ASR_AUDIO_PATH_PREFIX must be passed through so each tenant's shared-ASR audio mount reaches the connector"),
    (r"ASR_SEGMENT_THRESHOLD_SECONDS\s*:\s*\$\{ASR_SEGMENT_THRESHOLD_SECONDS:-600\}",
     "ASR segmentation threshold must default to 600 seconds"),
    (r"ASR_SEGMENT_SECONDS\s*:\s*\$\{ASR_SEGMENT_SECONDS:-600\}",
     "ASR windows must default to 600 seconds"),
    (r"PIPELINE_ASR_ENGINE\s*:\s*\$\{PIPELINE_ASR_ENGINE:-mixed\}",
     "ASR must use the utterance-level mixed router"),
    (r"TENANT_ID\s*:\s*\$\{TENANT_ID:\?",
     "TENANT_ID must be explicitly required (no silent default tenant)"),
]

# Anything that looks like real secret material committed by accident.
SECRET_VALUE_PATTERNS: List[Tuple[str, str]] = [
    (r"\bclient_[0-9a-f]{8}-[0-9a-f]{4}-", "PLAUD client id literal"),
    (r"\beyJ[A-Za-z0-9_-]{20,}", "JWT literal"),
    (r"\b[A-Za-z0-9_-]{40,}\b", "long opaque token-like literal"),
]

REQUIRED_ENV_KEYS = (
    "COMPOSE_PROJECT_NAME",
    "TENANT_ID",
    "TENANT_ARCHIVE_DIR",
    "TENANT_CREDS_DIR",
    "TENANT_CONTROL_TOKEN_FILE",
    "TENANT_CONTROL_DIR",
    "PLAUD_MCP_TENANT_URLS_JSON",
)

FORBIDDEN_ENV_KEYS = (
    "PLAUD_ACCESS_TOKEN",
    "PLAUD_REFRESH_TOKEN",
    "PLAUD_CLIENT_SECRET",
    "PLAUD_PASSWORD",
    "PLAUD_USERNAME",
    "PLAUD_API_TOKEN",
    "PLAUD_MCP_TOKEN",
    "ASR_MCP_TOKEN",
    # Direct PLAUD OAuth belongs to the tenant's MCP service, not here.
    "PLAUD_TOKENS_FILE",
    "PLAUD_CLIENT_ID",
    "PLAUD_REFRESH_URL",
    # A bare URL bypasses tenant-scoped selection.
    "PLAUD_MCP_URL",
    # Execution policy is pinned by the connector compose contract.  Letting a
    # tenant env override any of these changes stack-wide execution policy.
    "ASR_SEGMENT_SECONDS",
    "ASR_SEGMENT_THRESHOLD_SECONDS",
    "ASR_MAX_SEGMENT_SECONDS",
)


def _tenant_module():
    """The connector's own tenant module, loaded for its pinned URL table.

    Imported rather than copied: a second table here would drift from the one
    the connector actually obeys, and the drift would be silent — the whole
    class of bug this validator exists to catch.
    """
    path = REPO_ROOT / "archive" / "tenant.py"
    spec = importlib.util.spec_from_file_location("tenant_for_validator", path)
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves annotations through sys.modules[cls.__module__], so
    # the module has to be registered before it executes.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def parse_tenant_urls(raw: str) -> Dict[str, str]:
    """Use the runtime parser so deployment validation cannot drift."""
    try:
        return _tenant_module()._tenant_urls({"PLAUD_MCP_TENANT_URLS_JSON": raw})
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc


TENANT_PATH_KEYS = ("TENANT_ARCHIVE_DIR", "TENANT_CREDS_DIR", "TENANT_CONTROL_DIR")
# This is deliberately separate from TENANT_PATH_KEYS: an exact token file may
# live under this tenant's legacy creds directory, but it must never be shared
# with or nested beneath another tenant's namespace.
TENANT_CONTROL_TOKEN_KEY = "TENANT_CONTROL_TOKEN_FILE"
# Two tenants sharing any of these are pointed at one identity: the same
# project, the same archive, or the same PLAUD account behind one MCP service.
TENANT_ID_KEYS = ("COMPOSE_PROJECT_NAME", "TENANT_ID", "PLAUD_MCP_EXPECTED_URL")


def strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        in_str, quote, cut, i = False, "", len(line), 0
        while i < len(line):
            c = line[i]
            if in_str:
                if c == quote and line[i - 1] != "\\":
                    in_str = False
            elif c in ("'", '"'):
                in_str, quote = True, c
            elif c == "#":
                cut = i
                break
            i += 1
        out.append(line[:cut].rstrip())
    return "\n".join(out)


def norm(p: str) -> str:
    return os.path.normpath(p).rstrip("/") or "/"


def paths_overlap(a: str, b: str) -> bool:
    """True if a == b or either contains the other."""
    a, b = norm(a), norm(b)
    if a == b:
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")


def check_compose(path: Path) -> List[str]:
    errors: List[str] = []
    text = strip_comments(path.read_text())

    for pat, msg in FORBIDDEN_COMPOSE_PATTERNS:
        m = re.search(pat, text)
        if m:
            errors.append(f"[compose] {msg} (matched: {m.group(0)!r})")

    for pat, msg in REQUIRED_COMPOSE_PATTERNS:
        if not re.search(pat, text):
            errors.append(f"[compose] required-but-missing: {msg}")

    for pat, label in SECRET_VALUE_PATTERNS:
        for m in re.finditer(pat, text):
            literal = m.group(0)
            # Image digests and well-known non-secret words are fine.
            if literal.startswith("sha256") or "docker" in literal.lower():
                continue
            errors.append(
                f"[compose] possible {label} committed: {literal[:24]}...")

    control_token_dir_mount = re.search(
        r"(?m)^\s*-\s*\$\{TENANT_CONTROL_TOKEN_FILE[^}]*\}:/run/secrets(?::ro)?\s*$", text)
    if control_token_dir_mount:
        errors.append(
            "[compose] control-token parent directory mount is forbidden "
            f"({control_token_dir_mount.group(0).strip()!r}); mount only the "
            "exact recordings-control-token file")

    destinations: Dict[str, str] = {}
    for line in text.splitlines():
        mount = re.match(r"^\s*-\s*\$\{[^}]+\}:(/[^:\s]+)(?::[^\s]+)?\s*$", line)
        if not mount:
            continue
        destination = mount.group(1)
        if destination in destinations:
            errors.append(
                f"[compose] duplicate mount destination {destination!r}; "
                "a later bind silently overrides the audited credential source")
        else:
            destinations[destination] = line.strip()

    # The credential mount must be READ-ONLY. It used to be writable so the
    # connector could persist a rotated PLAUD refresh token; the tenant's MCP
    # service owns that lifecycle now, so nothing in the container writes here
    # and write access is pure blast radius on the one directory that holds
    # this tenant's tokens.
    mounts = list(re.finditer(
        r"(?m)^\s*-\s*\$\{TENANT_CREDS_DIR[^}]*\}:(\S+)", text))
    if not mounts:
        errors.append(
            "[compose] required-but-missing: the tenant credential directory "
            "must be mounted at /creds (it carries the PLAUD and ASR caller "
            "tokens)")
    for m in mounts:
        if not m.group(1).endswith(":ro"):
            errors.append(
                "[compose] credential mount is writable "
                f"({m.group(0).strip()!r}) — the connector writes nothing "
                "under /creds; mount the token directory :ro")
    control_mount = re.search(
        r"(?m)^\s*-\s*\$\{TENANT_CONTROL_DIR[^}]*\}:/run/recordings(?:\s|$)", text)
    if not control_mount:
        errors.append("[compose] required-but-missing: tenant-private control directory must mount at /run/recordings")
    elif control_mount.group(0).rstrip().endswith(":ro"):
        errors.append("[compose] control socket mount is read-only "
                      f"({control_mount.group(0).strip()!r}) — connector must "
                      "create /run/recordings/control.sock, so remove :ro only "
                      "from TENANT_CONTROL_DIR (never from credentials)")
    model_mount = re.search(
        r"(?m)^\s*-\s*\$\{DIARIZATION_MODELS_DIR[^}]*\}:/models(?::ro)?\s*$",
        text)
    if not model_mount or not model_mount.group(0).rstrip().endswith(":ro"):
        errors.append("[compose] diarization model root must be mounted read-only at /models")
    return errors


def parse_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
    return out


def check_control_token_file(path: Path) -> List[str]:
    """Reject a mounted control credential that the runtime will fail closed.

    Bind mounts preserve the host file mode. A compose file can prove that the
    mount is exact and read-only, but only deployment preflight can prove the
    source itself is private enough for archive/control.py to accept.
    """
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return [f"[control token] unavailable: {path}"]
    if not path.is_file():
        return [f"[control token] must be a regular file: {path}"]
    if mode != 0o600:
        return [
            f"[control token] mode must be 0600, got {mode:04o}: {path}"
        ]
    return []


def check_declared_target(name: str, env: Dict[str, str]) -> List[str]:
    """The selected endpoint must come from the tenant mapping."""
    tenant_id = env.get("TENANT_ID", "")
    raw = env.get("PLAUD_MCP_TENANT_URLS_JSON", "")
    if not tenant_id or not raw:
        return []
    try:
        mapping = parse_tenant_urls(raw)
    except ValueError as exc:
        return [f"[{name}] {exc}"]
    if tenant_id not in mapping:
        return [f"[{name}] TENANT_ID={tenant_id!r} is not present in PLAUD_MCP_TENANT_URLS_JSON"]
    declared = env.get("PLAUD_MCP_EXPECTED_URL", "")
    if declared and declared != mapping[tenant_id]:
        return [f"[{name}] PLAUD_MCP_EXPECTED_URL={declared} does not match configured endpoint for {tenant_id}"]
    return []


def check_env_file(path: Path, env: Dict[str, str]) -> List[str]:
    errors: List[str] = []
    name = path.name

    for key in REQUIRED_ENV_KEYS:
        if not env.get(key):
            errors.append(f"[{name}] missing required key: {key}")

    for key in FORBIDDEN_ENV_KEYS:
        if key in env:
            errors.append(
                f"[{name}] forbidden key present: {key} — credentials belong "
                f"in a mounted file, not an env var")

    engine = env.get("PIPELINE_ASR_ENGINE")
    if engine is not None and engine not in {"mixed", "large-v3"}:
        errors.append(
            f"[{name}] PIPELINE_ASR_ENGINE must be mixed or large-v3")

    public_identifier_keys = {"VOICEPRINT_MODEL_VERSION"}
    for pat, label in SECRET_VALUE_PATTERNS:
        for key, val in env.items():
            if (key not in public_identifier_keys and val
                    and re.search(pat, val) and not val.startswith("/")):
                errors.append(
                    f"[{name}] {key} looks like a committed {label}")

    errors += check_declared_target(name, env)

    control_token = env.get(TENANT_CONTROL_TOKEN_KEY, "")
    if control_token:
        if not os.path.isabs(control_token):
            errors.append(f"[{name}] {TENANT_CONTROL_TOKEN_KEY} must be an absolute exact-file path")
        elif Path(control_token).name != "recordings-control-token":
            errors.append(
                f"[{name}] {TENANT_CONTROL_TOKEN_KEY} must name the exact "
                "recordings-control-token file, not its parent directory")
        elif Path(control_token).exists():
            errors += check_control_token_file(Path(control_token))

    paths = [(key, env.get(key, "")) for key in TENANT_PATH_KEYS]
    for i, (left_key, left) in enumerate(paths):
        for right_key, right in paths[i + 1:]:
            if left and right and paths_overlap(left, right):
                errors.append(
                    f"[{name}] intra-tenant overlap: {left_key}={left} vs "
                    f"{right_key}={right} — archive, credentials and control "
                    "socket namespace must be distinct")
    return errors


def check_cross_tenant(envs: List[Tuple[Path, Dict[str, str]]]) -> List[str]:
    errors: List[str] = []
    mappings = {env.get("PLAUD_MCP_TENANT_URLS_JSON", "") for _, env in envs}
    if len(mappings) > 1:
        errors.append("[tenants] PLAUD_MCP_TENANT_URLS_JSON must be identical across connector deployments")
    for i in range(len(envs)):
        for j in range(i + 1, len(envs)):
            (pa, ea), (pb, eb) = envs[i], envs[j]
            label = f"{pa.name} vs {pb.name}"

            for key in TENANT_ID_KEYS:
                va, vb = ea.get(key, ""), eb.get(key, "")
                if va and vb and va == vb:
                    errors.append(
                        f"[{label}] {key} must differ across tenants "
                        f"(both {va!r})")

            isolation_keys = (*TENANT_PATH_KEYS, TENANT_CONTROL_TOKEN_KEY)
            for ka in isolation_keys:
                for kb in isolation_keys:
                    va, vb = ea.get(ka, ""), eb.get(kb, "")
                    if va and vb and paths_overlap(va, vb):
                        errors.append(
                            f"[{label}] {ka}={va} overlaps other tenant's "
                            f"{kb}={vb} — tenant archive, credentials and "
                            "control namespaces must all be disjoint")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compose", type=Path, default=DEFAULT_COMPOSE)
    ap.add_argument("--tenant", type=Path, action="append")
    args = ap.parse_args()

    compose = args.compose
    tenants = args.tenant or DEFAULT_TENANTS

    if not compose.is_file():
        print(f"error: compose file not found: {compose}", file=sys.stderr)
        return 2
    if len(tenants) < 2:
        print("error: at least two tenant env files are required to prove "
              "isolation", file=sys.stderr)
        return 2

    errors = check_compose(compose)
    loaded: List[Tuple[Path, Dict[str, str]]] = []
    for t in tenants:
        if not t.is_file():
            print(f"error: tenant env not found: {t}", file=sys.stderr)
            return 2
        env = parse_env(t)
        loaded.append((t, env))
        errors += check_env_file(t, env)
    errors += check_cross_tenant(loaded)

    if errors:
        print("VALIDATION FAILED")
        for e in errors:
            print(f"  - {e}")
        return 1

    print("VALIDATION PASSED")
    print(f"  compose: {compose}")
    for t, _ in loaded:
        print(f"  tenant:  {t.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
