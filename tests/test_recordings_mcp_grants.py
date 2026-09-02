"""Release-critical owner-only grants for recordings-mcp."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recordings_mcp import admin, grants


class GrantRegistryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = os.path.join(self.directory.name, "recordings-mcp-state.db")
        self.registry = grants.GrantRegistry(self.state)

    def test_create_stores_only_hash_and_audit_has_no_token_or_transcript(self):
        token = "T" * 48
        created = self.registry.create(caller_id="automation-bot", token=token,
                                       scopes=["metadata", "transcript"],
                                       recordings=["N-0001"])
        self.assertEqual(created["caller_id"], "automation-bot")
        with sqlite3.connect(self.state) as conn:
            raw = "\n".join(str(v) for row in conn.execute("SELECT * FROM grants") for v in row)
            self.assertNotIn(token, raw)
            self.assertIn(grants.hash_token(token), raw)
        grant = self.registry.authorize(token, "recording_get", "N-0001")
        self.assertEqual(grant.caller_id, "automation-bot")
        self.registry.audit(grant, "recording_get", "N-0001", "allowed", detail="transcript text must not persist")
        with sqlite3.connect(self.state) as conn:
            audit = "\n".join(str(v) for row in conn.execute("SELECT * FROM audit_log") for v in row)
        self.assertNotIn("transcript", audit.lower())
        self.assertNotIn(token, audit)

    def test_expired_revoked_and_allowlisted_grants_fail_closed(self):
        token = "E" * 48
        self.registry.create(caller_id="bot", token=token, scopes=["metadata"],
                             recordings=["N-0001"], expires_at="2000-01-01T00:00:00Z")
        with self.assertRaises(grants.AuthorizationError):
            self.registry.authorize(token, "recordings_list")
        live = "L" * 48
        self.registry.create(caller_id="bot-live", token=live, scopes=["metadata"],
                             recordings=["N-0001"])
        self.registry.authorize(live, "recording_get", "N-0001")
        with self.assertRaises(grants.AuthorizationError):
            self.registry.authorize(live, "recording_get", "N-0002")
        self.registry.revoke(caller_id="bot-live")
        with self.assertRaises(grants.AuthorizationError):
            self.registry.authorize(live, "recordings_list")

    def test_per_request_reload_observes_revocation_and_corrupt_state_denies(self):
        token = "R" * 48
        self.registry.create(caller_id="bot", token=token, scopes=["metadata"], recordings=["*"])
        self.registry.authorize(token, "recordings_list")
        second = grants.GrantRegistry(self.state)
        second.revoke(caller_id="bot")
        with self.assertRaises(grants.AuthorizationError):
            self.registry.authorize(token, "recordings_list")
        with open(self.state, "wb") as handle:
            handle.write(b"not sqlite")
        with self.assertRaises(grants.AuthorizationError):
            self.registry.authorize(token, "recordings_list")

    def test_denied_attempt_is_audited_and_audit_failure_denies(self):
        token = "D" * 48
        self.registry.create(caller_id="bot", token=token, scopes=["metadata"],
                             recordings=["N-0001"])
        with self.assertRaises(grants.AuthorizationError):
            self.registry.authorize(token, "recording_get", "N-0002")
        self.registry.audit_denied(token, "recording_get", "N-0002")
        conn = sqlite3.connect(self.state)
        try:
            row = conn.execute(
                "SELECT caller_id,tool,recording_number,outcome FROM audit_log "
                "WHERE outcome='denied'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ("bot", "recording_get", "N-0002", "denied"))
        with open(self.state, "wb") as handle:
            handle.write(b"not sqlite")
        with self.assertRaises(grants.AuthorizationError):
            self.registry.audit_denied(token, "recording_get", "N-0002")

    def test_default_scope_denies_audio_and_mutations(self):
        token = "S" * 48
        self.registry.create(caller_id="bot", token=token, scopes=None, recordings=["*"])
        for tool in ("recording_audio_get", "recordings_delete", "recording_get"):
            with self.assertRaises(grants.AuthorizationError):
                self.registry.authorize(token, tool)

    def test_create_rejects_more_than_100_allowlist_entries_before_archive_access(self):
        recordings = [f"N-{number:04d}" for number in range(1, 102)]
        with self.assertRaises(grants.GrantError) as caught:
            self.registry.create(caller_id="too-many", token="M" * 48,
                                 scopes=["metadata"], recordings=recordings)
        self.assertIn("at most 100", str(caught.exception))

    def test_mutations_are_atomic_audited_and_rotation_reenables_the_caller(self):
        first = "F" * 48
        second = "G" * 48
        created = self.registry.create(caller_id=" Agent.One ", token=first,
                                       scopes=["metadata"], recordings=["*"])
        self.assertEqual(created["caller_id"], "agent.one")
        revoked = self.registry.revoke(caller_id="AGENT.ONE")
        self.assertTrue(revoked["changed"])
        self.assertFalse(self.registry.revoke(caller_id="agent.one")["changed"])
        rotated = self.registry.rotate(caller_id="agent.one", token=second)
        self.assertFalse(rotated["disabled"])
        with self.assertRaises(grants.AuthorizationError):
            self.registry.authorize(first, "recordings_list")
        self.assertEqual(self.registry.authorize(second, "recordings_list").caller_id,
                         "agent.one")
        with sqlite3.connect(self.state) as conn:
            actions = conn.execute(
                "SELECT tool,outcome FROM audit_log WHERE caller_id='agent.one' ORDER BY id"
            ).fetchall()
        self.assertEqual(actions, [("admin.create", "mutated"),
                                   ("admin.revoke", "mutated"),
                                   ("admin.rotate", "mutated")])

    def test_caller_names_and_tokens_have_strict_bounds(self):
        for caller in ("", "../client-a", "client-a token", "a" * 65):
            with self.assertRaises(grants.GrantError):
                self.registry.create(caller_id=caller, token="B" * 48,
                                     scopes=["metadata"], recordings=["*"])
        with self.assertRaises(grants.GrantError):
            self.registry.create(caller_id="client-a", token="X" * 513,
                                 scopes=["metadata"], recordings=["*"])

    def test_legacy_mixed_case_caller_can_be_revoked_without_a_duplicate_identity(self):
        token = "Q" * 48
        now = "2026-01-01T00:00:00Z"
        with sqlite3.connect(self.state) as conn:
            conn.execute(
                "INSERT INTO grants(caller_id,token_hash,scopes_json,recordings_json,"
                "expires_at,disabled_at,created_at,updated_at) VALUES(?,?,?,?,NULL,NULL,?,?)",
                ("Legacy.Bot", grants.hash_token(token), "metadata", "*", now, now),
            )
        with self.assertRaises(grants.GrantError):
            self.registry.create(caller_id="legacy.bot", token="Z" * 48,
                                 scopes=["metadata"], recordings=["*"])
        revoked = self.registry.revoke(caller_id=" LEGACY.BOT ")
        self.assertTrue(revoked["changed"])
        with self.assertRaises(grants.AuthorizationError):
            self.registry.authorize(token, "recordings_list")


class AdminCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = os.path.join(self.directory.name, "state.db")

    def run_cli(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = admin.main(["--state", self.state, *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_create_shows_generated_token_once_and_list_never_discloses_it(self):
        code, output, error = self.run_cli(
            "create", "--caller-id", "client-a", "--scope", "metadata")
        self.assertEqual((code, error), (0, ""))
        created = json.loads(output)
        token = created.pop("token")
        self.assertGreaterEqual(len(token), 43)
        self.assertEqual(created["caller_id"], "client-a")

        code, listed, error = self.run_cli("list")
        self.assertEqual((code, error), (0, ""))
        self.assertNotIn(token, listed)
        self.assertNotIn("token", listed.lower())

    def test_create_and_rotate_can_write_an_exclusive_mode_0600_delivery_file(self):
        first_path = os.path.join(self.directory.name, "client-a.first")
        code, output, error = self.run_cli(
            "create", "--caller-id", "client-a", "--scope", "metadata",
            "--token-out", first_path)
        self.assertEqual((code, error), (0, ""))
        self.assertNotIn("token", output.lower())
        self.assertEqual(os.stat(first_path).st_mode & 0o777, 0o600)
        first = Path(first_path).read_text(encoding="utf-8").strip()

        second_path = os.path.join(self.directory.name, "client-a.second")
        code, output, error = self.run_cli(
            "rotate", "--caller-id", "CLIENT-A", "--token-out", second_path)
        self.assertEqual((code, error), (0, ""))
        self.assertNotIn("token", output.lower())
        second = Path(second_path).read_text(encoding="utf-8").strip()
        self.assertNotEqual(first, second)
        registry = grants.GrantRegistry(self.state)
        with self.assertRaises(grants.AuthorizationError):
            registry.authorize(first, "recordings_list")
        registry.authorize(second, "recordings_list")

        code, _, error = self.run_cli(
            "rotate", "--caller-id", "client-a", "--token-out", second_path)
        self.assertEqual(code, 2)
        self.assertIn("already exists", error)

    def test_cli_has_no_token_input_argument_and_revoke_is_idempotent(self):
        with self.assertRaises(SystemExit):
            self.run_cli("create", "--caller-id", "client-a", "--scope", "metadata",
                         "--token", "not-allowed")
        self.run_cli("create", "--caller-id", "client-a", "--scope", "metadata")
        code, first, _ = self.run_cli("revoke", "--caller-id", "client-a")
        code2, second, _ = self.run_cli("revoke", "--caller-id", "client-a")
        self.assertEqual((code, code2), (0, 0))
        self.assertTrue(json.loads(first)["changed"])
        self.assertFalse(json.loads(second)["changed"])


if __name__ == "__main__":
    unittest.main()
