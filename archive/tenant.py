#!/usr/bin/env python3
"""Resolve one connector tenant's archive, endpoint, and credentials.

Explicit tenant mode requires ``PLAUD_MCP_TENANT_URLS_JSON``: a JSON object
mapping each allowed ``TENANT_ID`` to one unique HTTP(S) MCP endpoint. An
optional ``PLAUD_MCP_EXPECTED_URL`` asserts the selected value. A bare
``PLAUD_MCP_URL`` is never used for tenant routing.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ASR_MCP_URL = 'http://127.0.0.1:62362/mcp'


@dataclass
class Tenant:
    tenant_id: str
    archive_dir: str
    db_path: str
    audio_dir: str
    mcp_url: str
    caller_token_file: str | None = None
    static_token: str | None = None
    asr_url: str = ''
    asr_token_file: str = ''

    @property
    def is_owner(self) -> bool:
        """Compatibility signal for the no-TENANT_ID layout."""
        return self.caller_token_file is None

    @property
    def uses_legacy_static_token(self) -> bool:
        """Whether this is the no-TENANT_ID compatibility layout."""
        return self.caller_token_file is None

    def describe(self) -> str:
        mode = ('static-token' if self.uses_legacy_static_token
                else 'mcp-caller-token')
        return (f'tenant={self.tenant_id} mode={mode} '
                f'mcp={self.mcp_url} archive={self.archive_dir}')


def _require(name: str, tenant_id: str = '') -> str:
    value = os.environ.get(name, '').strip()
    if not value:
        who = f' for tenant {tenant_id}' if tenant_id else ''
        raise SystemExit(f'tenant: {name} is required in tenant mode{who}')
    return value


def _tenant_urls(env) -> dict[str, str]:
    raw = env.get('PLAUD_MCP_TENANT_URLS_JSON', '').strip()
    if not raw:
        raise SystemExit('tenant: PLAUD_MCP_TENANT_URLS_JSON is required in tenant mode')
    try:
        urls = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit('tenant: PLAUD_MCP_TENANT_URLS_JSON must be a valid JSON object') from exc
    if not isinstance(urls, dict) or not urls:
        raise SystemExit('tenant: PLAUD_MCP_TENANT_URLS_JSON must be a non-empty JSON object')

    clean: dict[str, str] = {}
    for tenant_id, endpoint in urls.items():
        if not isinstance(tenant_id, str) or not tenant_id.strip() or not isinstance(endpoint, str):
            raise SystemExit('tenant: endpoint mapping requires non-empty string tenant IDs and URLs')
        endpoint = endpoint.strip()
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in {'http', 'https'} or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment):
            raise SystemExit(f'tenant: endpoint for {tenant_id!r} must be one HTTP(S) URL without credentials or a fragment')
        clean[tenant_id.strip()] = endpoint
    if len(set(clean.values())) != len(clean):
        raise SystemExit('tenant: each MCP endpoint must belong to exactly one tenant')
    return clean


def resolve_mcp_url(tenant_id: str, env=None) -> str:
    """Resolve exactly one reviewed endpoint for ``tenant_id`` or fail closed."""
    env = os.environ if env is None else env
    urls = _tenant_urls(env)
    try:
        endpoint = urls[tenant_id]
    except KeyError as exc:
        raise SystemExit(f'tenant: TENANT_ID={tenant_id!r} is not present in PLAUD_MCP_TENANT_URLS_JSON') from exc
    expected = env.get('PLAUD_MCP_EXPECTED_URL', '').strip()
    if expected and expected != endpoint:
        raise SystemExit(
            f'tenant: PLAUD_MCP_EXPECTED_URL={expected} does not match configured endpoint for {tenant_id}')
    return endpoint


def load(explicit_id: str | None = None) -> Tenant:
    """Build tenant paths and credentials from the environment."""
    tenant_id = (explicit_id or os.environ.get('TENANT_ID', '')).strip()

    if not tenant_id:
        token_path = os.path.join(HERE, '.plaud_token')
        token = ''
        if os.path.exists(token_path):
            with open(token_path, encoding='utf-8') as token_file:
                token = token_file.read().strip()
        token = os.environ.get('PLAUD_MCP_TOKEN', token)
        return Tenant(
            tenant_id='default',
            archive_dir=HERE,
            db_path=os.path.join(HERE, 'archive.db'),
            audio_dir=os.path.join(HERE, 'audio'),
            mcp_url=os.environ.get('PLAUD_MCP_EXPECTED_URL', 'http://127.0.0.1/mcp'),
            static_token=token,
            asr_url=os.environ.get('ASR_MCP_URL', DEFAULT_ASR_MCP_URL),
            asr_token_file=os.path.join(HERE, '.asr_token'),
        )

    archive_dir = _require('TENANT_ARCHIVE_DIR', tenant_id)
    caller_token_file = _require('PLAUD_MCP_TOKEN_FILE', tenant_id)
    return Tenant(
        tenant_id=tenant_id,
        archive_dir=archive_dir,
        db_path=os.path.join(archive_dir, 'archive.db'),
        audio_dir=os.path.join(archive_dir, 'audio'),
        mcp_url=resolve_mcp_url(tenant_id),
        caller_token_file=caller_token_file,
        asr_url=os.environ.get('ASR_MCP_URL', DEFAULT_ASR_MCP_URL),
        asr_token_file=os.environ.get('ASR_TOKEN_FILE', ''),
    )
