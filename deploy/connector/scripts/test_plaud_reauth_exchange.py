#!/usr/bin/env python3
"""Offline contract test for plaud_reauth.cmd_exchange().

Asserts the outbound token POST matches official @plaud-ai/mcp 0.3.7
exchangeCode(): exact URL, headers, and form fields (state present,
grant_type absent). urlopen is replaced, so NO network call is ever made
and no real code/credential is used.

Run: python3 test_plaud_reauth_exchange.py
"""
import base64, importlib.util, json, os, sys, tempfile
import urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    'plaud_reauth', os.path.join(HERE, 'plaud_reauth.py'))
pr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr)

FAILURES = []


def check(cond, msg):
    print(('  ok   ' if cond else '  FAIL ') + msg)
    if not cond:
        FAILURES.append(msg)


class FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run_exchange(state='STATEXYZ', code='CODE123', file_state=None,
                 verifier='VERIFIER456'):
    """Returns (rc, captured_request_or_None) with urlopen intercepted."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['req'] = req
        return FakeResp({'access_token': 'AAA', 'refresh_token': 'RRR',
                         'token_type': 'Bearer', 'expires_in': 3600})

    vpath = f'/tmp/plaud_reauth_{state}.json'
    with open(vpath, 'w') as fh:
        json.dump({'verifier': verifier,
                   'state': state if file_state is None else file_state}, fh)
    dest = tempfile.mktemp(suffix='.json')
    cb = f'http://localhost:8199/auth/callback?code={code}&state={state}'
    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        rc = pr.cmd_exchange(cb, dest)
    finally:
        urllib.request.urlopen = orig
    saved = json.load(open(dest)) if os.path.exists(dest) else None
    for p in (vpath, dest):
        if os.path.exists(p):
            os.unlink(p)
    return rc, captured.get('req'), saved


print('1. outbound POST matches official 0.3.7')
rc, req, saved = run_exchange()
check(rc == 0, 'exchange returned rc=0')
check(req is not None, 'a request was built')
check(saved['expires_at'] > 20_000_000_000,
      'expiry is persisted as epoch milliseconds for plaud-mcp')
check(req.full_url == 'https://platform.plaud.ai/developer/api/oauth/'
      'third-party/access-token', f'URL == official token URL ({req.full_url})')
check(req.get_method() == 'POST', 'method is POST')

h = {k.lower(): v for k, v in req.headers.items()}
check(h.get('Content-type'.lower()) == 'application/x-www-form-urlencoded',
      'Content-Type: application/x-www-form-urlencoded')
check(h.get('accept') == 'application/json', 'Accept: application/json')
expected_basic = 'Basic ' + base64.b64encode(
    f'{pr.CLIENT_ID}:'.encode()).decode()
check(h.get('authorization') == expected_basic,
      'Authorization: Basic <client_id:> (empty secret, as in official)')
check(set(h) <= {'content-type', 'accept', 'authorization', 'user-agent',
                 'content-length'},
      f'no unexpected headers (got {sorted(h)})')

form = urllib.parse.parse_qs(req.data.decode(), keep_blank_values=True)
check(set(form) == {'code', 'redirect_uri', 'code_verifier', 'state'},
      f'form fields exactly code/redirect_uri/code_verifier/state (got {sorted(form)})')
check('grant_type' not in form, 'grant_type is ABSENT')
check(form.get('state') == ['STATEXYZ'], 'state field carries callback state')
check(form.get('code') == ['CODE123'], 'code unchanged')
check(form.get('redirect_uri') == ['http://localhost:8199/auth/callback'],
      'redirect_uri unchanged')
check(form.get('code_verifier') == ['VERIFIER456'], 'code_verifier unchanged')
check(req.data.decode().startswith('code=CODE123&redirect_uri='),
      'field order matches official (code, redirect_uri, code_verifier, state)')

print('2. callback state must match the verifier file before any POST')
rc, req, saved = run_exchange(state='GOODSTATE', file_state='OTHERSTATE')
check(rc == 2, f'mismatched state refused (rc={rc})')
check(req is None, 'no POST was attempted on state mismatch')

print('3. unknown state -> no POST')
cb = 'http://localhost:8199/auth/callback?code=X&state=NOSUCHSTATE99'
sent = []
orig = urllib.request.urlopen
urllib.request.urlopen = lambda *a, **k: sent.append(1)
try:
    rc = pr.cmd_exchange(cb, tempfile.mktemp())
finally:
    urllib.request.urlopen = orig
check(rc == 2 and not sent, 'missing verifier file refused with no POST')

print()
if FAILURES:
    print(f'FAILED: {len(FAILURES)}')
    sys.exit(1)
print('ALL TESTS PASSED')
