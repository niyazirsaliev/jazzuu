#!/usr/bin/env python3
"""plaud_mcp_auth.py — caller authentication against a tenant's PLAUD MCP.

A tenant connector holds no PLAUD account credentials. The tenant's
own self-hosted PLAUD MCP service owns the OAuth tokens and refreshes them; the
connector is just a CALLER of that service and proves who it is with a bearer
token read from a mounted file.

The token is read per request rather than cached at import. An operator can
then replace the file under a running container — rotation, re-provisioning,
fixing a bad paste — and the next call picks it up without a restart.
"""
from __future__ import annotations

import json
import os
import re

# The env var that points at the mounted token file. Named in error messages so
# an operator does not have to go read the compose file to find the knob.
TOKEN_FILE_VAR = 'PLAUD_MCP_TOKEN_FILE'


class PlaudMcpError(RuntimeError):
    """Base for the failures that need telling apart at a call site."""


class PlaudAuthConfigError(PlaudMcpError):
    """This connector's caller token is missing, empty or unreadable.

    Local misconfiguration: nothing was sent to the service, and no amount of
    retrying will change the outcome until a human provisions the file.
    """


class PlaudCallerAuthError(PlaudMcpError):
    """The tenant's MCP service rejected THIS connector's caller token.

    Deliberately not a subclass of PlaudAuthConfigError: a token that exists
    but is refused is a different repair (reissue it on the service) from a
    token that was never mounted, and a retry fixes neither.
    """


class PlaudServiceAccountNotSeededError(PlaudMcpError):
    """The MCP service accepted us but holds no usable PLAUD credentials.

    Distinct from both caller-auth failures: our side is entirely correct, and
    the repair is on the service (complete the PLAUD login / seed the account).
    Distinct from a transient too — retrying forever just hides it.
    """


class PlaudMcpServiceError(PlaudMcpError):
    """The service or the protocol failed, and it is none of the above.

    An unknown tool, a malformed reply, a missing response, an upstream PLAUD
    outage the service reports as a tool error: every one of these means the
    call returned NO DATA. It is a sibling of the credential failures, never a
    parent or a child of them, so `except` order cannot mis-report it — and it
    is not a network error either, because a service that answered 200 with an
    error is up and disagreeing with us, not unreachable.

    It exists because the alternative was worse than an unclear message: an
    unclassified error fell through to callers that read it as an empty list or
    an empty transcript, so an outage was indistinguishable from an account
    with nothing in it.
    """


# Machine-readable values a service may put in a structured field. Checked
# before any prose: a field is unambiguous and prose is a guess.
_CALLER_AUTH_STATES = frozenset({
    'invalid_token', 'invalid_bearer_token', 'token_invalid', 'token_revoked',
    'revoked_token', 'token_expired', 'expired_token',
    'caller_unauthorized', 'caller_unauthorised',
    'caller_not_authorized', 'caller_not_authorised',
})
_NOT_SEEDED_STATES = frozenset({
    'pending_seed', 'not_seeded', 'unseeded', 'seed_pending',
    'account_not_authorized', 'account_not_authorised',
    'account_pending_seed',
})
# Structured keys worth reading. A service reporting `{"state": "pending_seed"}`
# is telling us plainly; the rest of this module is fallback guesswork.
_STATE_KEYS = ('state', 'status', 'reason', 'error', 'code', 'error_code',
               'error_type', 'auth_state')

# Prose markers, matched only when a structured field said nothing. Each is
# anchored on the noun that decides WHOSE credential is at fault, because the
# unqualified phrase does not: "not authorized" alone could equally be our
# caller token or the service's own PLAUD account, and guessing it meant the
# seed state sent operators to re-run a login that was never the problem.
_CALLER_AUTH_PATTERNS = (
    r'invalid[_ ]token',
    r'\btoken\b[^.]{0,24}\b(invalid|revoked|expired)\b',
    r'\b(invalid|revoked|expired)\b[^.]{0,24}\btoken\b',
    r'\bcallers?\b[^.]{0,24}\bnot author(?:ized|ised)\b',
)
# These qualifiers identify the MCP service's upstream PLAUD credential, not
# the caller bearer presented to the MCP transport. They suppress ambiguous
# token prose, but never an explicit `caller` or `bearer` marker.
_SERVICE_TOKEN_PATTERNS = (
    r'\b(?:central|refresh|account|plaud)\b[^.]{0,24}\btoken\b',
    r'\btoken store\b',
)
_EXPLICIT_CALLER_PATTERNS = (
    r'\b(?:caller|bearer)\b[^.]{0,32}\b(?:token|invalid|revoked|expired|not author(?:ized|ised))\b',
    r'\btoken\b[^.]{0,24}\b(?:caller|bearer)\b',
)
_NOT_SEEDED_PATTERNS = (
    r'pending[_ ]seed',
    r'not[_ ]seeded',
    r'not\s+(?:been\s+)?seeded',
    r'\btoken store\b[^.]{0,40}\bempty\b',
    r'\badmin bootstrap\b',
    r'\baccount\b[^.]{0,24}\bnot author(?:ized|ised)\b',
)


def _error_facts(payload):
    """What an MCP reply says about a failure, flattened.

    Returns (failed, text, states, label): whether the reply is a failure at
    all, its free text lowercased for marker matching, the structured state
    values it declared, and a short description for an operator-facing message.
    """
    if not isinstance(payload, dict):
        return False, '', [], ''

    parts, states, label = [], [], ''
    error = payload.get('error')
    if isinstance(error, dict):
        message = str(error.get('message') or '').strip()
        code = error.get('code')
        parts.append(message)
        data = error.get('data')
        if isinstance(data, dict):
            parts.append(json.dumps(data))
            for key in _STATE_KEYS:
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    states.append(value.strip().lower())
        elif isinstance(data, list):
            parts.append(json.dumps(data))
        elif data:
            parts.append(str(data))
        label = f'JSON-RPC error (code {code}): {message or "no message"}'
    elif error:
        parts.append(str(error))
        label = f'JSON-RPC error: {error}'

    result = payload.get('result')
    if isinstance(result, dict):
        texts = [str(item.get('text') or '')
                 for item in (result.get('content') or [])
                 if isinstance(item, dict)]

        # plaud-mcp 3.4.4 deliberately reports an empty central token store as
        # a successful MCP tool result (`isError: false`) whose domain status is
        # `not_configured`. It is still a failed readiness check: there can be
        # no recordings until the service is seeded. Require both the status
        # and token/bootstrap-specific evidence so an unrelated optional
        # feature marked not_configured is not mistaken for an auth problem.
        documents = []
        structured = result.get('structuredContent')
        if isinstance(structured, dict):
            documents.append(structured)
        for text_item in texts:
            try:
                parsed = json.loads(text_item)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                documents.append(parsed)
        for document in documents:
            status = str(document.get('status') or '').strip().lower()
            evidence = json.dumps(document).lower()
            if status == 'not_configured' and (
                    re.search(r'\btoken store\b[^.]{0,40}\bempty\b', evidence)
                    or re.search(r'\bcentral token\b[^.]{0,50}\bnot (?:been )?seeded\b', evidence)
                    or 'admin bootstrap required' in evidence):
                parts.append(evidence)
                states.append('pending_seed')
                label = ('tool readiness status not_configured: central PLAUD '
                         'token store is empty')
                break

        # A tool call may report failure as a SUCCESSFUL JSON-RPC call carrying
        # an MCP error result, so it never reaches the `error` branch above and
        # every plain `if error` check misses it entirely.
        if result.get('isError'):
            parts.extend(texts)
            joined = ' '.join(t for t in texts if t).strip()
            label = f'tool error result: {joined or "no detail"}'

    if not label:
        return False, '', [], ''
    return True, ' '.join(p for p in parts if p).lower(), states, label


def _matches(text: str, patterns) -> bool:
    return any(re.search(p, text) for p in patterns)


def _is_caller_auth_text(text: str) -> bool:
    """Whether prose specifically blames the connector's MCP credential.

    Explicit caller/bearer wording wins. Otherwise the familiar token markers
    are accepted only when the sentence does not qualify the token as PLAUD's
    central/refresh/account credential.
    """
    if _matches(text, _EXPLICIT_CALLER_PATTERNS):
        return True
    if _matches(text, _SERVICE_TOKEN_PATTERNS):
        return False
    return _matches(text, _CALLER_AUTH_PATTERNS)


def raise_for_jsonrpc_error(payload, tenant_id: str, url: str) -> None:
    """Classify any failure an MCP reply carries, and raise it.

    Nothing that is a failure returns from here. The rule that matters is that
    a caller can never mistake an error for data: before this classified every
    error, an unknown-tool reply and an unseeded service both came back as an
    ordinary value and were read as "this account has no files".

    Precedence, most specific first:
      1. a structured seed state — the service account owns the repair;
      2. a structured caller/bearer state — this connector owns the repair;
      3. seed-specific prose, before ambiguous token wording;
      4. caller/bearer-specific prose;
      5. everything else — a generic service/protocol failure.

    A successful reply, including a legitimately EMPTY one (an account with no
    recordings, a recording with no note), raises nothing.
    """
    failed, text, states, label = _error_facts(payload)
    if not failed:
        return

    # A service that explicitly declares pending_seed/not_seeded owns the
    # repair. Check that before any sibling state so an accompanying upstream
    # `invalid_grant` cannot override the more specific seed diagnosis.
    if any(state in _NOT_SEEDED_STATES for state in states):
        raise _not_seeded_error(tenant_id, url, label)
    if any(state in _CALLER_AUTH_STATES for state in states):
        raise _caller_auth_error(tenant_id, url, label)
    # Seed evidence also precedes ambiguous token prose. "central token has not
    # been seeded" contains the word token, but it emphatically does not mean
    # the connector's bearer was rejected.
    if _matches(text, _NOT_SEEDED_PATTERNS):
        raise _not_seeded_error(tenant_id, url, label)
    if _is_caller_auth_text(text):
        raise _caller_auth_error(tenant_id, url, label)

    raise PlaudMcpServiceError(
        f'tenant {tenant_id}: the PLAUD MCP service at {url} returned a '
        f'{label}. This is the service or the protocol, not this connector\'s '
        f'credentials; the call returned no data and must not be read as an '
        f'empty result.')


def _caller_auth_error(tenant_id: str, url: str, label: str):
    return PlaudCallerAuthError(
        f'tenant {tenant_id}: the PLAUD MCP service at {url} rejected this '
        f'connector\'s caller token in a {label}. The token exists but is '
        f'wrong, revoked or expired: reissue it on that service and replace '
        f'the file {TOKEN_FILE_VAR} points at.')


def _not_seeded_error(tenant_id: str, url: str, label: str):
    return PlaudServiceAccountNotSeededError(
        f'tenant {tenant_id}: the PLAUD MCP service at {url} accepted this '
        f'connector but reports its own PLAUD account credentials are not '
        f'seeded ({label}). Nothing on the connector side can fix this: '
        f'complete the PLAUD login on that service.')


def read_caller_token(path: str, tenant_id: str) -> str:
    """The caller bearer token for this tenant, read fresh from disk.

    Never quotes file contents into the error: the one thing this file holds is
    the secret, and these messages end up in logs and healthcheck output.
    """
    try:
        with open(path, encoding='utf-8') as handle:
            token = handle.read().strip()
    except FileNotFoundError:
        raise PlaudAuthConfigError(
            f'tenant {tenant_id}: no caller token at {path} — mount the token '
            f'issued by this tenant\'s PLAUD MCP service there and point '
            f'{TOKEN_FILE_VAR} at it') from None
    except OSError as exc:
        raise PlaudAuthConfigError(
            f'tenant {tenant_id}: caller token at {path} is unreadable '
            f'({exc.strerror}); it must be readable by the connector uid') \
            from None
    if not token:
        raise PlaudAuthConfigError(
            f'tenant {tenant_id}: caller token file {path} is empty — the '
            f'file exists but holds no token, so this connector cannot prove '
            f'who it is to its PLAUD MCP service')
    return token


def raise_for_http_error(exc, tenant_id: str, url: str) -> None:
    """Re-raise an HTTPError as a caller-auth failure when that is what it is.

    Only 401 and 403 qualify. Everything else — 5xx, a proxy hiccup, a
    connection reset — is transient by default and must stay transient, or the
    loop would give up on an outage and an operator would go looking for a
    credential problem that does not exist.
    """
    if getattr(exc, 'code', None) not in (401, 403):
        raise exc
    raise PlaudCallerAuthError(
        f'tenant {tenant_id}: the PLAUD MCP service at {url} rejected this '
        f'connector\'s caller token (HTTP {exc.code}). The token itself is '
        f'wrong or revoked, not merely missing: reissue it on that service '
        f'and replace the file {TOKEN_FILE_VAR} points at.') from None
