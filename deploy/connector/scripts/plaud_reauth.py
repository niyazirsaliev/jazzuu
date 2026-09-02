#!/usr/bin/env python3
"""plaud_reauth.py — re-authorise ONE PLAUD account via PKCE and install the
resulting tokens-mcp.json into a tenant credential directory.

Needed when PLAUD answers REFRESH_TOKEN_INVALID: the refresh token has been
revoked (rotation lost / account re-authorised elsewhere) and no automated
repair exists — the account owner must approve the grant in a browser.

Usage:
  1) python3 plaud_reauth.py url
       Prints an authorization URL and saves the PKCE verifier to
       /tmp/plaud_reauth_<state>.json. Send the URL to the account owner.
  2) The owner approves. The browser lands on
       http://localhost:8199/auth/callback?code=...&state=...
       (the page will not load — that is expected). They copy the FULL URL back.
  3) python3 plaud_reauth.py exchange '<full callback url>' <dest tokens-mcp.json>
       Exchanges the code (valid ~2 minutes) and writes the token file 0600.

Never prints token values.
"""
import base64, hashlib, json, os, secrets, shutil, sys, time
import urllib.parse, urllib.request, urllib.error

CLIENT_ID = os.environ.get('PLAUD_CLIENT_ID',
                           'client_9c501dad-8a0d-40b2-a7b0-d1cb8787f674')
AUTH_URL = 'https://web.plaud.ai/platform/oauth'
TOKEN_URL = ('https://platform.plaud.ai/developer/api/oauth/'
             'third-party/access-token')
REDIRECT = 'http://localhost:8199/auth/callback'
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36')


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def cmd_url() -> int:
    verifier = b64u(secrets.token_bytes(64))
    challenge = b64u(hashlib.sha256(verifier.encode()).digest())
    state = b64u(secrets.token_bytes(12))
    q = urllib.parse.urlencode({
        'client_id': CLIENT_ID,
        'redirect_uri': REDIRECT,
        'response_type': 'code',
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'state': state,
    })
    path = f'/tmp/plaud_reauth_{state}.json'
    with open(path, 'w') as fh:
        json.dump({'verifier': verifier, 'state': state}, fh)
    os.chmod(path, 0o600)
    print(f'{AUTH_URL}?{q}')
    print(f'\nverifier saved to {path} (state={state})', file=sys.stderr)
    return 0


def cmd_exchange(callback_url: str, dest: str) -> int:
    parsed = urllib.parse.urlparse(callback_url)
    params = urllib.parse.parse_qs(parsed.query)
    code = (params.get('code') or [''])[0]
    state = (params.get('state') or [''])[0]
    if not code:
        print('no ?code= in that URL', file=sys.stderr)
        return 2
    path = f'/tmp/plaud_reauth_{state}.json'
    if not os.path.exists(path):
        print(f'no saved verifier for state={state} ({path})', file=sys.stderr)
        return 2
    verifier_blob = json.load(open(path))
    verifier = verifier_blob['verifier']
    saved_state = verifier_blob.get('state')
    if saved_state and saved_state != state:
        print(f'state mismatch: callback={state} verifier file={saved_state}',
              file=sys.stderr)
        return 2

    # Field set and order match official @plaud-ai/mcp 0.3.7 exchangeCode():
    # code, redirect_uri, code_verifier, state — and NO grant_type.
    fields = {
        'code': code,
        'redirect_uri': REDIRECT,
        'code_verifier': verifier,
    }
    if state:
        fields['state'] = state
    body = urllib.parse.urlencode(fields).encode()
    basic = base64.b64encode(f'{CLIENT_ID}:'.encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=body, method='POST', headers={
        'Authorization': f'Basic {basic}',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json',
        'User-Agent': UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print('token exchange HTTP error', e.code, file=sys.stderr)
        return 1

    if not payload.get('access_token'):
        print('token exchange returned no access token', file=sys.stderr)
        return 1
    exp = payload.get('expires_at')
    exp = float(exp) if exp else time.time() + float(payload.get('expires_in') or 3600)
    if exp > 20_000_000_000:
        exp /= 1000.0
    out = {
        'access_token': payload['access_token'],
        'refresh_token': payload.get('refresh_token', ''),
        'token_type': payload.get('token_type') or 'Bearer',
        # plaud-mcp stores and compares expiry in epoch milliseconds.
        'expires_at': int(exp * 1000),
    }
    if os.path.exists(dest):
        shutil.copy2(dest, f'{dest}.bak.{int(time.time())}')
        st = os.stat(dest)
        uid, gid = st.st_uid, st.st_gid
    else:
        uid = gid = 10000
    tmp = dest + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(out, fh, indent=2)
        fh.write('\n')
    os.chmod(tmp, 0o600)
    try:
        os.chown(tmp, uid, gid)
    except PermissionError:
        pass
    os.replace(tmp, dest)
    os.unlink(path)
    print(json.dumps({'ok': True, 'dest': dest,
                      'access_token_len': len(out['access_token']),
                      'has_refresh': bool(out['refresh_token']),
                      'expires_at': out['expires_at']}))
    return 0


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'url':
        sys.exit(cmd_url())
    if len(sys.argv) > 3 and sys.argv[1] == 'exchange':
        sys.exit(cmd_exchange(sys.argv[2], sys.argv[3]))
    print(__doc__)
    sys.exit(2)
