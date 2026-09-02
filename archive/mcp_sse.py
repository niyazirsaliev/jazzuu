"""Small shared parser for JSON-RPC responses delivered over SSE.

Transport/auth policy remains with each caller; this module only implements the
SSE framing rule shared by PLAUD and the local MCP clients.
"""
from __future__ import annotations

import json


def sse_events(raw: str):
    """Yield event payloads, joining repeated ``data:`` fields with newlines."""
    data = []
    for line in (raw or '').splitlines():
        line = line.rstrip('\r')
        if not line:
            if data:
                yield '\n'.join(data)
            data = []
            continue
        if line.startswith(':'):
            continue
        field, _sep, value = line.partition(':')
        if field == 'data':
            data.append(value[1:] if value.startswith(' ') else value)
    if data:
        yield '\n'.join(data)


def sse_json_objects(raw: str):
    """Yield object payloads only; comments and malformed frames are ignored."""
    for event in sse_events(raw):
        try:
            value = json.loads(event)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            yield value


def last_sse_json(raw: str):
    """Last complete JSON object from an SSE response, or ``None``."""
    result = None
    for result in sse_json_objects(raw):
        pass
    return result
