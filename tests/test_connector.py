"""Tests for the per-tenant PLAUD connector.

A connector holds no PLAUD account credentials. Each tenant uses an isolated
PLAUD MCP service, which owns the OAuth tokens and refreshes them;
the connector is only a caller of that service, authenticating with a bearer
token read from a mounted file. The dangerous parts are therefore which service
a tenant may talk to, and telling apart the failures that look alike.
"""
import importlib
import importlib.util
import http.server
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_module(filename, module_name, env=None):
    """Import an archive/ module under a controlled environment."""
    path = ROOT / "archive" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves annotations via sys.modules[cls.__module__]; a module
    # executed outside sys.modules makes that lookup return None and blow up.
    controlled = dict(env or {})
    tenant_id = controlled.get("TENANT_ID", "")
    if tenant_id and "PLAUD_MCP_TENANT_URLS_JSON" not in controlled:
        controlled["PLAUD_MCP_TENANT_URLS_JSON"] = json.dumps(
            {tenant_id: controlled.get("PLAUD_MCP_EXPECTED_URL")
             or "https://mcp.example/mcp"})
    with mock.patch.dict(sys.modules, {module_name: module}):
        with mock.patch.dict(os.environ, controlled, clear=False):
            spec.loader.exec_module(module)
    return module


def _real_as_doc(raw):
    """Mirror of archive_recording._as_doc for use inside MagicMock doubles."""
    try:
        return json.loads(raw)
    except Exception:
        return None


def _real_safe_recording_ids(ids):
    """The real archive_recording.safe_recording_ids, for MagicMock doubles.

    Imported rather than reimplemented: a copy of the containment rule that
    drifted from the real one would let these tests pass while production
    rejected — or worse, accepted — a different set of ids.
    """
    module = load_module("archive_recording.py", "archive_recording_id_rule",
                         {"TENANT_ID": "", "PLAUD_MCP_TOKEN": "x"})
    return module.safe_recording_ids(ids)


def _json_response(body, headers=None):
    """A urlopen result carrying one JSON body, as a context manager."""
    class FakeResp:
        def __init__(self):
            self.headers = {"Content-Type": "application/json",
                            **(headers or {})}

        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return FakeResp()


def write_caller_token(directory, token="caller-secret"):
    """Provision a tenant's caller token exactly as the host mount does."""
    path = os.path.join(directory, "plaud-mcp-token")
    Path(path).write_text(token)
    os.chmod(path, 0o600)
    return path


class TenantMcpTargetTests(unittest.TestCase):
    """Tenant IDs resolve only through the reviewed deployment mapping."""

    URLS = json.dumps({
        "tenant-alpha": "https://mcp-alpha.example/mcp",
        "tenant-beta": "http://mcp-beta.internal/mcp",
    })

    def load(self, **env):
        module = load_module("tenant.py", f"tenant_target_{id(self)}_{len(env)}")
        base = {"TENANT_ARCHIVE_DIR": "/archive",
                "PLAUD_MCP_TOKEN_FILE": "/creds/plaud-mcp-token",
                "PLAUD_MCP_TENANT_URLS_JSON": self.URLS}
        base.update(env)
        with mock.patch.dict(os.environ, base, clear=True):
            return module.load()

    def test_mapping_selects_exact_tenant_endpoint(self):
        self.assertEqual(self.load(TENANT_ID="tenant-alpha").mcp_url,
                         "https://mcp-alpha.example/mcp")

    def test_missing_mapping_fails_closed(self):
        with self.assertRaises(SystemExit) as caught:
            self.load(TENANT_ID="tenant-alpha", PLAUD_MCP_TENANT_URLS_JSON="")
        self.assertIn("PLAUD_MCP_TENANT_URLS_JSON", str(caught.exception))

    def test_unknown_tenant_fails_closed(self):
        with self.assertRaises(SystemExit) as caught:
            self.load(TENANT_ID="tenant-gamma")
        self.assertIn("tenant-gamma", str(caught.exception))

    def test_malformed_mapping_fails_closed(self):
        with self.assertRaises(SystemExit) as caught:
            self.load(TENANT_ID="tenant-alpha", PLAUD_MCP_TENANT_URLS_JSON="{")
        self.assertIn("valid JSON object", str(caught.exception))

    def test_mapping_rejects_non_http_endpoint(self):
        with self.assertRaises(SystemExit) as caught:
            self.load(TENANT_ID="tenant-alpha", PLAUD_MCP_TENANT_URLS_JSON=json.dumps(
                {"tenant-alpha": "file:///tmp/socket"}))
        self.assertIn("HTTP(S)", str(caught.exception))

    def test_mapping_rejects_shared_endpoint(self):
        shared = "https://mcp.example/mcp"
        with self.assertRaises(SystemExit) as caught:
            self.load(TENANT_ID="tenant-alpha", PLAUD_MCP_TENANT_URLS_JSON=json.dumps(
                {"tenant-alpha": shared, "tenant-beta": shared}))
        self.assertIn("exactly one tenant", str(caught.exception))

    def test_expected_url_is_an_assertion_not_an_override(self):
        with self.assertRaises(SystemExit) as caught:
            self.load(TENANT_ID="tenant-alpha",
                      PLAUD_MCP_EXPECTED_URL="https://other.example/mcp")
        self.assertIn("does not match", str(caught.exception))

    def test_ambient_bare_url_is_ignored(self):
        tenant = self.load(TENANT_ID="tenant-alpha",
                           PLAUD_MCP_URL="https://ambient.example/mcp")
        self.assertEqual(tenant.mcp_url, "https://mcp-alpha.example/mcp")

    def test_missing_caller_token_file_setting_names_the_tenant(self):
        module = load_module("tenant.py", f"tenant_notoken_{id(self)}")
        with mock.patch.dict(os.environ,
                             {"TENANT_ID": "tenant-alpha",
                              "TENANT_ARCHIVE_DIR": "/archive",
                              "PLAUD_MCP_TENANT_URLS_JSON": self.URLS}, clear=True):
            with self.assertRaises(SystemExit) as caught:
                module.load()
        message = str(caught.exception)
        self.assertIn("PLAUD_MCP_TOKEN_FILE", message)
        self.assertIn("tenant-alpha", message)


class TenantResolutionTests(unittest.TestCase):
    URLS = json.dumps({"tenant-alpha": "https://mcp-alpha.example/mcp"})

    def env(self, **extra):
        env = {"TENANT_ID": "tenant-alpha", "TENANT_ARCHIVE_DIR": "/archive", "PLAUD_MCP_TOKEN_FILE": "/creds/plaud-mcp-token", "PLAUD_MCP_TENANT_URLS_JSON": self.URLS}
        env.update(extra)
        return env

    def test_tenant_mode_isolates_paths_and_uses_a_caller_token_file(self):
        module = load_module("tenant.py", "tenant_paths")
        with mock.patch.dict(os.environ, self.env(), clear=True):
            tenant = module.load()
        self.assertEqual(tenant.db_path, "/archive/archive.db")
        self.assertEqual(tenant.audio_dir, "/archive/audio")
        self.assertEqual(tenant.mcp_url, "https://mcp-alpha.example/mcp")
        self.assertEqual(tenant.caller_token_file, "/creds/plaud-mcp-token")
        self.assertIsNone(tenant.static_token)

    def test_describe_reports_caller_token_mode(self):
        module = load_module("tenant.py", "tenant_describe")
        with mock.patch.dict(os.environ, self.env(), clear=True):
            described = module.load().describe()
        self.assertIn("mcp-caller-token", described)
        self.assertNotIn("oauth", described)

    def test_tenant_mode_requires_explicit_archive_dir(self):
        module = load_module("tenant.py", "tenant_incomplete")
        env = {"TENANT_ID": "tenant-alpha", "PLAUD_MCP_TENANT_URLS_JSON": self.URLS}
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(SystemExit):
            module.load()


class CallerTokenTests(unittest.TestCase):
    """The caller token is the connector's only credential now, and it arrives
    as a mounted file so it can be rotated under a running container without a
    restart and without ever landing in the environment."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.token_file = os.path.join(self.dir, "plaud-mcp-token")
        self.auth = load_module("plaud_mcp_auth.py", f"auth_{id(self)}")

    def test_token_is_read_from_the_mounted_file(self):
        Path(self.token_file).write_text("caller-secret-1\n")
        self.assertEqual(
            self.auth.read_caller_token(self.token_file, "tenant-alpha"),
            "caller-secret-1")

    def test_missing_token_file_is_an_actionable_config_error(self):
        """A container started before its token was provisioned must say so in
        terms an operator can act on, naming the tenant and the path."""
        with self.assertRaises(self.auth.PlaudAuthConfigError) as caught:
            self.auth.read_caller_token(self.token_file, "tenant-beta")
        message = str(caught.exception)
        self.assertIn("tenant-beta", message)
        self.assertIn(self.token_file, message)
        self.assertIn("PLAUD_MCP_TOKEN_FILE", message)

    def test_empty_token_file_is_the_same_config_error_not_an_empty_bearer(self):
        """An empty file used to be sent as `Authorization: Bearer ` and came
        back as an opaque 401 from the service. Fail closed here instead."""
        Path(self.token_file).write_text("   \n")
        with self.assertRaises(self.auth.PlaudAuthConfigError) as caught:
            self.auth.read_caller_token(self.token_file, "tenant-alpha")
        self.assertIn("empty", str(caught.exception))

    def test_the_error_never_quotes_the_token(self):
        Path(self.token_file).write_text("caller-secret-1")
        os.chmod(self.token_file, 0o000)
        try:
            with self.assertRaises(self.auth.PlaudAuthConfigError) as caught:
                self.auth.read_caller_token(self.token_file, "tenant-alpha")
        finally:
            os.chmod(self.token_file, 0o600)
        self.assertNotIn("caller-secret-1", str(caught.exception))

    def test_the_file_is_reread_so_rotation_needs_no_restart(self):
        Path(self.token_file).write_text("caller-secret-1")
        self.assertEqual(
            self.auth.read_caller_token(self.token_file, "tenant-alpha"),
            "caller-secret-1")
        Path(self.token_file).write_text("caller-secret-2")
        self.assertEqual(
            self.auth.read_caller_token(self.token_file, "tenant-alpha"),
            "caller-secret-2")


class McpAuthClassificationTests(unittest.TestCase):
    """Four failures look alike from a distance and need very different
    responses: our caller token is wrong (fix the mount), the service has no
    PLAUD credentials yet (seed the service), the service or protocol itself
    failed (read the error), or the network blinked (retry). Collapsing them
    sent an operator hunting a token problem that did not exist while the real
    one sat unfixed."""

    def setUp(self):
        self.auth = load_module("plaud_mcp_auth.py", f"authcls_{id(self)}")

    def http_error(self, code):
        return urllib.error.HTTPError(
            "https://mcp-alpha.example/mcp", code, "denied", {},
            mock.Mock(read=lambda: b'{"detail":"invalid caller token"}'))

    def test_401_is_a_caller_auth_error_naming_the_tenant(self):
        with self.assertRaises(self.auth.PlaudCallerAuthError) as caught:
            self.auth.raise_for_http_error(
                self.http_error(401), "tenant-alpha", "https://mcp-alpha.example/mcp")
        message = str(caught.exception)
        self.assertIn("tenant-alpha", message)
        self.assertIn("401", message)

    def test_403_is_the_same_caller_auth_error(self):
        with self.assertRaises(self.auth.PlaudCallerAuthError):
            self.auth.raise_for_http_error(
                self.http_error(403), "tenant-alpha", "https://mcp-alpha.example/mcp")

    def test_a_500_is_not_reclassified_as_an_auth_failure(self):
        """A transient service error must stay transient: the loop retries it
        next pass, and nobody goes looking for a credential problem."""
        error = self.http_error(503)
        with self.assertRaises(urllib.error.HTTPError):
            self.auth.raise_for_http_error(
                error, "tenant-alpha", "https://mcp-alpha.example/mcp")

    def test_caller_auth_error_is_not_the_config_error(self):
        """Rejected-by-the-service and never-configured need different fixes."""
        self.assertFalse(issubclass(self.auth.PlaudCallerAuthError,
                                    self.auth.PlaudAuthConfigError))
        self.assertFalse(issubclass(self.auth.PlaudAuthConfigError,
                                    self.auth.PlaudCallerAuthError))

    def test_pending_seed_is_a_service_account_error_not_a_caller_one(self):
        """Our caller token was accepted; the SERVICE has no PLAUD account
        credentials yet. Nothing on this side is broken and nothing here can
        fix it — somebody must complete the PLAUD login on that service."""
        payload = {"jsonrpc": "2.0", "id": 9,
                   "error": {"code": -32001,
                             "message": "account credentials pending_seed"}}
        with self.assertRaises(
                self.auth.PlaudServiceAccountNotSeededError) as caught:
            self.auth.raise_for_jsonrpc_error(
                payload, "tenant-beta", "https://mcp-beta.example/mcp")
        message = str(caught.exception)
        self.assertIn("tenant-beta", message)
        self.assertIn("pending_seed", message)

    def test_not_authorised_service_state_is_the_same_error(self):
        """Only an ACCOUNT that is not authorised means unseeded. The marker is
        anchored on the account for a reason: see the bare-"not authorized"
        and caller-rejection tests below."""
        for text in ("service account is not authorised",
                     "account not authorized with PLAUD"):
            payload = {"error": {"code": -32001, "message": text}}
            with self.assertRaises(
                    self.auth.PlaudServiceAccountNotSeededError):
                self.auth.raise_for_jsonrpc_error(
                    payload, "tenant-alpha", "https://mcp-alpha.example/mcp")

    def test_seed_state_reported_in_error_data_is_still_detected(self):
        payload = {"error": {"code": -32001, "message": "upstream unavailable",
                             "data": {"state": "pending_seed"}}}
        with self.assertRaises(self.auth.PlaudServiceAccountNotSeededError):
            self.auth.raise_for_jsonrpc_error(
                payload, "tenant-alpha", "https://mcp-alpha.example/mcp")

    def test_seed_state_reported_as_a_tool_error_result_is_detected(self):
        """MCP servers may report this as a successful call carrying an error
        result rather than a JSON-RPC error object."""
        payload = {"result": {"isError": True, "content": [
            {"type": "text", "text": "pending_seed: run the PLAUD login"}]}}
        with self.assertRaises(self.auth.PlaudServiceAccountNotSeededError):
            self.auth.raise_for_jsonrpc_error(
                payload, "tenant-alpha", "https://mcp-alpha.example/mcp")

    def test_live_not_configured_tool_result_is_detected_as_unseeded(self):
        """plaud-mcp 3.4.4 reports an empty token store as a successful MCP
        tool result, with isError=false. This exact shape must still fail the
        readiness probe rather than pretending the account has no files."""
        detail = ("PLAUD token store is empty. The central token has not been "
                  "seeded yet (admin bootstrap, gated).")
        payload = {"jsonrpc": "2.0", "id": 9, "result": {
            "content": [{"type": "text", "text": json.dumps({
                "status": "not_configured", "detail": detail,
                "reason": "token store empty; admin bootstrap required"})}],
            "structuredContent": {
                "status": "not_configured", "detail": detail,
                "reason": "token store empty; admin bootstrap required"},
            "isError": False}}
        with self.assertRaises(
                self.auth.PlaudServiceAccountNotSeededError):
            self.auth.raise_for_jsonrpc_error(
                payload, "tenant-alpha", "https://mcp-alpha.example/mcp")

    def test_unrelated_not_configured_result_is_not_guessed_as_unseeded(self):
        """not_configured alone may describe a non-auth feature. Require token
        store/bootstrap evidence before sending an operator to PLAUD login."""
        payload = {"result": {"isError": False,
                              "structuredContent": {
                                  "status": "not_configured",
                                  "detail": "optional export is disabled"}}}
        self.auth.raise_for_jsonrpc_error(
            payload, "tenant-alpha", "https://mcp-alpha.example/mcp")

    def test_an_unrelated_jsonrpc_error_is_raised_as_a_generic_mcp_error(self):
        """Previously every non-seed error was handed back to the caller, and
        `call` turned it into the envelope text — which parses to no files and
        no transcript. An outage then looked exactly like an empty account, so
        the archive quietly stopped growing and nothing said why."""
        payload = {"error": {"code": -32602, "message": "unknown tool"}}
        with self.assertRaises(self.auth.PlaudMcpServiceError) as caught:
            self.auth.raise_for_jsonrpc_error(
                payload, "tenant-alpha", "https://mcp-alpha.example/mcp")
        message = str(caught.exception)
        self.assertIn("tenant-alpha", message)
        self.assertIn("unknown tool", message)
        self.assertIn("-32602", message)

    def test_a_tool_error_result_with_no_known_marker_is_still_an_error(self):
        """isError is the other way a service says "this call failed". It
        arrives inside a 200 with a `result`, so nothing else notices it."""
        payload = {"result": {"isError": True, "content": [
            {"type": "text", "text": "upstream PLAUD API returned 500"}]}}
        with self.assertRaises(self.auth.PlaudMcpServiceError) as caught:
            self.auth.raise_for_jsonrpc_error(
                payload, "tenant-alpha", "https://mcp-alpha.example/mcp")
        self.assertIn("upstream PLAUD API returned 500", str(caught.exception))

    def test_a_caller_token_rejected_in_a_200_body_is_a_caller_auth_error(self):
        """Not every service rejects a bad caller token with 401. One that
        answers 200 + JSON-RPC error is saying the same thing, and it needs the
        same repair: reissue the token and replace the mounted file."""
        for text in ("invalid_token",
                     "the bearer token is invalid",
                     "caller token has been revoked",
                     "expired token",
                     "caller not authorized",
                     "caller is not authorised for this service"):
            payload = {"error": {"code": -32000, "message": text}}
            with self.subTest(text=text):
                with self.assertRaises(
                        self.auth.PlaudCallerAuthError) as caught:
                    self.auth.raise_for_jsonrpc_error(
                        payload, "tenant-alpha", "https://mcp-alpha.example/mcp")
                # Emphatically NOT the seed error: that would send someone to
                # re-run a PLAUD login on a service whose account is fine.
                self.assertNotIsInstance(
                    caught.exception,
                    self.auth.PlaudServiceAccountNotSeededError)

    def test_a_bare_not_authorized_is_not_assumed_to_mean_unseeded(self):
        """The old marker list matched any "not authorized" anywhere in the
        payload, so a rejection of OUR token was reported as the service's
        account being unseeded. Unqualified text now stays generic."""
        payload = {"error": {"code": -32000, "message": "not authorized"}}
        with self.assertRaises(self.auth.PlaudMcpServiceError):
            self.auth.raise_for_jsonrpc_error(
                payload, "tenant-alpha", "https://mcp-alpha.example/mcp")

    def test_a_structured_state_is_believed_before_free_text(self):
        """A machine-readable field is unambiguous; prose is the fallback."""
        payload = {"error": {"code": -32000, "message": "request failed",
                             "data": {"error": "invalid_token"}}}
        with self.assertRaises(self.auth.PlaudCallerAuthError):
            self.auth.raise_for_jsonrpc_error(
                payload, "tenant-alpha", "https://mcp-alpha.example/mcp")
        payload = {"error": {"code": -32000, "message": "request failed",
                             "data": {"state": "not_seeded"}}}
        with self.assertRaises(self.auth.PlaudServiceAccountNotSeededError):
            self.auth.raise_for_jsonrpc_error(
                payload, "tenant-alpha", "https://mcp-alpha.example/mcp")

    def test_upstream_oauth_states_are_not_blamed_on_the_caller_token(self):
        for state in ("invalid_grant", "invalid_client", "unauthorized",
                      "unauthorised"):
            payload = {"error": {"code": -32000, "message": "upstream failed",
                                 "data": {"error": state}}}
            with self.subTest(state=state):
                with self.assertRaises(self.auth.PlaudMcpServiceError) as caught:
                    self.auth.raise_for_jsonrpc_error(
                        payload, "tenant-alpha", "https://mcp-alpha.example/mcp")
                self.assertNotIsInstance(caught.exception,
                                         self.auth.PlaudCallerAuthError)

    def test_pending_seed_overrides_ambiguous_upstream_oauth_state(self):
        payload = {"error": {"code": -32000, "message": "upstream failed",
                             "data": {"error": "invalid_grant",
                                      "status": "pending_seed"}}}
        with self.assertRaises(
                self.auth.PlaudServiceAccountNotSeededError):
            self.auth.raise_for_jsonrpc_error(
                payload, "tenant-alpha", "https://mcp-alpha.example/mcp")

    def test_upstream_token_prose_is_not_blamed_on_the_caller_token(self):
        for text in ("PLAUD refresh token expired; re-run the admin login",
                     "the central PLAUD token was revoked by PLAUD"):
            with self.subTest(text=text):
                with self.assertRaises(self.auth.PlaudMcpServiceError) as caught:
                    self.auth.raise_for_jsonrpc_error(
                        {"error": {"code": -32000, "message": text}},
                        "tenant-alpha", "https://mcp-alpha.example/mcp")
                self.assertNotIsInstance(caught.exception,
                                         self.auth.PlaudCallerAuthError)

    def test_live_seed_prose_shapes_are_classified_as_unseeded(self):
        text = ("PLAUD token store is empty. The central token has not been "
                "seeded yet; admin bootstrap required")
        for payload in (
                {"error": {"code": -32000, "message": text}},
                {"result": {"isError": True, "content": [
                    {"type": "text", "text": text}]}}):
            with self.subTest(payload=payload):
                with self.assertRaises(
                        self.auth.PlaudServiceAccountNotSeededError):
                    self.auth.raise_for_jsonrpc_error(
                        payload, "tenant-alpha", "https://mcp-alpha.example/mcp")

    def test_explicit_structured_caller_state_stays_caller_auth(self):
        for state in ("invalid_token", "invalid_bearer_token",
                      "caller_unauthorized", "caller_not_authorized"):
            with self.subTest(state=state):
                with self.assertRaises(self.auth.PlaudCallerAuthError):
                    self.auth.raise_for_jsonrpc_error(
                        {"error": {"data": {"status": state}}},
                        "tenant-alpha", "https://mcp-alpha.example/mcp")

    def test_a_healthy_payload_raises_nothing(self):
        self.auth.raise_for_jsonrpc_error(
            {"result": {"content": [{"type": "text", "text": "{}"}]}},
            "tenant-alpha", "https://mcp-alpha.example/mcp")

    def test_an_empty_but_successful_result_raises_nothing(self):
        """A recording with no note and an account with no transcripts answer
        with an empty, NON-error result. That is data, not a failure, and it
        must keep flowing through untouched."""
        for payload in ({"result": {"content": []}},
                        {"result": {"content": [
                            {"type": "text", "text": "[]"}]}},
                        {"result": {"isError": False, "content": []}}):
            self.auth.raise_for_jsonrpc_error(
                payload, "tenant-alpha", "https://mcp-alpha.example/mcp")

    def test_the_four_failure_modes_are_mutually_distinguishable(self):
        seeded = self.auth.PlaudServiceAccountNotSeededError
        generic = self.auth.PlaudMcpServiceError
        self.assertFalse(issubclass(seeded, self.auth.PlaudCallerAuthError))
        self.assertFalse(issubclass(seeded, self.auth.PlaudAuthConfigError))
        # The generic service error must not be able to stand in for any of the
        # credential failures, or `except` order in probe() would mis-report it.
        for named in (seeded, self.auth.PlaudCallerAuthError,
                      self.auth.PlaudAuthConfigError):
            self.assertFalse(issubclass(generic, named))
            self.assertFalse(issubclass(named, generic))
        self.assertTrue(issubclass(generic, self.auth.PlaudMcpError))
        # ...and none of them can swallow a transient network failure.
        self.assertFalse(issubclass(urllib.error.URLError,
                                    self.auth.PlaudMcpError))


class TenantMcpClientTests(unittest.TestCase):
    """End of the wire: what archive_recording actually sends to a tenant's
    MCP service, and how it reacts to what comes back."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.token_file = os.path.join(self.dir, "plaud-mcp-token")
        Path(self.token_file).write_text("caller-secret-1")
        self.env = {"TENANT_ID": "tenant-alpha",
                    "TENANT_ARCHIVE_DIR": self.dir,
                    "PLAUD_MCP_TOKEN_FILE": self.token_file}
        self.module = load_module(
            "archive_recording.py", f"arec_client_{id(self)}", self.env)
        self.sent = []
        self.timeouts = []

    def fake_urlopen(self, body=b'{"result":{"content":[]}}', status=None,
                     content_type="application/json"):
        outer = self

        class FakeResp:
            headers = {"Content-Type": content_type}

            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout=None):
            outer.sent.append(dict(req.headers))
            outer.timeouts.append(timeout)
            if status is not None:
                raise urllib.error.HTTPError(
                    req.full_url, status, "denied", {},
                    mock.Mock(read=lambda: b"{}"))
            return FakeResp()

        return opener

    def run_with(self, opener, fn):
        with mock.patch.dict(os.environ, self.env, clear=False), \
             mock.patch.object(self.module.urllib.request, "urlopen", opener):
            return fn()

    def test_requests_carry_the_token_from_the_mounted_file(self):
        self.run_with(self.fake_urlopen(),
                      lambda: self.module.call("list_files", {}, "sid"))
        self.assertEqual(self.sent[0]["Authorization"], "Bearer caller-secret-1")

    def test_a_rotated_token_file_is_picked_up_on_the_next_request(self):
        """Rotation replaces the file under a running container. A token read
        once at import would keep presenting the revoked one until a restart."""
        self.run_with(self.fake_urlopen(),
                      lambda: self.module.call("list_files", {}, "sid"))
        Path(self.token_file).write_text("caller-secret-2")
        self.run_with(self.fake_urlopen(),
                      lambda: self.module.call("list_files", {}, "sid"))
        self.assertEqual(self.sent[-1]["Authorization"], "Bearer caller-secret-2")

    def test_a_missing_token_file_fails_before_any_request_is_made(self):
        os.unlink(self.token_file)
        with self.assertRaises(Exception) as caught:
            self.run_with(self.fake_urlopen(),
                          lambda: self.module.call("list_files", {}, "sid"))
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudAuthConfigError")
        self.assertIn("tenant-alpha", str(caught.exception))
        self.assertEqual(self.sent, [])

    def test_401_from_initialize_is_a_caller_auth_error(self):
        with self.assertRaises(Exception) as caught:
            self.run_with(self.fake_urlopen(status=401),
                          self.module.mcp_connect)
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudCallerAuthError")

    def test_403_from_a_tool_call_is_a_caller_auth_error(self):
        with self.assertRaises(Exception) as caught:
            self.run_with(self.fake_urlopen(status=403),
                          lambda: self.module.call("list_files", {}, "sid"))
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudCallerAuthError")

    def test_401_from_the_initialized_notification_is_a_caller_auth_error(self):
        """The notification is posted with a bare urlopen, outside _post. It
        used to surface a raw HTTPError that read as a transient.

        initialize must therefore SUCCEED here, or the notification is never
        reached: an initialize with no result is refused before this point.
        """
        hello = ({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}},
                 "sid")
        with mock.patch.object(self.module, "_post", return_value=hello):
            with self.assertRaises(Exception) as caught:
                self.run_with(self.fake_urlopen(status=401),
                              self.module.mcp_connect)
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudCallerAuthError")

    def test_a_503_stays_transient(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.run_with(self.fake_urlopen(status=503),
                          lambda: self.module.call("list_files", {}, "sid"))

    def test_pending_seed_from_a_tool_call_is_the_service_account_error(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 9, "error": {
            "code": -32001,
            "message": "PLAUD account credentials pending_seed"}}).encode()
        with self.assertRaises(Exception) as caught:
            self.run_with(self.fake_urlopen(body=body),
                          lambda: self.module.call("list_files", {}, "sid"))
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudServiceAccountNotSeededError")

    def test_pending_seed_from_initialize_is_the_service_account_error(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {
            "code": -32001,
            "message": "the account is not authorised with PLAUD"}}).encode()
        with self.assertRaises(Exception) as caught:
            self.run_with(self.fake_urlopen(body=body), self.module.mcp_connect)
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudServiceAccountNotSeededError")

    def test_a_generic_tool_error_raises_instead_of_returning_the_envelope(self):
        """`call` used to hand an unclassified error back as its return value.
        Every caller parses that value as data, so a broken service produced an
        empty file list and an empty transcript instead of a failure."""
        body = json.dumps({"jsonrpc": "2.0", "id": 9, "error": {
            "code": -32603, "message": "internal error"}}).encode()
        with self.assertRaises(Exception) as caught:
            self.run_with(self.fake_urlopen(body=body),
                          lambda: self.module.call("list_files", {}, "sid"))
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudMcpServiceError")

    def test_an_error_result_raises_for_list_files(self):
        """isError rides inside a 200 with a `result`, so it survived every
        check on the way in and arrived as a listing with no files."""
        body = json.dumps({"jsonrpc": "2.0", "id": 9, "result": {
            "isError": True,
            "content": [{"type": "text", "text": "listing failed"}]}}).encode()
        with self.assertRaises(Exception) as caught:
            self.run_with(self.fake_urlopen(body=body),
                          lambda: self.module.call("list_files", {}, "sid"))
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudMcpServiceError")
        self.assertIn("listing failed", str(caught.exception))

    def test_an_error_result_raises_for_get_file_too(self):
        """get_file decides whether audio is downloaded at all. A tolerated
        error here writes a row with no metadata and no audio."""
        body = json.dumps({"jsonrpc": "2.0", "id": 9, "result": {
            "isError": True,
            "content": [{"type": "text", "text": "no such file"}]}}).encode()
        with self.assertRaises(Exception) as caught:
            self.run_with(
                self.fake_urlopen(body=body),
                lambda: self.module.call("get_file", {"file_id": "f1"}, "sid"))
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudMcpServiceError")

    def test_an_empty_successful_result_is_still_an_ordinary_answer(self):
        """The narrow tolerance that has to survive: an account with no notes
        answers with an empty, non-error result. It is data, not a failure."""
        body = json.dumps({"jsonrpc": "2.0", "id": 9, "result": {
            "content": [{"type": "text", "text": "[]"}]}}).encode()
        out = self.run_with(
            self.fake_urlopen(body=body),
            lambda: self.module.call("get_note", {"file_id": "f1"}, "sid"))
        self.assertEqual(out, "[]")
        self.assertEqual(self.module.extract_summary(out), "")

    def test_a_200_that_rejects_the_caller_token_is_a_caller_auth_error(self):
        """Some services answer a dead caller token with 200 + JSON-RPC error
        rather than 401. Same repair, so it must reach the same exception."""
        body = json.dumps({"jsonrpc": "2.0", "id": 9, "error": {
            "code": -32000,
            "message": "invalid_token: caller token rejected"}}).encode()
        with self.assertRaises(Exception) as caught:
            self.run_with(self.fake_urlopen(body=body),
                          lambda: self.module.call("list_files", {}, "sid"))
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudCallerAuthError")

    def test_a_caller_rejection_is_never_reported_as_an_unseeded_service(self):
        """'caller not authorized' is our token. Reporting it as the service's
        account being unseeded sends an operator to the wrong machine."""
        body = json.dumps({"jsonrpc": "2.0", "id": 9, "error": {
            "code": -32000,
            "message": "caller not authorized for this service"}}).encode()
        with self.assertRaises(Exception) as caught:
            self.run_with(self.fake_urlopen(body=body),
                          lambda: self.module.call("list_files", {}, "sid"))
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudCallerAuthError")

    def test_ingest_keeps_its_long_timeout_by_default(self):
        """The probe needs a short deadline; ingest does not. A 4h recording's
        metadata call must keep the timeout it has always had."""
        self.run_with(self.fake_urlopen(),
                      lambda: self.module.call("list_files", {}, "sid"))
        self.assertEqual(self.timeouts, [self.module.REQUEST_TIMEOUT_S])
        self.assertGreaterEqual(self.module.REQUEST_TIMEOUT_S, 120)

    def test_a_caller_supplied_timeout_bounds_every_request_it_makes(self):
        """mcp_connect posts twice — initialize and the notification — and the
        healthcheck's budget covers both, so both must honour it."""
        self.run_with(self.fake_urlopen(
            body=b'{"jsonrpc":"2.0","id":1,"result":{"capabilities":{}}}'),
            lambda: self.module.mcp_connect(timeout=3))
        self.assertEqual(len(self.timeouts), 2)
        for timeout in self.timeouts:
            self.assertLessEqual(timeout, 3)
        self.run_with(self.fake_urlopen(),
                      lambda: self.module.call("list_files", {}, "sid",
                                               timeout=3))
        self.assertEqual(self.timeouts[-1], 3)

    def test_an_unclassified_http_error_can_never_fall_through(self):
        """Defensive: if the HTTP classifier ever returns instead of raising,
        `_post` would run on with no response at all and fail somewhere far
        away. The HTTPError must leave this function no matter what."""
        with mock.patch.object(self.module._auth, "raise_for_http_error",
                               lambda exc, tenant_id, url: None):
            with self.assertRaises(urllib.error.HTTPError):
                self.run_with(self.fake_urlopen(status=503),
                              lambda: self.module.call("list_files", {}, "sid"))


class McpHandshakeTests(unittest.TestCase):
    """notifications/initialized is the client saying "the handshake is done".

    Sending it after an initialize that failed, or that answered with no
    result at all, tells the service a session exists that does not. Whatever
    the service does with that — accept doomed calls, log a phantom session —
    it starts with the connector asserting something untrue.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = {"TENANT_ID": "tenant-alpha",
                    "TENANT_ARCHIVE_DIR": self.dir,
                    "PLAUD_MCP_TOKEN_FILE": write_caller_token(self.dir)}
        self.module = load_module(
            "archive_recording.py", f"arec_handshake_{id(self)}", self.env)
        self.posted = []

    def opener(self, body):
        """Record every request body; answer them all with `body`."""
        outer = self

        class FakeResp:
            headers = {"Content-Type": "application/json",
                       "Mcp-Session-Id": "sid-1"}

            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def open_(req, timeout=None):
            outer.posted.append(json.loads(req.data.decode()))
            return FakeResp()

        return open_

    def connect_with(self, payload):
        opener = self.opener(json.dumps(payload).encode())
        with mock.patch.dict(os.environ, self.env, clear=False), \
             mock.patch.object(self.module.urllib.request, "urlopen", opener):
            return self.module.mcp_connect()

    def methods(self):
        return [p.get("method") for p in self.posted]

    def test_a_completed_initialize_is_confirmed_with_the_notification(self):
        sid = self.connect_with({
            "jsonrpc": "2.0", "id": 1,
            "result": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        self.assertEqual(sid, "sid-1")
        self.assertEqual(self.methods(),
                         ["initialize", "notifications/initialized"])

    def test_an_initialize_error_sends_no_initialized_notification(self):
        with self.assertRaises(Exception) as caught:
            self.connect_with({"jsonrpc": "2.0", "id": 1, "error": {
                "code": -32001,
                "message": "account credentials pending_seed"}})
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudServiceAccountNotSeededError")
        self.assertEqual(self.methods(), ["initialize"])

    def test_an_initialize_with_no_result_sends_no_notification_either(self):
        """No error and no result is not a handshake. It used to pass straight
        through, and the failure surfaced later as an unrelated tool error."""
        with self.assertRaises(Exception) as caught:
            self.connect_with({"jsonrpc": "2.0", "id": 1})
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudMcpServiceError")
        self.assertEqual(self.methods(), ["initialize"])

    def test_an_empty_body_from_initialize_is_not_a_session(self):
        opener = self.opener(b"")
        with mock.patch.dict(os.environ, self.env, clear=False), \
             mock.patch.object(self.module.urllib.request, "urlopen", opener):
            with self.assertRaises(Exception) as caught:
                self.module.mcp_connect()
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudMcpServiceError")
        self.assertEqual(self.methods(), ["initialize"])

    def test_a_tool_response_with_no_result_is_not_an_empty_answer(self):
        """`call` returns the raw envelope when a result carries no content,
        and every caller parses that. A response with no result at all parsed
        to an empty listing — the exact shape of a healthy, empty account."""
        opener = self.opener(json.dumps({"jsonrpc": "2.0", "id": 9}).encode())
        with mock.patch.dict(os.environ, self.env, clear=False), \
             mock.patch.object(self.module.urllib.request, "urlopen", opener):
            with self.assertRaises(Exception) as caught:
                self.module.call("list_files", {}, "sid")
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudMcpServiceError")


class SseTransportTests(unittest.TestCase):
    """The streamable-HTTP transport answers with an SSE stream, and a stream
    is not one message: it carries notifications, keep-alives and the response,
    in any order, each `data` field possibly split over several lines.

    The parser kept whatever the LAST `data:` line held, so a trailing
    notification overwrote the real answer, a multiline payload was truncated
    to its final line (and then failed to parse), and an error announced before
    the last event was never classified at all.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = {"TENANT_ID": "tenant-alpha",
                    "TENANT_ARCHIVE_DIR": self.dir,
                    "PLAUD_MCP_TOKEN_FILE": write_caller_token(self.dir)}
        self.module = load_module(
            "archive_recording.py", f"arec_sse_{id(self)}", self.env)
        self.requests = []

    def opener(self, build):
        """Serve an SSE stream built from the request that asked for it.

        The stream echoes the id the connector actually sent, so these tests
        prove the response is matched against the outgoing request rather than
        against a constant that happens to agree.
        """
        outer = self

        class FakeResp:
            def __init__(self, body):
                self.body = body
                self.headers = {"Content-Type": "text/event-stream"}

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def open_(req, timeout=None):
            body = json.loads(req.data.decode())
            outer.requests.append(body)
            return FakeResp(build(body).encode())

        return open_

    def run_with(self, opener, fn):
        with mock.patch.dict(os.environ, self.env, clear=False), \
             mock.patch.object(self.module.urllib.request, "urlopen", opener):
            return fn()

    def event(self, data, name="message"):
        """One SSE event, data split across lines exactly as written."""
        lines = "".join(f"data: {part}\n" for part in data.split("\n"))
        return f"event: {name}\n{lines}\n"

    def response(self, rid, text):
        return self.event(json.dumps({
            "jsonrpc": "2.0", "id": rid,
            "result": {"content": [{"type": "text", "text": text}]}}))

    def call_list(self):
        return self.module.call("list_files", {}, "sid")

    def test_the_response_matching_our_request_id_is_the_one_used(self):
        """Last-event-wins returned the trailing notification and threw the
        answer away: a full page of recordings read as an empty account."""
        def stream(body):
            return (self.event(json.dumps({
                        "jsonrpc": "2.0", "method": "notifications/progress",
                        "params": {"progress": 1}}))
                    + self.response(body["id"], '{"data":[{"id":"f1"}]}')
                    + self.response(9999, "WRONG RESPONSE")
                    + self.event(json.dumps({
                        "jsonrpc": "2.0", "method": "notifications/message",
                        "params": {"level": "info"}})))

        out = self.run_with(self.opener(stream), self.call_list)
        self.assertIn("f1", out)
        self.assertNotIn("WRONG", out)
        self.assertNotIn("notifications", out)

    def test_a_multiline_data_field_is_concatenated_not_truncated(self):
        """SSE splits one payload across repeated `data:` lines. Keeping only
        the last line left a JSON fragment that could not parse."""
        def stream(body):
            payload = json.dumps({
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"content": [
                    {"type": "text", "text": "whole answer"}]}}, indent=1)
            self.assertIn("\n", payload)
            return self.event(payload)

        self.assertEqual(self.run_with(self.opener(stream), self.call_list),
                         "whole answer")

    def test_an_unparseable_event_is_skipped_rather_than_raising(self):
        """Keep-alives, comments and half-written frames share the stream with
        the response. None of them may take the pass down."""
        def stream(body):
            return (": keep-alive\n\n"
                    + self.event("not json at all", name="ping")
                    + "event: ping\ndata:\n\n"
                    + self.response(body["id"], "the answer")
                    + self.event('{"broken": '))

        self.assertEqual(self.run_with(self.opener(stream), self.call_list),
                         "the answer")

    def test_a_seed_error_announced_before_a_notification_is_still_caught(self):
        """The service says pending_seed and then keeps talking. Reading only
        the final event turned an unseeded account into a silent empty list."""
        def stream(body):
            return (self.event(json.dumps({
                        "jsonrpc": "2.0", "id": body["id"],
                        "error": {"code": -32001,
                                  "message": "account credentials "
                                             "pending_seed"}}))
                    + self.event(json.dumps({
                        "jsonrpc": "2.0", "method": "notifications/message",
                        "params": {"level": "info", "data": "done"}})))

        with self.assertRaises(Exception) as caught:
            self.run_with(self.opener(stream), self.call_list)
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudServiceAccountNotSeededError")

    def test_a_caller_auth_error_on_any_event_is_classified(self):
        """Even carried by an event that is not our response: a service that
        announces our token is dead is not answering this request either."""
        def stream(body):
            return (self.event(json.dumps({
                        "jsonrpc": "2.0", "id": 4242,
                        "error": {"code": -32000,
                                  "message": "invalid_token"}}))
                    + self.response(body["id"], "{}"))

        with self.assertRaises(Exception) as caught:
            self.run_with(self.opener(stream), self.call_list)
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudCallerAuthError")

    def test_a_response_id_echoed_as_a_string_still_matches(self):
        """JSON-RPC ids match by value, not by type. A service that echoes our
        numeric id as "9" is answering us; now that an unmatched stream is a
        hard failure, refusing to see that would break every call it makes."""
        def stream(body):
            return self.response(str(body["id"]), '{"data":[{"id":"f1"}]}')

        self.assertIn("f1", self.run_with(self.opener(stream), self.call_list))

    def test_a_stream_with_no_response_for_us_is_a_failure_not_empty_data(self):
        def stream(body):
            return self.event(json.dumps({
                "jsonrpc": "2.0", "method": "notifications/progress",
                "params": {"progress": 1}}))

        with self.assertRaises(Exception) as caught:
            self.run_with(self.opener(stream), self.call_list)
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudMcpServiceError")

    def test_a_generic_error_event_is_reported_even_without_our_id(self):
        def stream(body):
            return self.event(json.dumps({
                "jsonrpc": "2.0", "id": 4242,
                "error": {"code": -32603, "message": "internal error"}}))

        with self.assertRaises(Exception) as caught:
            self.run_with(self.opener(stream), self.call_list)
        self.assertEqual(type(caught.exception).__name__,
                         "PlaudMcpServiceError")
        self.assertIn("internal error", str(caught.exception))


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.archive = os.path.join(self.dir, "archive")
        os.makedirs(self.archive)
        self.token_file = write_caller_token(self.dir)
        self.env = {"TENANT_ID": "tenant-alpha",
                    "TENANT_ARCHIVE_DIR": self.archive,
                    "PLAUD_MCP_TOKEN_FILE": self.token_file,
                    "RUN_ASR": "0"}

    def load_connector(self):
        return load_module("connector.py", f"connector_{id(self)}", self.env)

    def test_ensure_layout_creates_viewer_compatible_schema(self):
        conn_mod = self.load_connector()
        conn_mod.ensure_layout()
        db = sqlite3.connect(os.path.join(self.archive, "archive.db"))
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        cols = {r[1] for r in db.execute("PRAGMA table_info(recordings)")}
        db.close()
        # Exactly what the viewer's API queries.
        self.assertTrue({"recordings", "recordings_fts", "asr_attempts",
                         "summary_attempts"} <= tables)
        self.assertTrue({"plaud_segments_json", "summary_json", "asr_meta_json",
                         "asr_alternative_transcript"} <= cols)
        self.assertTrue(os.path.isdir(os.path.join(self.archive, "audio")))

    def test_sync_archives_only_recordings_not_already_present(self):
        conn_mod = self.load_connector()
        conn_mod.ensure_layout()
        db = sqlite3.connect(os.path.join(self.archive, "archive.db"))
        db.execute("INSERT INTO recordings(id,name) VALUES('have','Old')")
        db.commit()
        db.close()
        Path(self.archive, "audio", "have.mp3").write_bytes(b"ID3 complete")

        pages = {1: {"data": [{"id": "have"}, {"id": "new1"}, {"id": "new2"}]}}
        archived = []

        fake = mock.MagicMock()
        fake.mcp_connect.return_value = "sid"
        fake.call.side_effect = lambda name, args, sid: json.dumps(
            pages.get(args.get("page"), {"data": []}))
        fake.archive_one.side_effect = (
            lambda conn, sid, fid, force: archived.append(fid))
        fake.init_db.side_effect = lambda conn: None
        # sync_once parses list_files through archive_recording._as_doc and
        # contains discovered ids through safe_recording_ids, so the double
        # must provide the real helpers rather than MagicMocks.
        fake._as_doc.side_effect = _real_as_doc
        fake.safe_recording_ids.side_effect = _real_safe_recording_ids

        with mock.patch.dict("sys.modules", {"archive_recording": fake}):
            result = conn_mod.sync_once()

        self.assertEqual(archived, ["new1", "new2"])
        self.assertEqual(result["new"], 2)
        self.assertEqual(result["archived"], 2)

    def test_sync_retries_a_discovered_row_until_tenant_local_audio_exists(self):
        """A first get_file may legitimately omit a short-lived download URL.
        The recording metadata is durable, but it must remain a discovery repair
        candidate; it may not be treated as fully archived or enqueued for ASR.
        """
        conn_mod = self.load_connector()
        conn_mod.ensure_layout()
        db = sqlite3.connect(os.path.join(self.archive, "archive.db"))
        db.execute("INSERT INTO recordings(id,name,audio_path) VALUES(?,?,?)",
                   ("late-audio", "Metadata only",
                    os.path.join(self.archive, "audio", "late-audio.mp3")))
        db.commit()
        db.close()
        archived = []
        fake = mock.MagicMock()
        fake.mcp_connect.return_value = "sid"
        fake.call.side_effect = lambda name, args, sid: json.dumps(
            {"data": [{"id": "late-audio"}]} if args.get("page") == 1 else {"data": []})
        fake.archive_one.side_effect = lambda conn, sid, fid, force: archived.append(fid)
        fake.init_db.side_effect = lambda conn: None
        fake._as_doc.side_effect = _real_as_doc
        fake.safe_recording_ids.side_effect = _real_safe_recording_ids

        with mock.patch.dict("sys.modules", {"archive_recording": fake}):
            result = conn_mod.sync_once()

        self.assertEqual(archived, ["late-audio"])
        self.assertEqual(result["new"], 1)

    def test_one_failing_recording_does_not_abort_the_pass(self):
        conn_mod = self.load_connector()
        conn_mod.ensure_layout()
        pages = {1: {"data": [{"id": "a"}, {"id": "bad"}, {"id": "c"}]}}
        done = []

        def archive_one(conn, sid, fid, force):
            if fid == "bad":
                raise RuntimeError("download failed")
            done.append(fid)

        fake = mock.MagicMock()
        fake.mcp_connect.return_value = "sid"
        fake.call.side_effect = lambda name, args, sid: json.dumps(
            pages.get(args.get("page"), {"data": []}))
        fake.archive_one.side_effect = archive_one
        fake.init_db.side_effect = lambda conn: None
        # sync_once parses list_files through archive_recording._as_doc and
        # contains discovered ids through safe_recording_ids, so the double
        # must provide the real helpers rather than MagicMocks.
        fake._as_doc.side_effect = _real_as_doc
        fake.safe_recording_ids.side_effect = _real_safe_recording_ids

        with mock.patch.dict("sys.modules", {"archive_recording": fake}):
            result = conn_mod.sync_once()

        self.assertEqual(done, ["a", "c"])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["archived"], 2)

    def test_connector_refuses_to_run_in_legacy_owner_mode(self):
        module = load_module("connector.py", "connector_owner_legacy", {})
        with mock.patch.object(module, "TENANT",
                               mock.Mock(is_owner=True, tenant_id="owner")):
            with mock.patch.object(module.sys, "argv", ["connector.py", "loop"]):
                self.assertEqual(module.main(), 2)

    def test_explicit_owner_connector_runs_once_with_control_server(self):
        self.env.update({
            "TENANT_ID": "owner",
            "PLAUD_MCP_EXPECTED_URL": "https://mcp-default.example/mcp",
            "ASR_TOKEN_FILE": os.path.join(self.dir, "asr-token"),
            "RUN_ASR": "1",
            "RUN_SUMMARIES": "1",
        })
        module = self.load_connector()
        server = mock.Mock()
        with mock.patch.object(module.control, "ControlServer",
                               return_value=server), \
             mock.patch.object(module.pipeline, "recover_stale_jobs",
                               wraps=module.pipeline.recover_stale_jobs) as recover, \
             mock.patch.object(module, "pass_once") as run, \
             mock.patch.object(module.sys, "argv", ["connector.py", "once"]):
            self.assertFalse(module.TENANT.uses_legacy_static_token)
            self.assertEqual(module.main(), 0)
        run.assert_called_once_with()
        self.assertTrue(any(call.kwargs.get("reclaim_foreign") is True
                            for call in recover.call_args_list))
        server.start.assert_called_once_with()
        server.stop.assert_called_once_with()

    def test_startup_releases_processing_segment_for_queued_asr_job(self):
        self.env.update({
            "TENANT_ID": "owner",
            "PLAUD_MCP_EXPECTED_URL": "https://mcp-default.example/mcp",
            "ASR_TOKEN_FILE": os.path.join(self.dir, "asr-token"),
            "RUN_ASR": "1",
            "RUN_SUMMARIES": "1",
        })
        module = self.load_connector()
        module.ensure_layout()
        conn = sqlite3.connect(module.TENANT.db_path)
        module.pipeline.enqueue(conn, "orphan", module.pipeline.STAGE_ASR)
        conn.execute('''CREATE TABLE IF NOT EXISTS asr_segments(
            id TEXT NOT NULL, seg_index INTEGER NOT NULL,
            start_sec INTEGER NOT NULL, duration_sec INTEGER NOT NULL,
            text TEXT, engine TEXT, meta_json TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            updated_at TEXT, status TEXT, claim_epoch INTEGER,
            claim_owner TEXT, PRIMARY KEY(id, seg_index))''')
        conn.execute('''INSERT INTO asr_segments(
            id,seg_index,start_sec,duration_sec,status,claim_epoch,claim_owner)
            VALUES('orphan',0,0,600,'processing',123,'retired:1')''')
        conn.commit()
        conn.close()

        server = mock.Mock()
        with mock.patch.object(module.control, "ControlServer",
                               return_value=server), \
             mock.patch.object(module, "pass_once"), \
             mock.patch.object(module.sys, "argv", ["connector.py", "once"]):
            self.assertEqual(module.main(), 0)

        conn = sqlite3.connect(module.TENANT.db_path)
        state = conn.execute(
            "SELECT status,claim_epoch,claim_owner FROM asr_segments "
            "WHERE id='orphan' AND seg_index=0").fetchone()
        conn.close()
        self.assertEqual(state, ('pending', None, None))

    def test_startup_releases_processing_review_for_queued_asr_job(self):
        self.env.update({
            "TENANT_ID": "owner",
            "PLAUD_MCP_EXPECTED_URL": "https://mcp-default.example/mcp",
            "ASR_TOKEN_FILE": os.path.join(self.dir, "asr-token"),
            "RUN_ASR": "1",
            "RUN_SUMMARIES": "1",
        })
        module = self.load_connector()
        module.ensure_layout()
        conn = sqlite3.connect(module.TENANT.db_path)
        module.pipeline.enqueue(conn, "orphan-review", module.pipeline.STAGE_ASR)
        conn.execute(
            """INSERT INTO plaud_reviews(
                id,state,claim_epoch,claim_owner,updated_at)
                VALUES('orphan-review','processing',123,'same-container:1','before')"""
        )
        conn.commit()
        conn.close()

        server = mock.Mock()
        with mock.patch.object(module.control, "ControlServer",
                               return_value=server), \
             mock.patch.object(module, "pass_once"), \
             mock.patch.object(module.sys, "argv", ["connector.py", "once"]):
            self.assertEqual(module.main(), 0)

        conn = sqlite3.connect(module.TENANT.db_path)
        state = conn.execute(
            "SELECT state,claim_epoch,claim_owner FROM plaud_reviews "
            "WHERE id='orphan-review'").fetchone()
        conn.close()
        self.assertEqual(state, ('queued', None, None))

    def test_asr_pass_is_skipped_when_no_caller_token_present(self):
        self.env["RUN_ASR"] = "1"
        self.env["ASR_MCP_URL"] = "http://asr.invalid/mcp"
        self.env["ASR_TOKEN_FILE"] = os.path.join(self.dir, "absent-token")
        conn_mod = self.load_connector()
        conn_mod.ensure_layout()
        # Must not raise, and must not try to import/hit ASR.
        conn_mod.run_asr()

    def test_production_pass_never_invokes_legacy_stage_loops(self):
        self.env["RUN_ASR"] = "1"
        self.env["RUN_SUMMARIES"] = "1"
        conn_mod = self.load_connector()
        order = []
        with mock.patch.object(
            conn_mod, "sync_once",
            side_effect=lambda: order.append("sync") or {
                "seen": 0, "new": 0, "archived": 0, "failed": 0,
            },
        ), mock.patch.object(
            conn_mod, "run_asr", side_effect=lambda: order.append("asr"),
        ), mock.patch.object(
            conn_mod, "run_summaries", side_effect=lambda: order.append("summary"),
        ):
            conn_mod.pass_once()

        self.assertEqual(order, ["sync"])

    def test_pipeline_not_legacy_loops_runs_when_discovery_fails(self):
        self.env.update({"RUN_ASR": "1", "RUN_SUMMARIES": "1"})
        module = self.load_connector()
        order = []
        with mock.patch.object(
            module,
            "sync_once",
            side_effect=lambda: order.append("sync") or (_ for _ in ()).throw(
                RuntimeError("PLAUD unavailable")
            ),
        ), mock.patch.object(
            module, "run_asr", side_effect=lambda: order.append("asr")
        ), mock.patch.object(
            module, "run_summaries", side_effect=lambda: order.append("summary")
        ):
            with self.assertRaisesRegex(RuntimeError, "PLAUD unavailable"):
                module.pass_once()

        self.assertEqual(order, ["sync"])



class SourcePollRequestTests(unittest.TestCase):
    def test_requests_coalesce_and_cooldown_without_losing_pre_wait_wake(self):
        module = load_module("connector.py", "connector_source_request", {})
        now = [100.0]
        waker = module.pipeline.Waker()
        requests = module.SourcePollRequest(waker, monotonic=lambda: now[0], cooldown_s=12)
        self.assertEqual(requests.request(), "queued")
        self.assertTrue(waker.wait(0))
        self.assertTrue(requests.consume())
        self.assertEqual(requests.request(), "cooldown")
        now[0] += 12
        self.assertEqual(requests.request(), "queued")
        self.assertEqual(requests.request(), "already_queued")
        self.assertTrue(requests.consume())


class ConnectorReviewTests(unittest.TestCase):
    """The tenant loop transcribes its own audio and validates PLAUD's text.

    Two things must hold every pass: a recording showing nothing gets the queue
    before one that already shows PLAUD text, and neither path may reach for
    transcribe_plaud — this connector has no credentials for the account those
    file_ids belong to.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.archive = os.path.join(self.dir, "archive")
        self.audio = os.path.join(self.archive, "audio")
        os.makedirs(self.audio)
        self.asr_token = os.path.join(self.dir, "asr-token")
        Path(self.asr_token).write_text("asr-caller-secret")
        self.env = {"TENANT_ID": "tenant-alpha",
                    "TENANT_ARCHIVE_DIR": self.archive,
                    "PLAUD_MCP_TOKEN_FILE": write_caller_token(self.dir),
                    "ASR_MCP_URL": "http://127.0.0.1:62362/mcp",
                    "ASR_TOKEN_FILE": self.asr_token,
                    "RUN_ASR": "1", "RUN_SUMMARIES": "0"}
        self.connector = load_module(
            "connector.py", f"connector_review_{id(self)}", self.env)
        self.connector.ensure_layout()
        self.db = sqlite3.connect(os.path.join(self.archive, "archive.db"))
        self.addCleanup(self.db.close)

    def recording(self, rec_id, plaud="", duration_ms=14_000, with_audio=True):
        # Old enough that the PLAUD grace period has passed either way.
        self.db.execute(
            """INSERT INTO recordings(id,name,start_at,duration_ms,
                 plaud_transcript,asr_transcript,archived_at)
               VALUES(?,?,?,?,?,'',?)""",
            (rec_id, f"rec {rec_id}", "2026-08-05T21:00:00", duration_ms,
             plaud, "2026-08-05T21:00:00"))
        self.db.commit()
        if with_audio:
            Path(self.audio, f"{rec_id}.mp3").write_bytes(b"ID3fake")

    def run_pass(self):
        """One run_asr pass against a real asr_backfill bound to this tenant.

        run_asr reloads asr_backfill so a long-lived container picks up a
        rotated token file. Here the module is already bound to this tenant's
        archive, and reloading it would only discard the test's doubles.
        """
        asr = load_module("asr_backfill.py", "asr_backfill",
                          {k: v for k, v in self.env.items()
                           if k in ("TENANT_ID", "TENANT_ARCHIVE_DIR",
                                    "ASR_MCP_URL", "ASR_TOKEN_FILE")})
        calls = []

        def fake_call(name, args, sid, **kwargs):
            calls.append((name, dict(args)))
            return {"text": "локальная расшифровка целиком, много слов " * 6,
                    "engine_used": "large-v3", "meta": {"detected_lang": "ru"}}

        with mock.patch.dict(sys.modules, {"asr_backfill": asr}), \
             mock.patch.object(importlib, "reload", lambda module: module), \
             mock.patch.object(asr, "mcp_connect", return_value="sid"), \
             mock.patch.object(asr, "call", side_effect=fake_call):
            self.connector.run_asr()
        return calls

    def test_layout_creates_the_review_ledger(self):
        tables = {row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("plaud_reviews", tables)

    def test_a_textless_recording_is_transcribed_from_local_audio(self):
        self.recording("silent", plaud="")

        calls = self.run_pass()

        self.assertEqual([name for name, _ in calls], ["transcribe_url"])
        self.assertNotIn("file_id", calls[0][1])
        self.assertTrue(self.db.execute(
            "SELECT asr_transcript FROM recordings WHERE id='silent'"
        ).fetchone()[0])

    def test_textless_work_runs_before_plaud_validation(self):
        self.recording("silent", plaud="")
        self.recording("has-text", plaud="Текст от PLAUD, вполне обычный.")

        self.run_pass()

        state = self.db.execute(
            "SELECT state FROM plaud_reviews WHERE id='has-text'").fetchone()
        self.assertEqual(state[0], "queued")   # enrolled, not yet started

    def test_validation_runs_once_nothing_is_textless(self):
        self.recording("has-text", plaud="Текст от PLAUD, вполне обычный.")

        calls = self.run_pass()

        self.assertEqual([name for name, _ in calls], ["transcribe_url"])
        state, source = self.db.execute(
            "SELECT state, selected_source FROM plaud_reviews WHERE id='has-text'"
        ).fetchone()
        self.assertEqual(state, "reviewed")
        self.assertIn(source, ("plaud", "local"))
        # Whatever won, the PLAUD text itself is untouched.
        self.assertEqual(self.db.execute(
            "SELECT plaud_transcript FROM recordings WHERE id='has-text'"
        ).fetchone()[0], "Текст от PLAUD, вполне обычный.")

    def test_a_pass_with_nothing_to_do_makes_no_call(self):
        self.assertEqual(self.run_pass(), [])


class StatusHealthTests(unittest.TestCase):
    """Local status and live probe must report caller readiness honestly.

    Docker runs `probe`; `status` remains a deterministic local diagnostic.
    Neither may expose the bearer value in JSON or logs.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.archive = os.path.join(self.dir, "archive")
        os.makedirs(self.archive)
        self.token_file = os.path.join(self.dir, "plaud-mcp-token")
        Path(self.token_file).write_text("caller-secret-1")
        self.env = {"TENANT_ID": "tenant-alpha",
                    "TENANT_ARCHIVE_DIR": self.archive,
                    "PLAUD_MCP_TOKEN_FILE": self.token_file}

    def load_connector(self):
        return load_module("connector.py", f"status_{id(self)}", self.env)

    def status(self):
        module = self.load_connector()
        module.ensure_layout()
        return module, module.status()

    def test_a_provisioned_tenant_reports_healthy(self):
        _, out = self.status()
        self.assertTrue(out["ok"])
        self.assertEqual(out["tenant"], "tenant-alpha")
        self.assertEqual(out["mcp_url"], "https://mcp.example/mcp")
        self.assertTrue(out["auth"]["ok"])

    def test_a_missing_caller_token_is_not_healthy(self):
        os.unlink(self.token_file)
        _, out = self.status()
        self.assertFalse(out["ok"])
        self.assertFalse(out["auth"]["ok"])
        self.assertIn("tenant-alpha", out["auth"]["error"])

    def test_an_empty_caller_token_is_not_healthy(self):
        Path(self.token_file).write_text("")
        _, out = self.status()
        self.assertFalse(out["ok"])
        self.assertFalse(out["auth"]["ok"])

    def test_status_exits_non_zero_when_auth_is_unusable(self):
        """The healthcheck reads the exit code, not the JSON."""
        os.unlink(self.token_file)
        module = self.load_connector()
        with mock.patch.object(module.sys, "argv", ["connector.py", "status"]):
            self.assertNotEqual(module.main(), 0)

    def test_status_exits_zero_when_the_tenant_is_provisioned(self):
        module = self.load_connector()
        with mock.patch.object(module.sys, "argv", ["connector.py", "status"]):
            self.assertEqual(module.main(), 0)

    def test_status_never_prints_the_caller_token(self):
        module, out = self.status()
        self.assertNotIn("caller-secret-1", json.dumps(out))
        # The path is not a secret and is what an operator needs to fix it.
        self.assertEqual(out["auth"]["token_file"], self.token_file)

    def auth_module(self, module):
        """The very module object connector.probe() will import.

        load_module() builds a fresh module each call, and a second copy of
        plaud_mcp_auth defines DIFFERENT exception classes, so `except` in the
        connector would not match. Production has exactly one copy.
        """
        return importlib.import_module("plaud_mcp_auth")

    def probe_with(self, module, error):
        fake = mock.MagicMock()
        fake.mcp_connect.side_effect = error
        with mock.patch.dict("sys.modules", {"archive_recording": fake}):
            return module.probe()

    def test_probe_reports_ready_only_after_a_live_file_listing(self):
        module = self.load_connector()
        fake = mock.MagicMock()
        fake.mcp_connect.return_value = "sid"
        fake.call.return_value = json.dumps({"data": []})
        with mock.patch.dict("sys.modules", {"archive_recording": fake}):
            out = module.probe()
        self.assertTrue(out["ok"])
        self.assertEqual(out["mcp_url"], "https://mcp.example/mcp")
        # Every request the probe makes carries the healthcheck's short
        # deadline: Docker kills the check at --timeout and reports nothing.
        fake.mcp_connect.assert_called_once_with(
            timeout=module.PROBE_TIMEOUT_S)
        fake.call.assert_called_once_with(
            "list_files", {"page": 1, "page_size": 1}, "sid",
            timeout=module.PROBE_TIMEOUT_S)

    def test_the_probe_budget_fits_inside_the_docker_healthcheck_timeout(self):
        """Request inactivity timeouts are not a wall-clock bound: an SSE
        server can keep every read alive with periodic comments. The probe must
        therefore own a hard deadline shorter than Docker's kill window."""
        module = self.load_connector()
        self.assertGreaterEqual(module.PROBE_STARTUP_MARGIN_S, 3)
        self.assertLessEqual(
            module.PROBE_DEADLINE_S,
            module.HEALTHCHECK_TIMEOUT_S - module.PROBE_STARTUP_MARGIN_S)

    def test_probe_enforces_a_wall_clock_deadline_while_the_peer_keeps_working(self):
        """A trickling/keep-alive peer may never hit urllib's inactivity
        timeout. Exercise a real streaming HTTP read and require probe() itself
        to return a classified result promptly."""
        module = self.load_connector()
        setattr(module, "PROBE_DEADLINE_S", 0.1)

        class KeepAlive(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                stop_at = time.monotonic() + 1
                try:
                    while time.monotonic() < stop_at:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        time.sleep(0.01)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, format, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), KeepAlive)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        fake = mock.MagicMock()
        url = f"http://127.0.0.1:{server.server_port}/mcp"

        def blocking_connect(timeout):
            request = urllib.request.Request(url, data=b"{}", method="POST")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()

        fake.mcp_connect.side_effect = blocking_connect

        try:
            started = time.monotonic()
            with mock.patch.dict("sys.modules", {"archive_recording": fake}):
                out = module.probe()
            elapsed = time.monotonic() - started
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

        self.assertLess(elapsed, 0.5)
        self.assertFalse(out["ok"])
        self.assertEqual(out["failure"], "transient")
        self.assertIn("wall-clock deadline", out["error"])

    def test_probe_uses_a_sqlite_native_timeout_for_archive_layout(self):
        """SIGALRM cannot reliably interrupt SQLite's C-level busy handler.
        A real exclusive lock must therefore fail through SQLite's own timeout
        well before the probe and Docker deadlines."""
        module = self.load_connector()
        setattr(module, "PROBE_DEADLINE_S", 1)
        setattr(module, "PROBE_DB_TIMEOUT_S", 0.05)
        locker = sqlite3.connect(module.TENANT.db_path, timeout=0)
        locker.execute("BEGIN EXCLUSIVE")
        locker.execute("CREATE TABLE held_by_test(value)")
        fake = mock.MagicMock()

        try:
            started = time.monotonic()
            with mock.patch.dict("sys.modules", {"archive_recording": fake}):
                out = module.probe()
            elapsed = time.monotonic() - started
        finally:
            locker.rollback()
            locker.close()

        self.assertLess(elapsed, 0.5)
        self.assertFalse(out["ok"])
        self.assertEqual(out["failure"], "transient")
        self.assertIn("locked", out["error"].lower())
        fake.mcp_connect.assert_not_called()

    def test_probe_names_a_jsonrpc_failure_as_an_mcp_error_not_a_transient(self):
        """A service answering with a JSON-RPC error is up and refusing, so
        retrying does nothing. Calling it transient told an operator to wait."""
        module = self.load_connector()
        auth = self.auth_module(module)
        fake = mock.MagicMock()
        fake.mcp_connect.return_value = "sid"
        fake.call.side_effect = auth.PlaudMcpServiceError("unknown tool")
        with mock.patch.dict("sys.modules", {"archive_recording": fake}):
            out = module.probe()
        self.assertFalse(out["ok"])
        self.assertEqual(out["failure"], "mcp-error")
        self.assertIn("unknown tool", out["error"])

    def test_probe_fails_on_a_list_files_error_through_the_real_client(self):
        """End to end, through the module the container actually runs: the
        service initializes cleanly and then errors on list_files. That used to
        come back as an empty listing and a healthy probe."""
        module = self.load_connector()
        posted = []

        def opener(req, timeout=None):
            body = json.loads(req.data.decode())
            posted.append(body.get("method"))
            if body.get("method") == "initialize":
                payload = {"jsonrpc": "2.0", "id": body["id"],
                           "result": {"protocolVersion": "2025-06-18"}}
            else:
                payload = {"jsonrpc": "2.0", "id": body.get("id"),
                           "error": {"code": -32603,
                                     "message": "PLAUD upstream exploded"}}
            return _json_response(json.dumps(payload).encode())

        probe_env = {**self.env, "PLAUD_MCP_TENANT_URLS_JSON": json.dumps(
            {self.env["TENANT_ID"]: "https://mcp.example/mcp"})}
        with mock.patch.dict(sys.modules), \
             mock.patch.dict(os.environ, probe_env, clear=False), \
             mock.patch("urllib.request.urlopen", opener):
            sys.modules.pop("archive_recording", None)
            out = module.probe()

        self.assertIn("tools/call", posted)
        self.assertFalse(out["ok"])
        self.assertEqual(out["failure"], "mcp-error")
        self.assertIn("PLAUD upstream exploded", out["error"])

    def test_probe_names_a_rejected_caller_token(self):
        module = self.load_connector()
        auth = self.auth_module(module)
        out = self.probe_with(module, auth.PlaudCallerAuthError("rejected"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["failure"], "caller-auth")

    def test_probe_distinguishes_an_unseeded_service_account(self):
        module = self.load_connector()
        auth = self.auth_module(module)
        fake = mock.MagicMock()
        fake.mcp_connect.return_value = "sid"
        fake.call.side_effect = auth.PlaudServiceAccountNotSeededError(
            "pending_seed")
        with mock.patch.dict("sys.modules", {"archive_recording": fake}):
            out = module.probe()
        self.assertFalse(out["ok"])
        self.assertEqual(out["failure"], "service-account-not-seeded")

    def test_probe_calls_a_network_outage_transient(self):
        module = self.load_connector()
        out = self.probe_with(module, urllib.error.URLError("connection refused"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["failure"], "transient")

    def test_probe_mode_exit_code_follows_the_result(self):
        module = self.load_connector()
        auth = self.auth_module(module)
        fake = mock.MagicMock()
        fake.mcp_connect.side_effect = auth.PlaudCallerAuthError("rejected")
        with mock.patch.dict("sys.modules", {"archive_recording": fake}), \
             mock.patch.object(module.sys, "argv", ["connector.py", "probe"]):
            self.assertNotEqual(module.main(), 0)

        fake.mcp_connect.side_effect = None
        fake.mcp_connect.return_value = "sid"
        with mock.patch.dict("sys.modules", {"archive_recording": fake}), \
             mock.patch.object(module.sys, "argv", ["connector.py", "probe"]):
            self.assertEqual(module.main(), 0)

    def test_probe_never_prints_the_caller_token(self):
        module = self.load_connector()
        fake = mock.MagicMock()
        fake.mcp_connect.return_value = "sid"
        with mock.patch.dict("sys.modules", {"archive_recording": fake}):
            out = module.probe()
        self.assertNotIn("caller-secret-1", json.dumps(out))

    def test_status_makes_no_network_call(self):
        """The healthcheck polls every 60s; it stays deterministic and local.
        Liveness against the service belongs to `probe`."""
        module = self.load_connector()
        module.ensure_layout()
        with mock.patch.object(module, "sync_once",
                               side_effect=AssertionError("no ingest")):
            with mock.patch("urllib.request.urlopen",
                            side_effect=AssertionError("no network in status")):
                module.status()


class StatelessMcpTests(unittest.TestCase):
    """PLAUD's remote MCP answers initialize with no Mcp-Session-Id. Sending
    that None back as a header makes urllib raise TypeError and kills every
    ingest pass before a single file is listed."""

    def test_connect_survives_a_server_that_issues_no_session_id(self):
        tmp = tempfile.mkdtemp()
        env = {"TENANT_ID": "tenant-alpha",
               "TENANT_ARCHIVE_DIR": tmp,
               "PLAUD_MCP_TOKEN_FILE": write_caller_token(tmp)}
        module = load_module("archive_recording.py", "arec_stateless", env)

        sent_headers = []

        class FakeResp:
            headers = {}

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            sent_headers.append(dict(req.headers))
            return FakeResp()

        # A stateless server still answers initialize with a real result; the
        # only thing it withholds is the session id.
        hello = ({"jsonrpc": "2.0", "id": 1,
                  "result": {"protocolVersion": "2025-06-18"}}, None)
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(module, "_post", return_value=hello), \
             mock.patch.object(module.urllib.request, "urlopen", fake_urlopen):
            sid = module.mcp_connect()

        self.assertIsNone(sid)
        # urllib title-cases header keys, hence the capitalisation here.
        for headers in sent_headers:
            self.assertNotIn("Mcp-session-id", headers)
            self.assertNotIn("Mcp-Session-Id", headers)


    def test_a_fresh_account_with_no_transcripts_still_archives(self):
        """PLAUD returns a bare `[]` for get_transcript/get_note on an account
        that has none yet. Treating that as a dict raised AttributeError after
        the audio was already downloaded, leaving files on disk and zero rows.
        """
        tmp = tempfile.mkdtemp()
        env = {"TENANT_ID": "tenant-alpha",
               "TENANT_ARCHIVE_DIR": tmp,
               "PLAUD_MCP_TOKEN_FILE": write_caller_token(tmp)}
        module = load_module("archive_recording.py", "arec_empty", env)

        self.assertEqual(module.extract_plaud_transcript("[]"), "")
        self.assertEqual(module.extract_summary("[]"), "")
        # Malformed and null payloads must not raise either.
        for junk in ("null", "not json", "", "[1,2,3]"):
            self.assertEqual(module.extract_plaud_transcript(junk), "")
            self.assertEqual(module.extract_summary(junk), "")


class OfficialResponseShapeTests(unittest.TestCase):
    """The official server (plaud 0.3.7) and the self-hosted proxy return
    DIFFERENT shapes for the same tools. Reading only the proxy's shape means
    the archive fills with audio that has no text: no crash, no error, just
    permanently empty transcripts and an empty search index.

    Shapes below are the ones observed live against tenant-alpha's account.
    """

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.module = load_module(
            "archive_recording.py", f"arec_shapes_{id(self)}",
            {"TENANT_ID": "tenant-alpha", "TENANT_ARCHIVE_DIR": tmp,
             "PLAUD_MCP_TOKEN_FILE": write_caller_token(tmp)})

    def test_official_transcript_segments_are_extracted(self):
        payload = json.dumps({
            "file_id": "f1", "block": "transaction", "total": 2,
            "offset": 0, "limit": 50, "returned": 2, "next_cursor": None,
            "segments": [
                {"content": "Привет", "speaker": "Speaker 1",
                 "start_time": 0, "end_time": 1},
                {"content": "Как дела", "speaker": "Speaker 2",
                 "start_time": 1, "end_time": 2},
            ]})
        self.assertEqual(self.module.extract_plaud_transcript(payload),
                         "[Speaker 1] Привет\n[Speaker 2] Как дела")

    def test_official_note_list_is_a_bare_list(self):
        payload = json.dumps([
            {"data_id": "n1", "data_type": "summary_note",
             "data_content": "fallback note"},
            {"data_id": "n2", "data_type": "auto_sum_note",
             "data_content": "the real summary"},
        ])
        self.assertEqual(self.module.extract_summary(payload),
                         "the real summary")

    def test_bare_list_note_falls_back_when_no_auto_sum_note(self):
        payload = json.dumps([
            {"data_id": "n1", "data_type": "other_note", "data_content": ""},
            {"data_id": "n2", "data_type": "other_note",
             "data_content": "only content there is"},
        ])
        self.assertEqual(self.module.extract_summary(payload),
                         "only content there is")

    def test_proxy_shape_still_works(self):
        """The owner deployment must not regress."""
        inner = json.dumps([{"speaker": "S1", "content": "proxy text"}])
        payload = json.dumps({"source_list": [
            {"data_type": "transaction", "data_content": inner}]})
        self.assertEqual(self.module.extract_plaud_transcript(payload),
                         "[S1] proxy text")
        note = json.dumps({"note_list": [
            {"data_type": "auto_sum_note", "data_content": "proxy summary"}]})
        self.assertEqual(self.module.extract_summary(note), "proxy summary")

    def test_paginated_transcript_follows_the_cursor_to_the_end(self):
        """A long recording returns segments 50 at a time. Reading only the
        first page silently truncates the transcript to its opening minutes."""
        pages = [
            {"total": 5, "next_cursor": "c1",
             "segments": [{"content": "one"}, {"content": "two"}]},
            {"total": 5, "next_cursor": "c2",
             "segments": [{"content": "three"}, {"content": "four"}]},
            {"total": 5, "next_cursor": None,
             "segments": [{"content": "five"}]},
        ]
        seen_cursors = []

        def fake_call(name, args, sid):
            seen_cursors.append(args.get("cursor"))
            return json.dumps(pages[len(seen_cursors) - 1])

        with mock.patch.object(self.module, "call", side_effect=fake_call):
            text = self.module.fetch_plaud_transcript("f1", None)

        self.assertEqual(text, "one\ntwo\nthree\nfour\nfive")
        self.assertEqual(seen_cursors, [None, "c1", "c2"])

    def test_pagination_stops_when_total_is_reached_despite_a_cursor(self):
        """A server that always echoes a cursor must not spin forever."""
        page = {"total": 2, "next_cursor": "always",
                "segments": [{"content": "a"}, {"content": "b"}]}
        calls = []

        def fake_call(name, args, sid):
            calls.append(args.get("cursor"))
            return json.dumps(page)

        with mock.patch.object(self.module, "call", side_effect=fake_call):
            text = self.module.fetch_plaud_transcript("f1", None)

        self.assertEqual(len(calls), 1)
        self.assertEqual(text, "a\nb")


class TextRepairTests(unittest.TestCase):
    """A row written by a buggy run (audio present, transcript empty) looks
    identical to a healthy one to the "already archived" short-circuit, so it
    would be skipped on every future pass and stay blank forever. That is how
    tenant-alpha's first four recordings ended up as audio with no text.
    """

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.module = load_module(
            "archive_recording.py", f"arec_repair_{id(self)}",
            {"TENANT_ID": "tenant-alpha", "TENANT_ARCHIVE_DIR": tmp,
             "PLAUD_MCP_TOKEN_FILE": write_caller_token(tmp)})
        self.conn = sqlite3.connect(":memory:")
        self.module.init_db(self.conn)

    def _insert(self, rid, **cols):
        keys = ["id"] + list(cols)
        vals = [rid] + list(cols.values())
        self.conn.execute(
            f"INSERT INTO recordings({','.join(keys)}) "
            f"VALUES({','.join('?' * len(keys))})", vals)
        self.conn.commit()

    def test_row_with_audio_but_no_text_is_flagged_for_repair(self):
        self._insert("blank", plaud_transcript="", summary="",
                     duration_ms=253_000)
        self.assertTrue(self.module._needs_text_repair(self.conn, "blank"))

    def test_row_with_a_transcript_is_left_alone(self):
        self._insert("good", plaud_transcript="real text", summary="",
                     duration_ms=253_000)
        self.assertFalse(self.module._needs_text_repair(self.conn, "good"))

    def test_row_with_only_a_summary_is_left_alone(self):
        self._insert("sum", plaud_transcript="", summary="a summary",
                     duration_ms=253_000)
        self.assertFalse(self.module._needs_text_repair(self.conn, "sum"))

    def test_row_with_only_an_asr_transcript_is_left_alone(self):
        self._insert("asr", plaud_transcript="", summary="",
                     asr_transcript="asr text", duration_ms=253_000)
        self.assertFalse(self.module._needs_text_repair(self.conn, "asr"))

    def test_very_short_recording_is_not_refetched_forever(self):
        """Tenant B's 2-second clip genuinely has no transcript. Treating it as
        damaged would refetch it on every single pass, permanently."""
        self._insert("tiny", plaud_transcript="", summary="", duration_ms=2000)
        self.assertFalse(self.module._needs_text_repair(self.conn, "tiny"))

    def test_unknown_id_is_not_a_repair_candidate(self):
        self.assertFalse(self.module._needs_text_repair(self.conn, "nope"))


if __name__ == "__main__":
    unittest.main()


class ConnectorPipelineTests(unittest.TestCase):
    """Production orchestration runs on the durable queue, not on a schedule.

    The connector is the only scheduled thing left in the system, and what it
    is scheduled to do is RECONCILE: ask PLAUD what exists, then drain whatever
    the archive already owes. Everything downstream of a committed job must
    happen in that same pass — there is no second cron behind it any more.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.archive = os.path.join(self.dir, "archive")
        self.audio = os.path.join(self.archive, "audio")
        os.makedirs(self.audio)
        self.asr_token = os.path.join(self.dir, "asr-token")
        Path(self.asr_token).write_text("asr-caller-secret")
        self.env = {"TENANT_ID": "tenant-alpha",
                    "TENANT_ARCHIVE_DIR": self.archive,
                    "PLAUD_MCP_TOKEN_FILE": write_caller_token(self.dir),
                    "ASR_MCP_URL": "http://127.0.0.1:62362/mcp",
                    "ASR_TOKEN_FILE": self.asr_token,
                    "RUN_ASR": "1", "RUN_SUMMARIES": "1"}
        self.connector = load_module(
            "connector.py", f"connector_pipeline_{id(self)}", self.env)
        self.connector.ensure_layout()
        self.db = sqlite3.connect(os.path.join(self.archive, "archive.db"))
        self.addCleanup(self.db.close)

    def recording(self, rec_id, plaud="", duration_ms=14_000, with_audio=True):
        self.db.execute(
            """INSERT INTO recordings(id,name,start_at,archived_at,duration_ms,
                 plaud_transcript,asr_transcript)
               VALUES(?,?,?,?,?,?,'')""",
            (rec_id, f"rec {rec_id}", "2026-08-05T21:00:00",
             "2026-08-05T21:00:00", duration_ms, plaud))
        self.db.commit()
        if with_audio:
            Path(self.audio, f"{rec_id}.mp3").write_bytes(b"ID3fake")

    def jobs(self):
        return [(r[0], r[1], r[2]) for r in self.db.execute(
            "SELECT recording_id, stage, state FROM pipeline_jobs ORDER BY seq")]

    def transcript(self, rec_id):
        return self.db.execute(
            "SELECT COALESCE(asr_transcript,'') FROM recordings WHERE id=?",
            (rec_id,)).fetchone()[0]

    def with_asr(self, text=None):
        """Patch the modules the connector reloads, keeping them tenant-bound."""
        text = text or ("локальная расшифровка целиком, много слов " * 8)
        asr = load_module("asr_backfill.py", "asr_backfill",
                          {k: v for k, v in self.env.items()
                           if k.startswith(("TENANT", "ASR"))})
        summary = load_module("summary_backfill.py", "summary_backfill",
                              {k: v for k, v in self.env.items()
                               if k.startswith(("TENANT", "ASR"))})
        self.asr_calls = []

        def asr_call(name, args, sid, **kwargs):
            self.asr_calls.append((name, dict(args)))
            return {"text": text, "engine_used": "large-v3",
                    "meta": {"detected_lang": "ru"}}

        def summary_call(name, args, sid, **kwargs):
            return {"summary": "## Итог\n\nОбсудили планы.",
                    "summary_json": '{"overview": "Обсудили планы."}'}

        return mock.patch.multiple(
            "builtins", __import__=__import__), [
            mock.patch.dict(sys.modules,
                            {"asr_backfill": asr, "summary_backfill": summary}),
            mock.patch.object(importlib, "reload", lambda module: module),
            mock.patch.object(asr, "mcp_connect", return_value="sid"),
            mock.patch.object(summary, "mcp_connect", return_value="sid"),
            mock.patch.object(asr, "call", side_effect=asr_call),
            mock.patch.object(summary, "call", side_effect=summary_call),
        ]

    def run_pass(self, sync=None, text=None):
        """One production pass, with PLAUD discovery stubbed."""
        _unused, patches = self.with_asr(text=text)
        sync = sync or (lambda: {"seen": 0, "new": 0, "archived": 0,
                                 "failed": 0})
        stack = [mock.patch.object(self.connector, "sync_once",
                                   side_effect=sync)] + patches
        for patch in stack:
            patch.start()
        try:
            self.connector.pass_once()
        finally:
            for patch in reversed(stack):
                patch.stop()

    def test_a_queued_job_is_drained_in_the_very_same_pass(self):
        """No downstream cron. The pass that discovers work also finishes it."""
        self.recording("q1")
        pipeline = self.connector.pipeline
        conn = sqlite3.connect(os.path.join(self.archive, "archive.db"))
        pipeline.ensure_schema(conn)
        pipeline.enqueue(conn, "q1", pipeline.STAGE_ASR)
        conn.close()

        self.run_pass()

        self.assertTrue(self.transcript("q1"))
        self.assertEqual(
            [(rid, stage, state) for rid, stage, state in self.jobs()
             if stage == "asr"], [("q1", "asr", "done")])

    def test_three_missing_token_passes_defer_then_recover_without_strikes(self):
        """A mounted credential arriving late is deployment readiness, not bad
        audio. Repeated passes leave the job owed and runnable immediately when
        the token appears."""
        self.recording("late-token")
        pipeline = self.connector.pipeline
        pipeline.enqueue(self.db, "late-token", pipeline.STAGE_ASR)
        os.remove(self.asr_token)

        for _ in range(3):
            with mock.patch.object(self.connector, "sync_once", return_value={
                "seen": 0, "new": 0, "archived": 0, "failed": 0,
            }):
                self.connector.pass_once()
            self.assertEqual(self.db.execute(
                "SELECT state, attempts FROM pipeline_jobs WHERE recording_id=? "
                "AND stage='asr'", ("late-token",)).fetchone(), ("queued", 0))

        Path(self.asr_token).write_text("asr-caller-secret")
        self.run_pass()

        self.assertTrue(self.transcript("late-token"))
        self.assertEqual(self.db.execute(
            "SELECT state, attempts FROM pipeline_jobs WHERE recording_id=? "
            "AND stage='asr'", ("late-token",)).fetchone(), ("done", 0))

    def test_reconciliation_adopts_a_stranded_recording(self):
        """Audio on disk, a row in the archive, and no job — the interrupted
        enqueue. Production must repair it, not just tests."""
        self.recording("stranded")

        self.run_pass()

        self.assertEqual(
            [(rid, stage) for rid, stage, _state in self.jobs()
             if stage == "asr"], [("stranded", "asr")])
        self.assertTrue(self.transcript("stranded"))

    def test_a_plaud_outage_does_not_stop_local_work_draining(self):
        """Discovery is the only thing that needs PLAUD. Jobs whose audio is
        already on this disk owe nothing to the upstream, and holding them
        because the account is unreachable is the outage spreading."""
        self.recording("local1")

        def dead_plaud():
            raise RuntimeError("PLAUD unavailable")

        with self.assertRaisesRegex(RuntimeError, "PLAUD unavailable"):
            self.run_pass(sync=dead_plaud)

        self.assertTrue(self.transcript("local1"))
        self.assertEqual([s for _r, stage, s in self.jobs() if stage == "asr"],
                         ["done"])

    def test_the_whole_chain_runs_from_one_pass(self):
        """ASR, then the Russian summary, then the derived stages — each one
        enqueued by the stage before it, all inside a single pass."""
        self.recording("chain")

        self.run_pass()

        self.assertEqual(self.jobs(),
                         [("chain", "asr", "done"), ("chain", "summary", "done"),
                          ("chain", "mindmap", "done"), ("chain", "card", "done")])

    def test_historical_summary_category_backfill_runs_on_the_normal_connector_path(self):
        """Legacy structured summaries join the canonical forced summary queue,
        not an operator-only script or an ASR retry."""
        self.recording("legacy", plaud="достаточно длинный текст " * 12, with_audio=False)
        self.db.execute("UPDATE recordings SET summary='# old',summary_json=? WHERE id='legacy'",
                        (json.dumps({"overview": "old"}),))
        self.db.commit()

        self.run_pass()

        self.assertEqual([row for row in self.jobs() if row[0] == "legacy"],
                         [("legacy", "summary", "done"),
                          ("legacy", "mindmap", "done"),
                          ("legacy", "card", "done")])

    def test_local_audio_routing_survives_the_pipeline_path(self):
        """Tenant isolation, through production orchestration: never the
        account-bound tool, always this tenant's own file."""
        self.recording("iso")

        self.run_pass()

        self.assertEqual([name for name, _ in self.asr_calls], ["transcribe_url"])
        self.assertNotIn("file_id", self.asr_calls[0][1])


class ConnectorLogRedactionTests(unittest.TestCase):
    """Container logs and cron mail are not a private channel.

    Everything this process logs is readable by whoever can read the container
    logs, and a stage failure carries whatever string the failing library chose
    — routinely including the archived audio path, and with it the tenant's
    identity and the host's layout.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.archive = os.path.join(self.dir, "archive")
        self.audio = os.path.join(self.archive, "audio")
        os.makedirs(self.audio)
        self.asr_token = os.path.join(self.dir, "asr-token")
        Path(self.asr_token).write_text("asr-caller-secret")
        self.env = {"TENANT_ID": "tenant-alpha",
                    "TENANT_ARCHIVE_DIR": self.archive,
                    "PLAUD_MCP_TOKEN_FILE": write_caller_token(self.dir),
                    "ASR_MCP_URL": "http://127.0.0.1:62362/mcp",
                    "ASR_TOKEN_FILE": self.asr_token,
                    "RUN_ASR": "1", "RUN_SUMMARIES": "1"}
        self.connector = load_module(
            "connector.py", f"connector_redact_{id(self)}", self.env)

    def test_an_audio_path_never_reaches_the_log(self):
        detail = self.connector.safe_detail(
            RuntimeError(f"decode failed at {self.audio}/r.mp3"))

        self.assertNotIn(self.audio, detail)
        self.assertIn("decode failed", detail)

    def test_a_file_uri_never_reaches_the_log(self):
        detail = self.connector.safe_detail(
            f"download failed for file://{self.audio}/r.mp3")

        self.assertNotIn("file://", detail)
        self.assertNotIn(self.audio, detail)

    def test_the_archive_and_token_paths_are_redacted(self):
        detail = self.connector.safe_detail(
            f"cannot open {self.archive}/archive.db using {self.asr_token}")

        self.assertNotIn(self.archive, detail)
        self.assertNotIn(self.asr_token, detail)

    def test_a_caller_token_value_never_reaches_the_log(self):
        """A library that echoes the Authorization header it was handed is the
        realistic way a secret ends up in a log line."""
        detail = self.connector.safe_detail(
            "401 rejected: Authorization: Bearer asr-caller-secret")

        self.assertNotIn("asr-caller-secret", detail)

    def test_stage_failures_go_through_it(self):
        """Not just the helper: the paths that actually log a caught error."""
        lines = []
        with mock.patch.object(self.connector, "log", lines.append), \
                mock.patch.object(
                    self.connector, "drain_pipeline",
                    side_effect=RuntimeError(f"boom at {self.audio}/r.mp3")), \
                mock.patch.object(self.connector, "sync_once",
                                  return_value={"seen": 0, "new": 0,
                                                "archived": 0, "failed": 0}), \
                mock.patch.object(self.connector, "run_asr"), \
                mock.patch.object(self.connector, "run_summaries"):
            self.connector.pass_once()

        joined = "\n".join(lines)
        self.assertIn("pipeline pass failed", joined)
        self.assertNotIn(self.audio, joined)


class ConnectorStageDisablingTests(unittest.TestCase):
    """A required stage cannot be switched off quietly.

    RUN_ASR=0 used to mean "this container archives but never transcribes",
    while the healthcheck went on reporting the tenant healthy: recordings piled
    up with no text and the one signal the deployment had never fired.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.archive = os.path.join(self.dir, "archive")
        os.makedirs(os.path.join(self.archive, "audio"))
        self.env = {"TENANT_ID": "tenant-alpha",
                    "TENANT_ARCHIVE_DIR": self.archive,
                    "PLAUD_MCP_TOKEN_FILE": write_caller_token(self.dir)}

    def load(self, **env):
        self.env.update(env)
        return load_module("connector.py", f"connector_gate_{id(self)}",
                           self.env)

    def test_production_refuses_to_start_with_asr_disabled(self):
        module = self.load(RUN_ASR="0")
        lines = []
        with mock.patch.object(module, "log", lines.append), \
                mock.patch.object(module.sys, "argv", ["connector.py", "loop"]):
            self.assertNotEqual(module.main(), 0)

        joined = "\n".join(lines)
        self.assertIn("RUN_ASR", joined)
        self.assertIn("refusing", joined.lower())

    def test_production_refuses_to_start_with_summaries_disabled(self):
        module = self.load(RUN_SUMMARIES="0")
        with mock.patch.object(module, "log", lambda _m: None), \
                mock.patch.object(module.sys, "argv", ["connector.py", "once"]):
            self.assertNotEqual(module.main(), 0)

    def test_the_healthcheck_still_answers_with_a_stage_disabled(self):
        """Refusing to run must not also blind the operator: `status` has to
        keep working and say what is wrong."""
        module = self.load(RUN_ASR="0")
        module.ensure_layout()

        out = module.status()

        self.assertFalse(out["ok"])
        self.assertIn("RUN_ASR", " ".join(out["stages"]["disabled"]))


class ConnectorQueueHealthTests(unittest.TestCase):
    """Health reports the queue, because the queue is where work now lives."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.archive = os.path.join(self.dir, "archive")
        os.makedirs(os.path.join(self.archive, "audio"))
        self.env = {"TENANT_ID": "tenant-alpha",
                    "TENANT_ARCHIVE_DIR": self.archive,
                    "PLAUD_MCP_TOKEN_FILE": write_caller_token(self.dir)}
        self.connector = load_module(
            "connector.py", f"connector_health_{id(self)}", self.env)
        self.connector.ensure_layout()
        self.db = sqlite3.connect(os.path.join(self.archive, "archive.db"))
        self.addCleanup(self.db.close)

    def job(self, rid, stage, state, attempts=0):
        self.db.execute(
            """INSERT INTO pipeline_jobs(recording_id,stage,state,attempts,
                 enqueued_epoch,available_epoch) VALUES(?,?,?,?,0,0)""",
            (rid, stage, state, attempts))
        self.db.commit()

    def test_an_idle_archive_is_healthy(self):
        out = self.connector.status()

        self.assertTrue(out["ok"])
        self.assertEqual(out["pipeline"]["queued"], 0)
        self.assertEqual(out["pipeline"]["failed"], 0)

    def test_queued_and_in_flight_work_is_reported_without_failing_health(self):
        """Work in the queue is the system doing its job, not a fault."""
        self.job("a", "asr", "queued")
        self.job("b", "summary", "processing")

        out = self.connector.status()

        self.assertTrue(out["ok"])
        self.assertEqual(out["pipeline"]["queued"], 1)
        self.assertEqual(out["pipeline"]["processing"], 1)

    def test_a_stage_that_exhausted_its_retries_makes_the_tenant_unhealthy(self):
        """A failed job is work that will never be retried by anything. That is
        precisely the state an operator has to be told about."""
        self.job("c", "asr", "failed", attempts=3)

        out = self.connector.status()

        self.assertFalse(out["ok"])
        self.assertEqual(out["pipeline"]["failed"], 1)

    def test_optional_diarization_failure_is_exposed_without_failing_required_health(self):
        self.job("d", "diarization", "failed", attempts=3)
        out = self.connector.status()
        self.assertTrue(out["ok"])
        self.assertEqual(out["pipeline"]["failed"], 0)
        self.assertEqual(out["pipeline"]["diarization"]["failed"], 1)

    def test_health_reports_that_a_stage_failed_never_what_it_said(self):
        """`status` is printed by a healthcheck and read by anyone who can run
        `docker inspect`. A stage diagnosis is whatever string the failing
        library chose — routinely the archived audio path, sometimes a chunk of
        the input — so counts travel and the text does not. (The archive
        directory itself is deliberately reported: it is not a secret and it is
        the first thing an operator needs.)"""
        self.job("d", "asr", "failed", attempts=3)
        self.db.execute(
            "UPDATE pipeline_jobs SET last_error=? WHERE recording_id='d'",
            (f"decode failed at {self.archive}/audio/d.mp3",))
        self.db.commit()

        out = self.connector.status()
        rendered = json.dumps(out)

        self.assertFalse(out["ok"])
        self.assertNotIn("decode failed", rendered)
        self.assertNotIn("/audio/d.mp3", rendered)
