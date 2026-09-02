"""Recordings MCP (recordings-mcp) — the read-only MCP a bot talks to.

This is NOT the PLAUD source MCP. It serves ONE tenant's processed archive,
read-only, over authenticated JSON-RPC, and the tests here are mostly about the
things that must never happen: a caller reaching another tenant's series, a
tool argument widening the process's scope, a filesystem path or a caller token
appearing in a response or a log line, an unbounded page.
"""
import io
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recordings_mcp import config as mcp_config  # noqa: E402
from recordings_mcp import grants as mcp_grants  # noqa: E402
from recordings_mcp import server as mcp_server  # noqa: E402
from recordings_mcp import store as mcp_store  # noqa: E402

TOKEN = "caller-token-for-tests-with-at-least-32-bytes-0123456789"
OTHER_TOKEN = "another-caller-token-with-at-least-32-bytes-0123456789"


def build_archive(directory, tenant_prefix="N", rows=None):
    """A small archive.db shaped exactly like the real one."""
    os.makedirs(os.path.join(directory, "audio"), exist_ok=True)
    path = os.path.join(directory, "archive.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE recordings(
          id TEXT PRIMARY KEY, name TEXT, start_at TEXT, created_at TEXT,
          duration_ms INTEGER, lang TEXT, asr_engine TEXT, asr_transcript TEXT,
          plaud_transcript TEXT, summary TEXT, audio_path TEXT, archived_at TEXT,
          plaud_meta_json TEXT, summary_json TEXT, semantic_title TEXT, asr_meta_json TEXT,
          asr_alternative_transcript TEXT, recording_number TEXT);
        CREATE VIRTUAL TABLE recordings_fts USING fts5(
          id UNINDEXED, name, transcript);
        """
    )
    rows = rows if rows is not None else default_rows(tenant_prefix)
    for row in rows:
        columns = ",".join(row)
        placeholders = ",".join("?" for _ in row)
        conn.execute(
            f"INSERT INTO recordings({columns}) VALUES({placeholders})",
            tuple(row.values()),
        )
        conn.execute(
            "INSERT INTO recordings_fts(id,name,transcript) VALUES(?,?,?)",
            (row["id"], row.get("name", ""),
             row.get("asr_transcript") or row.get("plaud_transcript") or ""),
        )
    conn.commit()
    conn.close()
    return path


def default_rows(prefix="N"):
    return [
        {
            "id": "file-aaa",
            "name": "Планёрка команды",
            "start_at": "2026-01-05 10:00:00",
            "duration_ms": 1800000,
            "lang": "ru",
            "asr_engine": "host-whisper",
            "asr_transcript": "Обсудили бюджет и сроки поставки оборудования.",
            "plaud_transcript": "плауд версия расшифровки",
            "asr_alternative_transcript": "КАНДИДАТ-ГИПОТЕЗА-НЕ-ПОКАЗЫВАТЬ",
            "summary": "## Итог\nКоманда согласовала бюджет.",
            "summary_json": json.dumps(
                {"overview": "Согласован бюджет",
                 "themes": [{"title": "Бюджет", "summary": "Утверждён на квартал"}],
                 "decisions": ["Закупить оборудование"],
                 "action_items": [{"task": "Подписать договор", "owner": "Айбеке"}]},
                ensure_ascii=False),
            "asr_meta_json": json.dumps(
                {"selected_engine": "host-whisper", "requested_engine": "auto",
                 "route_reason": "detected Russian speech", "language": "ru",
                 "internal_debug": "секретное поле маршрутизатора"}),
            "audio_path": "/archive/audio/file-aaa.mp3",
            "plaud_meta_json": json.dumps({"presigned_hint": "https://plaud/secret"}),
            "recording_number": f"{prefix}-0001",
            "semantic_title": "Бюджет и сроки поставки",
        },
        {
            "id": "file-bbb",
            "name": "Звонок с поставщиком",
            "start_at": "2026-02-11 15:30:00",
            "duration_ms": 600000,
            "lang": "ru",
            "plaud_transcript": "Поставщик подтвердил отгрузку оборудования.",
            "summary": "Поставка подтверждена.",
            "recording_number": f"{prefix}-0002",
        },
        {
            "id": "file-ccc",
            "name": "Короткая заметка",
            "start_at": "2026-03-02 08:00:00",
            "duration_ms": 20000,
            "recording_number": f"{prefix}-0003",
        },
    ]


def make_config(directory, tenant="tenant-alpha", **over):
    env = {
        "TENANT_ID": tenant,
        "RECORDINGS_MCP_CODE_PREFIX": over.pop("RECORDINGS_MCP_CODE_PREFIX", "N"),
        "RECORDINGS_MCP_ARCHIVE_DIR": directory,
        "RECORDINGS_MCP_GRANT_STATE": os.path.join(tempfile.mkdtemp(), "recordings-mcp-test-state.db"),
    }
    env.update(over)
    return mcp_config.load(env)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        build_archive(self.dir, "A")

    def test_a_valid_single_tenant_configuration_loads(self):
        cfg = make_config(self.dir, RECORDINGS_MCP_CODE_PREFIX="A")
        self.assertEqual(cfg.tenant_id, "tenant-alpha")
        self.assertEqual(cfg.code_prefix, "A")
        self.assertEqual(cfg.db_path, os.path.join(self.dir, "archive.db"))

    def test_any_explicit_tenant_id_is_supported(self):
        self.assertEqual(make_config(self.dir, tenant="customer-42", RECORDINGS_MCP_CODE_PREFIX="A").tenant_id,
                         "customer-42")

    def test_missing_tenant_fails_closed(self):
        with self.assertRaises(mcp_config.ConfigError):
            mcp_config.load({"RECORDINGS_MCP_CODE_PREFIX": "A",
                             "RECORDINGS_MCP_ARCHIVE_DIR": self.dir})

    def test_missing_code_prefix_fails_closed(self):
        with self.assertRaises(mcp_config.ConfigError):
            make_config(self.dir, RECORDINGS_MCP_CODE_PREFIX="")

    def test_invalid_code_prefix_fails_closed(self):
        with self.assertRaises(mcp_config.ConfigError):
            make_config(self.dir, RECORDINGS_MCP_CODE_PREFIX="../A")

    def test_missing_grant_state_fails_closed(self):
        with self.assertRaises(mcp_config.ConfigError):
            mcp_config.load({"RECORDINGS_MCP_TENANT": "owner",
                             "RECORDINGS_MCP_ARCHIVE_DIR": self.dir})

    def test_missing_archive_directory_fails_closed(self):
        with self.assertRaises(mcp_config.ConfigError):
            mcp_config.load({"RECORDINGS_MCP_TENANT": "owner",
                             "RECORDINGS_MCP_GRANT_STATE": "/tmp/state.db"})

    def test_legacy_token_configuration_is_not_an_authority_source(self):
        cfg = make_config(self.dir, RECORDINGS_MCP_TOKEN=TOKEN,
                          RECORDINGS_MCP_TOKEN_FILE="/secrets/token",
                          RECORDINGS_MCP_TOKEN_SHA256="0" * 64)
        self.assertFalse(hasattr(cfg, "token_matches"))
        self.assertNotIn("token", repr(cfg).lower())

    def test_grant_state_must_be_separate_and_writable(self):
        with self.assertRaises(mcp_config.ConfigError):
            make_config(self.dir, RECORDINGS_MCP_GRANT_STATE=os.path.join(self.dir, "state.db"))

    def test_the_configuration_never_reprs_a_caller_token(self):
        cfg = make_config(self.dir)
        for text in (repr(cfg), str(cfg), cfg.describe()):
            self.assertNotIn(TOKEN, text)


class ServerTestCase(unittest.TestCase):
    """A live recordings-mcp on a loopback port, one tenant, one token."""

    tenant = "owner"
    prefix = "N"

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        build_archive(self.dir, self.prefix)
        self.config = make_config(self.dir, tenant=self.tenant)
        mcp_grants.GrantRegistry(self.config.grant_state_path).create(
            caller_id="test-bot", token=TOKEN,
            scopes=["metadata", "transcript", "summary", "mindmap"], recordings=["*"])
        self.httpd = mcp_server.make_server(self.config, host="127.0.0.1", port=0)
        self.port = self.httpd.server_address[1]
        # A short poll interval only shortens shutdown(); the default half a
        # second per test case adds up to most of this file's runtime.
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def url(self, path="/mcp"):
        return f"http://127.0.0.1:{self.port}{path}"

    def raw(self, path="/mcp", body=None, headers=None, method="POST"):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.url(path), data=data, method=method,
            headers=headers if headers is not None else self.auth_headers())
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def auth_headers(self, token=TOKEN):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def rpc(self, method, params=None, token=TOKEN, request_id=1):
        body = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            body["params"] = params
        status, text = self.raw(body=body, headers=self.auth_headers(token))
        return status, (json.loads(text) if text.strip() else {})

    def call_tool(self, name, arguments=None, token=TOKEN):
        return self.rpc("tools/call",
                        {"name": name, "arguments": arguments or {}}, token=token)

    def tool_result(self, name, arguments=None):
        status, payload = self.call_tool(name, arguments)
        self.assertEqual(status, 200, payload)
        self.assertNotIn("error", payload, payload)
        return payload["result"]["structuredContent"]

    def tool_error(self, name, arguments=None):
        status, payload = self.call_tool(name, arguments)
        self.assertEqual(status, 200, payload)
        self.assertIn("error", payload, payload)
        return payload["error"]


class AuthTests(ServerTestCase):
    def test_a_correct_bearer_token_is_accepted(self):
        status, payload = self.rpc("tools/list")
        self.assertEqual(status, 200)
        self.assertIn("result", payload)

    def test_a_missing_authorization_header_is_refused(self):
        status, payload = self.rpc("tools/list", token=None)
        self.assertEqual(status, 401)
        self.assertNotIn("result", payload)

    def test_a_malformed_authorization_header_is_refused(self):
        for header in ("", "Bearer", "Basic " + TOKEN, TOKEN, "Bearer  ",
                       "Bearer\t" + TOKEN):
            status, _ = self.raw(
                body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Content-Type": "application/json", "Authorization": header})
            self.assertEqual(status, 401, header)

    def test_a_wrong_token_is_refused(self):
        for wrong in (OTHER_TOKEN, TOKEN + "x", TOKEN[:-1], TOKEN.upper(), ""):
            status, _ = self.rpc("tools/list", token=wrong)
            self.assertEqual(status, 401, wrong)

    def test_early_unauthorized_response_closes_keepalive_with_unread_body(self):
        # A body left unread before a 401 must not be parsed as a second request.
        wire = (f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                "Content-Length: 32\r\nConnection: keep-alive\r\n\r\n"
                "x" * 32).encode()
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(wire)
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            response = b"".join(chunks).decode("iso-8859-1")
            self.assertIn("401", response)
            self.assertIn("Connection: close", response)

    def test_credentials_in_the_url_are_refused_outright(self):
        status, _ = self.raw(
            path=f"/mcp?token={TOKEN}",
            body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(status, 401)

    def test_grant_digest_comparison_is_constant_time(self):
        # The registry compares fixed-size persisted hashes; Config deliberately
        # carries no static credential or digest.
        self.assertEqual(mcp_grants.hash_token(TOKEN),
                         mcp_grants.hash_token(TOKEN))
        self.assertNotEqual(mcp_grants.hash_token(TOKEN),
                            mcp_grants.hash_token(TOKEN[:-1] + "Z"))
        source = Path(mcp_grants.__file__).read_text(encoding="utf-8")
        self.assertIn("compare_digest", source)
        self.assertNotIn("token ==", source)

    def test_no_token_is_ever_written_to_the_log(self):
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            self.rpc("tools/list")
            self.rpc("tools/list", token=OTHER_TOKEN)
            self.raw(path=f"/mcp?token={TOKEN}",
                     body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        logged = buffer.getvalue()
        self.assertNotIn(TOKEN, logged)
        self.assertNotIn(OTHER_TOKEN, logged)
        self.assertNotIn("token=", logged)

    def test_health_is_reachable_without_credentials_and_leaks_nothing(self):
        status, text = self.raw(path="/healthz", method="GET", headers={})
        self.assertEqual(status, 200)
        payload = json.loads(text)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload.get("service"), "recordings-mcp")
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(self.dir, blob)
        self.assertNotIn("Планёрка", blob)

    def test_readiness_reports_the_archive_without_exposing_it(self):
        status, text = self.raw(path="/readyz", method="GET", headers={})
        self.assertEqual(status, 200)
        payload = json.loads(text)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["tenant"], self.tenant)
        self.assertEqual(payload["recordings"], 3)
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(self.dir, blob)
        self.assertNotIn("archive.db", blob)

    def test_readiness_fails_closed_when_grant_audit_state_is_corrupt(self):
        with open(self.config.grant_state_path, "wb") as handle:
            handle.write(b"not sqlite")
        status, text = self.raw(path="/readyz", method="GET", headers={})
        self.assertEqual(status, 503)
        self.assertFalse(json.loads(text)["ready"])


class GrantToolScopeTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        mcp_grants.GrantRegistry(self.config.grant_state_path).create(
            caller_id="limited-bot", token=OTHER_TOKEN, scopes=["metadata"],
            recordings=["N-0001"])

    def test_allowlist_and_scopes_apply_to_every_read_tool(self):
        status, payload = self.rpc("tools/call", {
            "name": "recordings_list", "arguments": {}}, token=OTHER_TOKEN)
        self.assertEqual(status, 200)
        items = payload["result"]["structuredContent"]["items"]
        self.assertEqual([item["number"] for item in items], ["N-0001"])

        status, payload = self.rpc("tools/call", {
            "name": "recordings_search", "arguments": {"query": "оборудования"}},
            token=OTHER_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["message"], "request is not authorized")

        status, payload = self.rpc("tools/call", {
            "name": "recordings_hybrid_search",
            "arguments": {"query": "обслуживание оборудования"}},
            token=OTHER_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["message"], "request is not authorized")

        detail = self.rpc("tools/call", {"name": "recording_get",
                         "arguments": {"number": "N-0001"}}, token=OTHER_TOKEN)[1]
        result = detail["result"]["structuredContent"]
        self.assertEqual(set(result), {
            "number", "name", "title", "source_name", "started_at", "duration_ms", "language"})

        denied = self.rpc("tools/call", {"name": "recording_mindmap_get",
                           "arguments": {"number": "N-0001"}}, token=OTHER_TOKEN)[1]
        self.assertEqual(denied["error"]["code"], mcp_server.INVALID_PARAMS)

    def test_metadata_only_contract_has_no_summary_or_content_existence(self):
        listed = self.rpc("tools/call", {"name": "recordings_list", "arguments": {}},
                          token=OTHER_TOKEN)[1]["result"]["structuredContent"]
        self.assertEqual([item["number"] for item in listed["items"]], ["N-0001"])
        self.assertEqual(set(listed["items"][0]), {
            "number", "name", "title", "source_name", "started_at", "duration_ms", "language"})
        detail = self.rpc("tools/call", {"name": "recording_get",
                           "arguments": {"number": "N-0001"}}, token=OTHER_TOKEN)[1]
        result = detail["result"]["structuredContent"]
        self.assertEqual(set(result), {
            "number", "name", "title", "source_name", "started_at", "duration_ms", "language"})
        denied = self.rpc("tools/call", {
            "name": "recordings_search", "arguments": {"query": "оборудования"}},
            token=OTHER_TOKEN)[1]
        self.assertEqual(denied["error"]["message"], "request is not authorized")

    def test_content_scopes_only_expose_their_own_presence_data_and_filters(self):
        registry = mcp_grants.GrantRegistry(self.config.grant_state_path)
        summary_token = "summary-scope-token-with-at-least-32-bytes-012345"
        mindmap_token = "mindmap-scope-token-with-at-least-32-bytes-012345"
        transcript_token = "transcript-scope-token-with-at-least-32-bytes-"
        registry.create(caller_id="summary-bot", token=summary_token,
                        scopes=["metadata", "summary"], recordings=["*"])
        registry.create(caller_id="mindmap-bot", token=mindmap_token,
                        scopes=["metadata", "mindmap"], recordings=["*"])
        registry.create(caller_id="transcript-bot", token=transcript_token,
                        scopes=["metadata", "transcript"], recordings=["*"])

        contracts = (
            (OTHER_TOKEN, set(), set()),
            (summary_token, {"has_summary", "summary_preview"},
             {"transcript", "asr"}),
            (mindmap_token, set(), {"transcript", "asr", "summary", "has_mindmap"}),
            (transcript_token, {"has_transcript"}, {"summary", "has_mindmap"}),
        )
        for token, list_presence, absent_detail in contracts:
            listed = self.rpc("tools/call", {"name": "recordings_list", "arguments": {}},
                              token=token)[1]["result"]["structuredContent"]
            self.assertEqual(set(listed["items"][0]) - {
                "number", "name", "title", "source_name", "started_at", "duration_ms", "language"}, list_presence)
            detail = self.rpc("tools/call", {"name": "recording_get",
                              "arguments": {"number": "N-0001"}}, token=token)[1]
            result = detail["result"]["structuredContent"]
            for key in absent_detail:
                self.assertNotIn(key, result, (token, key))

        for token, forbidden in (
                (OTHER_TOKEN, ("has_summary", "has_transcript")),
                (summary_token, ("has_transcript",)),
                (mindmap_token, ("has_summary", "has_transcript")),
                (transcript_token, ("has_summary",))):
            for name in forbidden:
                for value in (True, False):
                    payload = self.rpc("tools/call", {
                        "name": "recordings_list", "arguments": {"filters": {name: value}}},
                        token=token)[1]
                    self.assertEqual(payload["error"]["code"], mcp_server.INVALID_PARAMS)
                    self.assertIn(name, payload["error"]["message"])

        for token, name, expected in (
                (summary_token, "has_summary", ["N-0002", "N-0001"]),
                (transcript_token, "has_transcript", ["N-0002", "N-0001"])):
            result = self.rpc("tools/call", {
                "name": "recordings_list", "arguments": {"filters": {name: True}}},
                token=token)[1]["result"]["structuredContent"]
            self.assertEqual([item["number"] for item in result["items"]], expected)

    def test_tools_list_does_not_advertise_privileged_presence_filters(self):
        payload = self.rpc("tools/list", token=OTHER_TOKEN)[1]
        listing = next(tool for tool in payload["result"]["tools"]
                       if tool["name"] == "recordings_list")
        filters = listing["inputSchema"]["properties"]["filters"]["properties"]
        self.assertNotIn("has_summary", filters)
        self.assertNotIn("has_transcript", filters)

    def test_allowlist_predicate_pages_visible_rows_without_dead_end(self):
        first = mcp_store.list_recordings(
            self.config, limit=1, allowed_numbers=frozenset({"N-0001", "N-0003"}))
        self.assertEqual([item["number"] for item in first["items"]], ["N-0003"])
        self.assertTrue(first["next_cursor"])
        second = mcp_store.list_recordings(
            self.config, limit=1, cursor=first["next_cursor"],
            allowed_numbers=frozenset({"N-0001", "N-0003"}))
        self.assertEqual([item["number"] for item in second["items"]], ["N-0001"])
        self.assertIsNone(second["next_cursor"])

    def test_forbidden_and_absent_codes_are_opaque_audited_without_archive_reads(self):
        handler = mcp_server.TOOLS["recording_get"]["handler"]
        with mock.patch.dict(mcp_server.TOOLS["recording_get"], {
                "handler": mock.Mock(side_effect=AssertionError("archive read"))}):
            forbidden = self.rpc("tools/call", {"name": "recording_get",
                                 "arguments": {"number": "N-0002"}},
                                 token=OTHER_TOKEN)[1]
            absent = self.rpc("tools/call", {"name": "recording_get",
                              "arguments": {"number": "N-0004"}},
                              token=OTHER_TOKEN)[1]
        self.assertEqual(forbidden["error"], absent["error"])
        with sqlite3.connect(self.config.grant_state_path) as conn:
            denied = conn.execute(
                "SELECT caller_id,tool,recording_number,outcome FROM audit_log "
                "WHERE outcome='denied' ORDER BY id DESC LIMIT 2").fetchall()
        self.assertEqual(denied, [("limited-bot", "recording_get", "N-0004", "denied"),
                                  ("limited-bot", "recording_get", "N-0002", "denied")])

    def test_initialize_announces_the_service_by_its_real_name(self):
        status, payload = self.rpc("initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(status, 200)
        info = payload["result"]["serverInfo"]
        self.assertEqual(info["name"], "recordings-mcp")
        self.assertIn("tools", payload["result"]["capabilities"])

    def test_tools_list_is_exactly_the_read_only_surface(self):
        _, payload = self.rpc("tools/list")
        names = sorted(tool["name"] for tool in payload["result"]["tools"])
        self.assertEqual(names, [
            "recording_get", "recording_mindmap_get",
            "recordings_hybrid_search", "recordings_list", "recordings_search",
        ])

    def test_no_audio_tool_is_exposed(self):
        _, payload = self.rpc("tools/list")
        blob = json.dumps(payload).lower()
        self.assertNotIn("audio_get", blob)
        self.assertNotIn("audio_download", blob)
        status, error = self.call_tool("recording_audio_get", {"number": 1})
        self.assertEqual(status, 200)
        self.assertEqual(error["error"]["code"], mcp_server.METHOD_NOT_FOUND)

    def test_an_unknown_tool_is_a_clean_error(self):
        error = self.tool_error("recordings_delete", {})
        self.assertEqual(error["code"], mcp_server.METHOD_NOT_FOUND)

    def test_an_unknown_method_is_a_clean_error(self):
        _, payload = self.rpc("recordings/wipe")
        self.assertEqual(payload["error"]["code"], mcp_server.METHOD_NOT_FOUND)

    def test_malformed_json_is_a_parse_error_not_a_traceback(self):
        status, text = self.raw(path="/mcp", body=None, headers=self.auth_headers())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text)["error"]["code"], mcp_server.PARSE_ERROR)

    def test_the_initialized_notification_is_accepted(self):
        status, text = self.raw(
            body={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=self.auth_headers())
        self.assertIn(status, (200, 202))
        self.assertEqual(text.strip(), "")


class ListingTests(ServerTestCase):
    def test_listing_returns_permanent_numbers_newest_first(self):
        result = self.tool_result("recordings_list")
        numbers = [item["number"] for item in result["items"]]
        self.assertEqual(numbers, ["N-0003", "N-0002", "N-0001"])
        self.assertIsNone(result["next_cursor"])

    def test_pagination_is_bounded_and_walks_the_whole_archive(self):
        first = self.tool_result("recordings_list", {"limit": 2})
        self.assertEqual([i["number"] for i in first["items"]], ["N-0003", "N-0002"])
        self.assertTrue(first["next_cursor"])
        second = self.tool_result(
            "recordings_list", {"limit": 2, "cursor": first["next_cursor"]})
        self.assertEqual([i["number"] for i in second["items"]], ["N-0001"])
        self.assertIsNone(second["next_cursor"])

    def test_an_absurd_limit_is_clamped_rather_than_honoured(self):
        result = self.tool_result("recordings_list", {"limit": 100000})
        self.assertLessEqual(result["limit"], mcp_store.MAX_LIMIT)
        self.assertEqual(len(result["items"]), 3)

    def test_a_nonsense_limit_is_rejected(self):
        for bad in ("many", -1, 0, 2.5, None, [10]):
            error = self.tool_error("recordings_list", {"limit": bad})
            self.assertEqual(error["code"], mcp_server.INVALID_PARAMS, bad)

    def test_a_forged_cursor_is_rejected_without_explaining_the_scheme(self):
        for bad in ("../../etc/passwd", "9999999999999", "!!", "o:1; DROP TABLE"):
            error = self.tool_error("recordings_list", {"cursor": bad})
            self.assertEqual(error["code"], mcp_server.INVALID_PARAMS, bad)
            self.assertNotIn("offset", error["message"].lower())

    def test_filters_narrow_the_page(self):
        result = self.tool_result(
            "recordings_list", {"filters": {"date_from": "2026-02-01"}})
        self.assertEqual([i["number"] for i in result["items"]], ["N-0003", "N-0002"])
        result = self.tool_result(
            "recordings_list",
            {"filters": {"date_to": "2026-02-28", "has_summary": True}})
        self.assertEqual([i["number"] for i in result["items"]], ["N-0002", "N-0001"])

    def test_store_refuses_presence_filters_before_an_archive_query(self):
        with mock.patch.object(mcp_store, "open_db", side_effect=AssertionError("archive query")):
            for value in (True, False):
                with self.assertRaises(mcp_store.InvalidArgument) as caught:
                    mcp_store.list_recordings(
                        self.config, filters={"has_transcript": value},
                        allowed_filters=mcp_store.METADATA_FILTERS)
                self.assertIn("has_transcript", str(caught.exception))

    def test_an_unknown_filter_is_refused_rather_than_ignored(self):
        error = self.tool_error("recordings_list", {"filters": {"sql": "1=1"}})
        self.assertEqual(error["code"], mcp_server.INVALID_PARAMS)

    def test_listing_uses_semantic_title_without_losing_source_name(self):
        item = next(item for item in self.tool_result("recordings_list")["items"]
                    if item["number"] == "N-0001")
        self.assertEqual(item["title"], "Бюджет и сроки поставки")
        self.assertEqual(item["source_name"], "Планёрка команды")

    def test_listing_never_carries_internal_identifiers_or_paths(self):
        result = self.tool_result("recordings_list")
        blob = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("file-aaa", blob)
        self.assertNotIn("/home/", blob)
        self.assertNotIn("audio_path", blob)
        self.assertNotIn("КАНДИДАТ", blob)


class SearchTests(ServerTestCase):
    def test_search_finds_by_transcript_and_returns_numbers(self):
        result = self.tool_result("recordings_search", {"query": "оборудования"})
        numbers = sorted(item["number"] for item in result["items"])
        self.assertEqual(numbers, ["N-0001", "N-0002"])
        self.assertTrue(all(item.get("snippet") for item in result["items"]))

    def test_search_paginates_within_bounds(self):
        first = self.tool_result(
            "recordings_search", {"query": "оборудования", "limit": 1})
        self.assertEqual(len(first["items"]), 1)
        self.assertTrue(first["next_cursor"])
        second = self.tool_result(
            "recordings_search",
            {"query": "оборудования", "limit": 1, "cursor": first["next_cursor"]})
        self.assertEqual(len(second["items"]), 1)
        self.assertNotEqual(first["items"][0]["number"], second["items"][0]["number"])

    def test_an_empty_query_is_rejected(self):
        for bad in ("", "   ", None, 5):
            error = self.tool_error("recordings_search", {"query": bad})
            self.assertEqual(error["code"], mcp_server.INVALID_PARAMS, bad)

    def test_fts_syntax_in_a_query_cannot_break_the_search(self):
        # A query is words, not an FTS expression: punctuation must come back
        # as "no results", never as a database error.
        for hostile in ('" OR 1=1 --', "оборудования*", "NEAR(", "^%$#"):
            status, payload = self.call_tool(
                "recordings_search", {"query": hostile})
            self.assertEqual(status, 200, hostile)
            self.assertIn("result", payload, hostile)

    def test_search_results_never_include_the_candidate_transcript(self):
        result = self.tool_result("recordings_search", {"query": "бюджет"})
        self.assertNotIn("КАНДИДАТ", json.dumps(result, ensure_ascii=False))


class RecordingGetTests(ServerTestCase):
    def test_get_by_canonical_code_in_any_case(self):
        for number in ("N-0001", "n-0001", "  N-0001  ", "N-1"):
            result = self.tool_result("recording_get", {"number": number})
            self.assertEqual(result["number"], "N-0001")
            self.assertEqual(result["name"], "Планёрка команды")

    def test_get_by_numeric_shorthand_in_this_single_tenant_scope(self):
        for number in (1, "1", "0001"):
            result = self.tool_result("recording_get", {"number": number})
            self.assertEqual(result["number"], "N-0001")

    def test_the_preferred_transcript_is_asr_with_plaud_as_the_fallback(self):
        first = self.tool_result("recording_get", {"number": 1})
        self.assertEqual(first["transcript"]["source"], "asr")
        self.assertIn("бюджет", first["transcript"]["text"])
        second = self.tool_result("recording_get", {"number": 2})
        self.assertEqual(second["transcript"]["source"], "plaud")
        self.assertIn("отгрузку", second["transcript"]["text"])

    def test_the_russian_summary_and_safe_asr_status_come_back(self):
        result = self.tool_result("recording_get", {"number": 1})
        self.assertIn("бюджет", result["summary"]["text"].lower())
        self.assertEqual(result["summary"]["structured"]["overview"],
                         "Согласован бюджет")
        asr = result["asr"]
        self.assertEqual(asr["engine"], "host-whisper")
        self.assertEqual(asr["route_reason"], "detected Russian speech")
        self.assertEqual(asr["status"], "ready")
        # Only the whitelisted routing fields; the router's own debug field is
        # not ours to hand to a bot.
        self.assertNotIn("internal_debug", json.dumps(asr, ensure_ascii=False))

    def test_a_recording_still_being_transcribed_reports_its_status(self):
        result = self.tool_result("recording_get", {"number": 3})
        self.assertIsNone(result["transcript"]["text"])
        self.assertEqual(result["transcript"]["source"], None)
        self.assertEqual(result["asr"]["status"], "pending")

    def test_get_never_leaks_paths_ids_candidates_or_plaud_metadata(self):
        result = self.tool_result("recording_get", {"number": 1})
        blob = json.dumps(result, ensure_ascii=False)
        for forbidden in ("file-aaa", "/home/", "audio_path", "КАНДИДАТ",
                          "presigned", "archive.db", self.dir):
            self.assertNotIn(forbidden, blob, forbidden)

    def test_a_missing_number_is_a_clear_not_found(self):
        error = self.tool_error("recording_get", {"number": 4242})
        self.assertEqual(error["code"], mcp_server.NOT_FOUND)
        self.assertIn("N-4242", error["message"])
        for forbidden in ("SELECT", "sqlite", "/", "archive.db"):
            self.assertNotIn(forbidden, error["message"], forbidden)

    def test_an_invalid_code_is_a_clear_invalid_params(self):
        for bad in ("N-", "abc", "", "N-0001-2", None, {"n": 1}):
            error = self.tool_error("recording_get", {"number": bad})
            self.assertEqual(error["code"], mcp_server.INVALID_PARAMS, bad)
            self.assertNotIn("SELECT", error["message"])

    def test_a_missing_argument_is_invalid_params(self):
        error = self.tool_error("recording_get", {})
        self.assertEqual(error["code"], mcp_server.INVALID_PARAMS)

    def test_the_text_content_block_mirrors_the_structured_result(self):
        status, payload = self.call_tool("recording_get", {"number": 1})
        self.assertEqual(status, 200)
        content = payload["result"]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(json.loads(content[0]["text"]),
                         payload["result"]["structuredContent"])


class MindmapTests(ServerTestCase):
    def test_mindmap_returns_structured_data_not_a_file(self):
        result = self.tool_result("recording_mindmap_get", {"number": 1})
        self.assertEqual(result["number"], "N-0001")
        self.assertEqual(result["mindmap"]["root"], "Планёрка команды")
        titles = [branch["title"] for branch in result["mindmap"]["branches"]]
        self.assertIn("Бюджет", titles)
        blob = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(".png", blob)
        self.assertNotIn("/cache/", blob)
        self.assertNotIn(self.dir, blob)

    def test_a_recording_without_structured_summary_says_so_plainly(self):
        result = self.tool_result("recording_mindmap_get", {"number": 3})
        self.assertIsNone(result["mindmap"])
        self.assertTrue(result["reason"])

    def test_mindmap_never_offers_a_viewer_url_without_a_real_stable_code_route(self):
        result = self.tool_result("recording_mindmap_get", {"number": 1})
        self.assertIsNone(result["app_url"])
        self.assertFalse(hasattr(self.config, "viewer_url"))


class TenantScopeTests(ServerTestCase):
    tenant = "owner"
    prefix = "N"

    def test_this_process_serves_exactly_one_series(self):
        result = self.tool_result("recordings_list")
        self.assertEqual([i["number"] for i in result["items"]],
                         ["N-0003", "N-0002", "N-0001"])

    def test_another_tenants_code_is_refused_without_confirming_anything(self):
        for foreign in ("D-0001", "B-0001", "d-0001"):
            error = self.tool_error("recording_get", {"number": foreign})
            self.assertEqual(error["code"], mcp_server.INVALID_PARAMS, foreign)
            message = error["message"].lower()
            for leak in ("owner", "tenant-c", "private-marker", "found", "exists"):
                self.assertNotIn(leak, message, foreign)

    def test_a_tenant_argument_cannot_widen_the_scope(self):
        for arguments in ({"tenant": "owner"}, {"tenant_id": "owner"},
                          {"db_path": "/tmp/other/archive.db"},
                          {"archive_dir": "/tmp/other"},
                          {"prefix": "N"}, {"limit": 5, "tenant": "tenant-c"}):
            error = self.tool_error("recordings_list", arguments)
            self.assertEqual(error["code"], mcp_server.INVALID_PARAMS, arguments)

    def test_a_tenant_argument_cannot_widen_a_lookup_either(self):
        error = self.tool_error("recording_get", {"number": 1, "tenant": "owner"})
        self.assertEqual(error["code"], mcp_server.INVALID_PARAMS)
        # ...and the scope is unchanged afterwards.
        self.assertEqual(
            self.tool_result("recording_get", {"number": 1})["number"], "N-0001")

    def test_the_server_reads_only_the_owner_archive_series(self):
        self.assertEqual(self.config.db_path, os.path.join(self.dir, "archive.db"))
        self.assertEqual(self.config.code_prefix, "N")


class ReadOnlyTests(ServerTestCase):
    def test_the_server_cannot_write_to_the_archive(self):
        with mcp_store.open_db(self.config) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM recordings")

    def test_closed_writer_readonly_directory_uses_safe_immutable_fallback(self):
        # Closing the writer checkpoints/removes its WAL. With no live WAL
        # state to lose, immutable=1 is safe when the mount cannot create shm.
        directory = tempfile.mkdtemp()
        build_archive(directory)
        conn = sqlite3.connect(os.path.join(directory, "archive.db"))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO recordings(id,name,recording_number) "
                     "VALUES('file-ddd','Ещё запись','N-0004')")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        self.assertFalse(os.path.exists(os.path.join(directory, "archive.db-wal")))
        os.chmod(directory, 0o555)
        self.addCleanup(os.chmod, directory, 0o755)
        config = make_config(directory)
        with mcp_store.open_db(config) as conn:
            count = conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]
        self.assertEqual(count, 4)

    def test_failed_primary_open_never_discards_a_live_wal(self):
        directory = tempfile.mkdtemp()
        build_archive(directory)
        db = os.path.join(directory, "archive.db")
        Path(db + "-wal").write_bytes(b"live-wal-marker")
        with mock.patch.object(mcp_store, "_open_readonly", side_effect=sqlite3.OperationalError):
            with self.assertRaises(mcp_store.Unavailable):
                mcp_store._connect(db)


class EntryPointTests(unittest.TestCase):
    def test_the_server_module_is_executable(self):
        path = ROOT / "recordings_mcp" / "server.py"
        self.assertTrue(os.access(path, os.X_OK), "server.py must be executable")
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        self.assertTrue(first_line.startswith("#!"), first_line)

    def test_a_bad_configuration_exits_nonzero_without_a_traceback(self):
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stderr(buffer):
            with self.assertRaises(SystemExit) as caught:
                mcp_server.main([])
        self.assertNotEqual(caught.exception.code, 0)
        self.assertNotIn("Traceback", buffer.getvalue())

    def test_the_product_is_never_called_archive_mcp(self):
        # The service is Recordings MCP / recordings-mcp. The old working name
        # must not survive anywhere a human or a bot can read it.
        # This file is excluded from its own scan for the obvious reason: the
        # banned strings have to appear here to be searched for at all.
        offenders = []
        for path in list((ROOT / "recordings_mcp").rglob("*")) + [
                ROOT / "README.md",
                ROOT / "archive" / "recording_codes.py",
                ROOT / "archive" / "archive_recording.py",
                ROOT / "viewer" / "app" / "static" / "app.js",
                ROOT / "viewer" / "app" / "main.py"]:
            if not path.is_file() or path.suffix == ".pyc":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            if "archive mcp" in text or "archive_mcp" in text or "archive-mcp" in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])


class RealArchiveSchemaTests(ServerTestCase):
    """The reader against a database the real ingest code produced.

    Everything else in this file builds the schema by hand, which cannot catch
    the two sides drifting apart. Here the archive is created by
    archive_recording.init_db() and numbered by the allocator the ingest pass
    uses, then read back through the MCP tools.
    """

    def setUp(self):
        super().setUp()
        self.live_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.live_dir, "audio"), exist_ok=True)
        sys.path.insert(0, str(ROOT / "archive"))
        import importlib

        archive = importlib.import_module("archive_recording")
        codes = importlib.import_module("recording_codes")
        conn = sqlite3.connect(os.path.join(self.live_dir, "archive.db"))
        archive.init_db(conn)
        conn.execute(
            "INSERT INTO recordings(id,name,start_at,duration_ms,asr_transcript) "
            "VALUES('file-real','Реальная запись','2026-05-01 12:00:00',60000,"
            "'расшифровка про бюджет')")
        conn.execute("INSERT INTO recordings_fts(id,name,transcript) "
                     "VALUES('file-real','Реальная запись','расшифровка про бюджет')")
        conn.commit()
        codes.allocate_number(conn, "N", "file-real")
        conn.close()
        self.live_config = make_config(self.live_dir)

    def test_the_reader_understands_what_the_writer_produced(self):
        listed = mcp_store.list_recordings(self.live_config)
        self.assertEqual([i["number"] for i in listed["items"]], ["N-0001"])
        detail = mcp_store.get_recording(self.live_config, 1)
        self.assertEqual(detail["name"], "Реальная запись")
        self.assertEqual(detail["transcript"]["source"], "asr")
        found = mcp_store.search_recordings(self.live_config, query="бюджет")
        self.assertEqual([i["number"] for i in found["items"]], ["N-0001"])
        self.assertEqual(mcp_store.counts(self.live_config)["recordings"], 1)


class UnnumberedArchiveTests(ServerTestCase):
    """An archive that predates numbering must say so, not fail obscurely."""

    def setUp(self):
        super().setUp()
        self.old_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.old_dir, "audio"), exist_ok=True)
        conn = sqlite3.connect(os.path.join(self.old_dir, "archive.db"))
        conn.executescript(
            "CREATE TABLE recordings(id TEXT PRIMARY KEY, name TEXT, "
            "start_at TEXT, created_at TEXT, duration_ms INTEGER, lang TEXT, "
            "asr_engine TEXT, asr_transcript TEXT, plaud_transcript TEXT, "
            "summary TEXT);"
            "CREATE VIRTUAL TABLE recordings_fts USING fts5("
            "  id UNINDEXED, name, transcript);")
        conn.execute("INSERT INTO recordings(id,name) VALUES('old','Старое')")
        conn.commit()
        conn.close()
        self.old_config = make_config(self.old_dir)

    def test_the_tools_report_an_unnumbered_archive_clearly(self):
        with self.assertRaises(mcp_store.Unavailable) as caught:
            mcp_store.list_recordings(self.old_config)
        message = str(caught.exception)
        self.assertIn("number", message.lower())
        for forbidden in ("SELECT", "sqlite", self.old_dir, "archive.db"):
            self.assertNotIn(forbidden, message, forbidden)

    def test_readiness_reports_not_ready_rather_than_crashing(self):
        httpd = mcp_server.make_server(self.old_config, host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever,
                                  kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/readyz", timeout=10) as response:
                    status, text = response.status, response.read().decode()
            except urllib.error.HTTPError as exc:
                status, text = exc.code, exc.read().decode()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
        self.assertEqual(status, 503)
        payload = json.loads(text)
        self.assertFalse(payload["ready"])
        self.assertNotIn(self.old_dir, json.dumps(payload))


class PackagingTests(unittest.TestCase):
    """The container and its documentation, checked the way deploy/ is."""

    def setUp(self):
        self.package = ROOT / "recordings_mcp"
        self.dockerfile = self.package / "Dockerfile"
        self.compose = self.package / "docker-compose.yml"
        self.env_example = self.package / "recordings-mcp.env.example"

    def read(self, path):
        self.assertTrue(path.exists(), f"{path.name} is missing")
        return path.read_text(encoding="utf-8")

    def directives(self, text):
        """The file's actual instructions, with comments dropped."""
        return "\n".join(line for line in text.splitlines()
                         if not line.strip().startswith("#"))

    def test_the_image_is_stdlib_only_and_unprivileged(self):
        text = self.read(self.dockerfile)
        self.assertNotIn("pip install", self.directives(text),
                         "recordings-mcp is standard library only")
        self.assertIn("USER ", text)
        # Shared archive modules own numbering and the durable readiness
        # projection. The image is broken if store.py imports either but the
        # Docker build omits it.
        self.assertIn("recording_codes.py", text)
        self.assertIn("pipeline.py", text)
        self.assertIn("readiness.py", text)
        self.assertIn("recordings_mcp.server", text)

    def test_the_image_healthcheck_uses_the_public_liveness_endpoint(self):
        text = self.read(self.dockerfile)
        self.assertIn("HEALTHCHECK", text)
        self.assertIn("/healthz", text)

    def test_compose_separates_readonly_archive_from_writable_hashed_state(self):
        text = self.read(self.compose)
        self.assertIn(":/archive:ro", text)
        self.assertIn(":/state:rw", text)
        self.assertIn("RECORDINGS_MCP_GRANT_STATE", text)
        # Caller credentials are not mounted or configured in the service:
        # only hashed grants/audit rows live in the separate writable state.
        self.assertNotIn(":/creds:ro", text)
        self.assertNotIn("RECORDINGS_MCP_TOKEN", text)
        directives = self.directives(text)
        for forbidden in ("privileged", "network_mode: host",
                          "/var/run/docker.sock"):
            self.assertNotIn(forbidden, directives, forbidden)

    def test_compose_is_generic_single_tenant_and_loopback_by_default(self):
        text = self.read(self.compose)
        self.assertIn("TENANT_ID: ${TENANT_ID:?", text)
        self.assertIn("RECORDINGS_MCP_CODE_PREFIX: ${RECORDINGS_MCP_CODE_PREFIX:?", text)
        self.assertIn("image: ${RECORDINGS_MCP_IMAGE:?RECORDINGS_MCP_IMAGE must be set}", text)
        self.assertIn('"${RECORDINGS_MCP_BIND_ADDR:-127.0.0.1}:${RECORDINGS_MCP_PUBLISH_PORT:-62390}:62390"', text)
        self.assertNotIn("container_name:", text)
        self.assertNotIn("RECORDINGS_MCP_OWNER_ARCHIVE_DIR", text)

    def test_the_env_example_carries_no_credential(self):
        text = self.read(self.env_example)
        self.assertIn("TENANT_ID=tenant-alpha", text)
        self.assertIn("RECORDINGS_MCP_ARCHIVE_DIR=", text)
        self.assertIn("RECORDINGS_MCP_STATE_DIR=", text)
        self.assertIn("RECORDINGS_MCP_CODE_PREFIX=A", text)
        for line in text.splitlines():
            if line.strip().startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            self.assertNotIn("TOKEN", name.upper(), line)
            self.assertLess(len(value.strip()), 80, line)

    def test_no_dotenv_file_was_added_to_the_repository(self):
        self.assertFalse((self.package / ".env").exists())
        self.assertFalse((ROOT / ".env").exists())

    def test_the_readme_documents_the_service_and_its_tools(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("recordings-mcp", text)
        self.assertIn("MCP записей", text)
        for tool in ("recordings_list", "recordings_search", "recording_get",
                     "recording_mindmap_get"):
            self.assertIn(tool, text, tool)
        self.assertIn("N-", text)  # the numbering scheme is documented


if __name__ == "__main__":
    unittest.main()
