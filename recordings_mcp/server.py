#!/usr/bin/env python3
"""server.py — recordings-mcp: JSON-RPC (MCP) over HTTP for one tenant's archive.

Run it:  RECORDINGS_MCP_TENANT=owner RECORDINGS_MCP_ARCHIVE_DIR=/archive \
         RECORDINGS_MCP_GRANT_STATE=/state/recordings-mcp-state.db \
         python3 -m recordings_mcp.server

Endpoints:
  POST /mcp      JSON-RPC 2.0. Requires `Authorization: Bearer <caller token>`.
  GET  /healthz  liveness. Public, numbers and names of the service only.
  GET  /readyz   readiness. Public, counts only — never content, never paths.

Deliberately built on the standard library. This service reads a SQLite file
and answers JSON; a framework would add a dependency tree to the one container
in this project that must be easy to reason about line by line.

Two rules shape the request path:

  * a credential never travels in a URL. A request carrying anything
    credential-shaped in its query string is refused before it is even looked
    at, and the log line is built from the path alone, never from the request
    line, so a token cannot reach the log by accident;
  * a tool argument can never change scope. Arguments are matched against a
    per-tool whitelist, so `{"tenant": "owner"}` is a rejected request rather
    than an ignored key — a caller that thinks it re-scoped the query must not
    read the answer as if it had.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

if __package__ in (None, ''):  # executed as a script rather than a module
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recordings_mcp import (PROTOCOL_VERSION, SERVICE_LABEL_RU, SERVICE_NAME,
                            VERSION, config as config_module, grants,
                            semantic_adapter, store)

# recording_codes lives with the transactional archive writer, but parsing a
# public N-code does not open the archive and is safe on the authorization path.
from recordings_mcp.store import recording_codes

# JSON-RPC. The first five are the standard codes; NOT_FOUND is ours, so a bot
# can tell "there is no such recording" from "you asked wrongly" without
# parsing prose.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
NOT_FOUND = -32001

MAX_BODY_BYTES = 1 << 20
MCP_PATHS = ('/mcp', '/')
# Query parameter names that would mean a caller put a secret in a URL.
_CREDENTIAL_PARAMS = frozenset({
    'token', 'access_token', 'api_key', 'apikey', 'key', 'secret',
    'authorization', 'bearer', 'password'})

_NUMBER_SCHEMA = {
    'anyOf': [{'type': 'string'}, {'type': 'integer'}],
    'description': ('Permanent recording number: the canonical code in any '
                    'case (N-0068 / n-0068), or, since this service holds one '
                    'archive, the bare number (68).'),
}

TOOLS = {
    'recordings_list': {
        'description': ('List this archive\'s recordings, newest first, one '
                        'bounded page at a time.'),
        'handler': store.list_recordings,
        'required': (),
        'schema': {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'limit': {'type': 'integer', 'minimum': 1,
                          'maximum': store.MAX_LIMIT,
                          'description': f'Page size (default '
                                         f'{store.DEFAULT_LIMIT}, max '
                                         f'{store.MAX_LIMIT}).'},
                'cursor': {'type': 'string',
                           'description': 'Opaque cursor from next_cursor.'},
                'filters': {
                    'type': 'object',
                    'additionalProperties': False,
                    'properties': {
                        'date_from': {'type': 'string', 'description': 'YYYY-MM-DD'},
                        'date_to': {'type': 'string', 'description': 'YYYY-MM-DD'},
                        'min_duration_ms': {'type': 'integer', 'minimum': 0},
                    },
                },
            },
        },
    },
    'recordings_search': {
        'description': ('Full-text search (FTS5) over recording names and '
                        'stored transcripts.'),
        'handler': store.search_recordings,
        'required': ('query',),
        'schema': {
            'type': 'object',
            'additionalProperties': False,
            'required': ['query'],
            'properties': {
                'query': {'type': 'string', 'description': 'Words to look for.'},
                'limit': {'type': 'integer', 'minimum': 1,
                          'maximum': store.MAX_LIMIT},
                'cursor': {'type': 'string'},
            },
        },
    },
    'recordings_hybrid_search': {
        'description': ('Hybrid lexical and tenant-local semantic search over '
                        'this archive. Exact lexical matches rank first.'),
        'handler': semantic_adapter.search_recordings_hybrid,
        'required': ('query',),
        'schema': {
            'type': 'object',
            'additionalProperties': False,
            'required': ['query'],
            'properties': {
                'query': {'type': 'string', 'minLength': 3,
                          'description': 'Meaning or words to look for.'},
                'limit': {'type': 'integer', 'minimum': 1,
                          'maximum': semantic_adapter.MAX_HYBRID_RESULTS},
            },
        },
    },
    'recording_get': {
        'description': ('One recording: metadata, the preferred transcript '
                        '(ASR, falling back to PLAUD), the generated Russian '
                        'summary, and which ASR engine produced it.'),
        'handler': store.get_recording,
        'required': ('number',),
        'schema': {
            'type': 'object',
            'additionalProperties': False,
            'required': ['number'],
            'properties': {'number': _NUMBER_SCHEMA},
        },
    },
    'recording_mindmap_get': {
        'description': 'Structured mind-map data for one recording.',
        'handler': store.get_mindmap,
        'required': ('number',),
        'schema': {
            'type': 'object',
            'additionalProperties': False,
            'required': ['number'],
            'properties': {'number': _NUMBER_SCHEMA},
        },
    },
}


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _scope_result(name, grant, structured):
    """Enforce least-privilege response contracts after authorized access."""
    if name == 'recordings_list':
        result = dict(structured)
        allowed = ('number', 'name', 'title', 'source_name',
                   'started_at', 'duration_ms', 'language')
        if 'transcript' in grant.scopes:
            allowed += ('has_transcript',)
        if 'summary' in grant.scopes:
            allowed += ('has_summary', 'summary_preview')
        result['items'] = [{key: value for key, value in item.items()
                            if key in allowed} for item in structured['items']]
        return result
    if name != 'recording_get':
        return structured
    result = {key: structured[key] for key in
              ('number', 'name', 'title', 'source_name',
               'started_at', 'duration_ms', 'language')}
    if 'transcript' in grant.scopes:
        result['transcript'] = structured['transcript']
        result['asr'] = structured['asr']
    if 'summary' in grant.scopes:
        result['summary'] = structured['summary']
        result['has_mindmap'] = structured['has_mindmap']
    # Audio is deliberately never a grantable capability in this release, so
    # even its presence bit is not part of a caller's response contract.
    return result


def call_tool(config, name, arguments, registry=None, token=None):
    """Dispatch one tool call against the process's fixed scope."""
    tool = TOOLS.get(name)
    if tool is None:
        raise RpcError(METHOD_NOT_FOUND, f'unknown tool {name!r}')
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise RpcError(INVALID_PARAMS, 'arguments must be an object')

    allowed = set(tool['schema']['properties'])
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        # Scope-shaped arguments land here: this service serves exactly one
        # tenant, chosen by its own configuration, and says so plainly.
        raise RpcError(
            INVALID_PARAMS,
            f'unsupported argument {unknown[0]!r} for {name}; this service '
            f'serves a single configured archive and accepts only '
            f'{", ".join(sorted(allowed))}')
    missing = [key for key in tool['required'] if key not in arguments]
    if missing:
        raise RpcError(INVALID_PARAMS,
                       f'{missing[0]!r} is required for {name}')
    # No argument this service takes is nullable, so an explicit null is a
    # caller bug rather than "use the default" — reading it as the default
    # would answer a question nobody asked.
    nulls = sorted(key for key, value in arguments.items() if value is None)
    if nulls:
        raise RpcError(INVALID_PARAMS,
                       f'{nulls[0]!r} must not be null')

    number = None
    if name in ('recording_get', 'recording_mindmap_get'):
        try:
            # Parsing is pure and does not open the archive. This is the only
            # identity work permitted before the grant check.
            number = recording_codes.parse_code(arguments['number'], config.code_prefix)
        except recording_codes.RecordingCodeError as exc:
            raise RpcError(INVALID_PARAMS, str(exc)) from None

    try:
        grant = None
        if registry is not None:
            # Authorize before dispatch/archive access. A well-formed absent N
            # code and a forbidden N code therefore share one opaque response.
            try:
                grant = registry.authorize(token, name, number)
            except grants.AuthorizationError:
                registry.audit_denied(token, name, number)
                raise
        handler_arguments = dict(arguments)
        if number is not None:
            handler_arguments['number'] = number
        if grant is not None and name in ('recordings_list', 'recordings_search',
                                           'recordings_hybrid_search'):
            # Predicate before LIMIT/OFFSET keeps a narrow allowlist pageable.
            handler_arguments['allowed_numbers'] = grant.recordings
        if grant is not None and name == 'recordings_list':
            allowed_filters = store.METADATA_FILTERS
            if 'summary' in grant.scopes:
                allowed_filters |= store.SUMMARY_FILTERS
            if 'transcript' in grant.scopes:
                allowed_filters |= store.TRANSCRIPT_FILTERS
            # list_recordings validates filters before opening the archive.
            handler_arguments['allowed_filters'] = allowed_filters
        structured = tool['handler'](config, **handler_arguments)
        if registry is not None:
            registry.audit(grant, name, number, 'allowed')
            return _scope_result(name, grant, structured)
        return structured
    except grants.AuthorizationError:
        raise RpcError(INVALID_PARAMS, 'request is not authorized') from None
    except store.InvalidArgument as exc:
        raise RpcError(INVALID_PARAMS, str(exc)) from None
    except store.NotFound as exc:
        raise RpcError(NOT_FOUND, str(exc)) from None
    except store.Unavailable as exc:
        # Unavailable messages are written to be shown: they say what an
        # operator has to fix and never name a file or a query.
        raise RpcError(INTERNAL_ERROR,
                       str(exc) or 'the recordings archive is not available') \
            from None


def dispatch(config, message, registry=None, token=None):
    """Handle one JSON-RPC request object; return its `result` payload."""
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
        raise RpcError(INVALID_REQUEST, 'not a JSON-RPC 2.0 request')
    method = message.get('method')
    params = message.get('params') or {}
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, 'params must be an object')

    if method == 'initialize':
        return {
            'protocolVersion': PROTOCOL_VERSION,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': SERVICE_NAME, 'title': SERVICE_LABEL_RU,
                           'version': VERSION},
        }
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': [{
            'name': name,
            'description': tool['description'],
            'inputSchema': tool['schema'],
        } for name, tool in sorted(TOOLS.items())]}
    if method == 'tools/call':
        name = params.get('name')
        if not isinstance(name, str) or not name:
            raise RpcError(INVALID_PARAMS, 'a tool name is required')
        structured = call_tool(config, name, params.get('arguments'), registry, token)
        return {
            'content': [{'type': 'text',
                         'text': json.dumps(structured, ensure_ascii=False)}],
            'structuredContent': structured,
            'isError': False,
        }
    raise RpcError(METHOD_NOT_FOUND, f'unknown method {method!r}')


def build_handler(config):
    registry = grants.GrantRegistry(config.grant_state_path)

    class Handler(BaseHTTPRequestHandler):
        server_version = f'{SERVICE_NAME}/{VERSION}'
        sys_version = ''          # no interpreter version on the wire
        protocol_version = 'HTTP/1.1'

        # ------------------------------------------------------------ logging
        # Built from the path, never from the request line: the request line
        # carries the query string, and a caller that mistakenly put a token
        # there must not have it persisted by us as well.
        def _log(self, code):
            path = urlsplit(self.path or '').path
            sys.stderr.write(f'{SERVICE_NAME} {self.command} {path} {code}\n')

        def log_request(self, code='-', size='-'):
            self._log(code)

        def log_message(self, fmt, *args):
            pass

        def log_error(self, fmt, *args):
            pass

        # ----------------------------------------------------------- plumbing
        def _send(self, status, payload=None, close=False, extra_headers=None):
            body = b'' if payload is None else json.dumps(
                payload, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            if body:
                self.send_header('Content-Type', 'application/json; charset=utf-8')
            for header, value in (extra_headers or {}).items():
                self.send_header(header, value)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if close:
                # Early denials deliberately do not consume attacker-controlled
                # bodies. Closing prevents the unread bytes becoming a request.
                self.send_header('Connection', 'close')
                self.close_connection = True
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _rpc_error(self, code, message, request_id=None):
            self._send(200, {'jsonrpc': '2.0', 'id': request_id,
                             'error': {'code': code, 'message': message}})

        def _query_is_hostile(self):
            """True when the URL carries something credential-shaped."""
            query = urlsplit(self.path or '').query
            if not query:
                return False, False
            names = {name.lower() for name in parse_qs(query, keep_blank_values=True)}
            return True, bool(names & _CREDENTIAL_PARAMS)

        def _bearer_token(self):
            header = self.headers.get('Authorization', '') or ''
            scheme, _, presented = header.partition(' ')
            if scheme.lower() != 'bearer' or not presented.strip():
                return None
            return presented.strip()

        def _read_body(self):
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                return b''
            if length <= 0:
                return b''
            if length > MAX_BODY_BYTES:
                return None
            return self.rfile.read(length)

        # ---------------------------------------------------------- endpoints
        def do_GET(self):
            path = urlsplit(self.path or '').path
            if path == '/healthz':
                return self._send(200, {'ok': True, 'service': SERVICE_NAME,
                                        'label': SERVICE_LABEL_RU,
                                        'version': VERSION})
            if path == '/readyz':
                return self._readiness()
            return self._send(404, {'error': 'not found'})

        def _readiness(self):
            payload = {'service': SERVICE_NAME, 'version': VERSION,
                       'tenant': config.tenant_id, 'series': config.code_prefix}
            try:
                registry.health()
                payload.update(store.counts(config))
                payload['ready'] = True
                return self._send(200, payload)
            except Exception:
                # Never the exception text: it would name the file that could
                # not be opened.
                payload.update({'ready': False, 'recordings': 0})
                return self._send(503, payload)

        def do_POST(self):
            has_query, credential_shaped = self._query_is_hostile()
            if credential_shaped:
                return self._send(401, {'error': 'unauthorized'}, close=True)
            if has_query:
                return self._send(400, {'error': 'this endpoint takes no query '
                                                 'parameters'})
            if urlsplit(self.path or '').path not in MCP_PATHS:
                return self._send(404, {'error': 'not found'})
            token = self._bearer_token()
            if not token:
                # No detail: which of missing/malformed/wrong it was is not the
                # caller's business. Do not read its body; close instead.
                return self._send(401, {'error': 'unauthorized'}, close=True,
                                  extra_headers={'WWW-Authenticate': 'Bearer'})

            try:
                registry.authorize(token, '__authenticate__')
            except grants.AuthorizationError:
                return self._send(401, {'error': 'unauthorized'}, close=True)

            raw = self._read_body()
            if raw is None:
                return self._send(413, {'error': 'request too large'}, close=True)
            try:
                message = json.loads(raw.decode('utf-8')) if raw.strip() else None
            except (ValueError, UnicodeDecodeError):
                return self._rpc_error(PARSE_ERROR, 'invalid JSON')
            if message is None:
                return self._rpc_error(PARSE_ERROR, 'invalid JSON')
            if isinstance(message, list):
                return self._rpc_error(
                    INVALID_REQUEST, 'batched requests are not supported')

            request_id = message.get('id') if isinstance(message, dict) else None
            is_notification = isinstance(message, dict) and 'id' not in message

            try:
                # Every RPC request reopens the writable grant state. A revoked,
                # expired, missing, or corrupt state fails before dispatch.
                registry.authorize(token, '__authenticate__')
                result = dispatch(config, message, registry, token)
            except grants.AuthorizationError:
                if is_notification:
                    return self._send(202)
                return self._rpc_error(INVALID_PARAMS, 'request is not authorized', request_id)
            except RpcError as exc:
                if is_notification:
                    return self._send(202)
                return self._rpc_error(exc.code, exc.message, request_id)
            except Exception:
                # The detail goes to the operator's log, never to the caller:
                # an exception string is exactly where a path or a fragment of
                # SQL would escape.
                sys.stderr.write(
                    f'{SERVICE_NAME} internal error handling '
                    f'{message.get("method")!r}\n')
                traceback.print_exc(file=sys.stderr)
                if is_notification:
                    return self._send(202)
                return self._rpc_error(INTERNAL_ERROR, 'internal error',
                                       request_id)

            if is_notification:
                return self._send(202)
            return self._send(200, {'jsonrpc': '2.0', 'id': request_id,
                                    'result': result})

    return Handler


def make_server(config, host=None, port=None) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(
        (host if host is not None else config.host,
         port if port is not None else config.port),
        build_handler(config))
    httpd.daemon_threads = True
    return httpd


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    try:
        config = config_module.load()
    except config_module.ConfigError as exc:
        # A configuration refusal is an operator message, not a crash: no
        # traceback, and a non-zero exit so a supervisor does not restart into
        # the same misconfiguration silently.
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)

    httpd = make_server(config)
    print(config.describe(), file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
