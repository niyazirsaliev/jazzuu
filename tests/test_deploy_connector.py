"""Tests for the connector deployment validator.

The validator encodes the deployment invariants that no unit test can see: the
connector receives its caller token as a mounted FILE, each tenant declares the
one MCP service it may talk to, and no family member can reach another's — or
the owner's — recordings.

These are mutation tests. Each one starts from the files actually shipped in
this repo, breaks exactly one thing, and asserts the validator catches it. A
rule that only passes against a hand-written fixture proves nothing about what
is deployed.
"""
import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "archive" / "docker-compose.yml"
DOCKERFILE = ROOT / "archive" / "Dockerfile"
SAMPLES = ROOT / "deploy" / "connector" / "samples"
TENANT_ALPHA_ENV = SAMPLES / "connector-tenant-alpha.env.example"
TENANT_BETA_ENV = SAMPLES / "connector-tenant-beta.env.example"
TENANT_ENVS = (TENANT_ALPHA_ENV, TENANT_BETA_ENV)


def load_validator():
    path = ROOT / "deploy" / "connector" / "scripts" / "validate_connector.py"
    spec = importlib.util.spec_from_file_location("validate_connector", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ValidatorTestCase(unittest.TestCase):
    def setUp(self):
        self.validator = load_validator()
        self.tmp = Path(tempfile.mkdtemp())

    def mutate(self, source, old, new):
        """Copy a shipped file with one substitution applied."""
        text = source.read_text()
        self.assertIn(old, text,
                      f"{source.name} no longer contains {old!r}; the mutation "
                      f"this test relies on is stale")
        path = self.tmp / source.name
        path.write_text(text.replace(old, new))
        return path

    def copy(self, source):
        path = self.tmp / source.name
        path.write_text(source.read_text())
        return path

    def assertRejected(self, errors, needle):
        joined = " ".join(errors)
        self.assertTrue(errors, "validator accepted a broken deployment")
        self.assertIn(needle, joined)


class ShippedStackTests(ValidatorTestCase):
    def test_the_shipped_compose_and_samples_validate(self):
        errors = self.validator.check_compose(COMPOSE)
        loaded = []
        for env_path in TENANT_ENVS:
            env = self.validator.parse_env(env_path)
            loaded.append((env_path, env))
            errors += self.validator.check_env_file(env_path, env)
        errors += self.validator.check_cross_tenant(loaded)
        self.assertEqual(errors, [])

    def test_connectors_default_to_ten_minute_windows(self):
        text = COMPOSE.read_text()
        self.assertIn(
            "ASR_SEGMENT_THRESHOLD_SECONDS: ${ASR_SEGMENT_THRESHOLD_SECONDS:-600}",
            text,
        )
        self.assertIn(
            "ASR_SEGMENT_SECONDS: ${ASR_SEGMENT_SECONDS:-600}", text
        )

    def test_asr_uses_utterance_level_mixed_router(self):
        self.assertIn(
            "PIPELINE_ASR_ENGINE: ${PIPELINE_ASR_ENGINE:-mixed}",
            COMPOSE.read_text(),
        )

    def test_legacy_manual_asr_path_uses_the_same_configured_engine(self):
        text = (ROOT / "archive" / "connector.py").read_text()
        self.assertIn("review_next(conn, sid, engine=ASR_ENGINE)", text)
        self.assertIn("conn, sid, rid, ASR_ENGINE,", text)

    def test_compatibility_stage_also_defaults_to_mixed(self):
        text = (ROOT / "archive" / "stages.py").read_text()
        self.assertIn(
            "ASR_ENGINE = os.environ.get('PIPELINE_ASR_ENGINE', 'mixed').strip() or 'mixed'",
            text,
        )

    def test_diarization_models_use_one_optional_shared_read_only_host_root(self):
        text = COMPOSE.read_text()
        self.assertIn("DIARIZATION_MODELS_DIR", text)
        self.assertIn("/models/segmentation/model.onnx", text)
        self.assertIn("/models/embedding.onnx", text)
        self.assertNotIn("DIARIZATION_SEGMENTATION_DIR:?", text)
        self.assertNotIn("DIARIZATION_EMBEDDING_MODEL:?", text)

        roots = {
            self.validator.parse_env(path).get("DIARIZATION_MODELS_DIR")
            for path in TENANT_ENVS
        }
        self.assertEqual(roots, {"/srv/jazzuu/models/diarization"})

    def test_docker_healthcheck_uses_the_live_probe(self):
        """A non-empty but revoked caller token must never report healthy."""
        text = DOCKERFILE.read_text()
        self.assertIn('CMD ["python", "/app/connector.py", "probe"]', text)
        self.assertNotIn('CMD ["python", "/app/connector.py", "status"]', text)

    def test_the_probe_budget_still_fits_the_healthcheck_timeout(self):
        """Docker kills the healthcheck at --timeout and records only
        "unhealthy": no failure class, no message. A probe whose own request
        deadlines add up past that window throws away every distinction it
        exists to make, and the Dockerfile is where the window is set, so the
        two are checked against each other rather than trusted to agree."""
        text = DOCKERFILE.read_text()
        declared = re.search(r"HEALTHCHECK[^\n]*--timeout=(\d+)s", text)
        self.assertIsNotNone(
            declared, "the healthcheck no longer declares a --timeout")
        connector = self.load_connector()
        self.assertEqual(connector.HEALTHCHECK_TIMEOUT_S,
                         int(declared.group(1)),
                         "connector.HEALTHCHECK_TIMEOUT_S has drifted from the "
                         "Dockerfile's HEALTHCHECK --timeout")
        self.assertGreaterEqual(connector.PROBE_STARTUP_MARGIN_S, 3)
        self.assertLessEqual(
            connector.PROBE_DEADLINE_S,
            connector.HEALTHCHECK_TIMEOUT_S - connector.PROBE_STARTUP_MARGIN_S,
            "probe deadline must leave an explicit process-start/output margin")

    def load_connector(self):
        """The connector module, resolved as an explicit tenant.

        Explicit mode avoids reading any host compatibility credential.
        """
        archive = self.tmp / "archive"
        archive.mkdir(exist_ok=True)
        token = self.tmp / "plaud-mcp-token"
        token.write_text("not-a-real-token")
        env = {"TENANT_ID": "tenant-alpha",
               "TENANT_ARCHIVE_DIR": str(archive),
               "PLAUD_MCP_TOKEN_FILE": str(token),
               "PLAUD_MCP_TENANT_URLS_JSON":
                   '{"tenant-alpha":"https://mcp-alpha.example/mcp"}'}
        path = ROOT / "archive" / "connector.py"
        spec = importlib.util.spec_from_file_location(
            "connector_for_healthcheck", path)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {spec.name: module}), \
             mock.patch.dict(os.environ, env, clear=False):
            spec.loader.exec_module(module)
        return module


class ComposeCredentialTests(ValidatorTestCase):
    def test_control_token_file_mode_must_be_0600(self):
        token = self.tmp / "recordings-control-token"
        token.write_text("x" * 64)
        token.chmod(0o660)

        self.assertRejected(
            self.validator.check_control_token_file(token), "0600")

    def test_control_token_file_mode_0600_is_allowed(self):
        token = self.tmp / "recordings-control-token"
        token.write_text("x" * 64)
        token.chmod(0o600)

        self.assertEqual(self.validator.check_control_token_file(token), [])

    def test_a_compose_that_does_not_mount_the_caller_token_is_rejected(self):
        """The caller token is the connector's only credential. If compose
        stops pointing at the mounted file, every tenant fails auth at once."""
        mutated = self.mutate(
            COMPOSE, "PLAUD_MCP_TOKEN_FILE: /creds/plaud-mcp-token",
            "SOMETHING_ELSE: /creds/plaud-mcp-token")
        self.assertRejected(self.validator.check_compose(mutated),
                            "PLAUD_MCP_TOKEN_FILE")

    def test_control_token_uses_a_separate_required_exact_file_mount(self):
        """The control token lives outside legacy PLAUD/ASR creds and must not
        depend on creating a nested mountpoint inside read-only /creds."""
        text = COMPOSE.read_text()
        self.assertIn(
            "${TENANT_CREDS_DIR:?TENANT_CREDS_DIR must be set}:/creds:ro", text)
        self.assertIn(
            "${TENANT_CONTROL_TOKEN_FILE:?TENANT_CONTROL_TOKEN_FILE must be set}:/run/secrets/recordings-control-token:ro",
            text)
        self.assertNotIn(
            "${TENANT_CREDS_DIR:?TENANT_CREDS_DIR must be set}/recordings-control-token",
            text)

    def test_a_writable_credential_mount_is_rejected(self):
        """Nothing under /creds is written any more. Write access there is
        blast radius on the directory holding this tenant's tokens."""
        mutated = self.mutate(COMPOSE, "}:/creds:ro", "}:/creds")
        self.assertRejected(self.validator.check_compose(mutated), ":ro")

    def test_dropping_the_credential_mount_entirely_is_rejected(self):
        mutated = self.mutate(
            COMPOSE, "- ${TENANT_CREDS_DIR:?TENANT_CREDS_DIR must be set}:/creds:ro",
            "# credential mount removed")
        self.assertRejected(self.validator.check_compose(mutated), "/creds")

    def test_an_optional_asr_token_path_is_rejected(self):
        """The durable pipeline's ASR caller credential is a startup contract,
        not an optional compatibility setting for the owner connector."""
        mutated = self.mutate(COMPOSE,
                              "ASR_TOKEN_FILE: ${ASR_TOKEN_FILE:?ASR_TOKEN_FILE must be set}",
                              "ASR_TOKEN_FILE: ${ASR_TOKEN_FILE:-}")
        self.assertRejected(self.validator.check_compose(mutated),
                            "ASR_TOKEN_FILE")

    def test_shared_asr_audio_path_prefix_is_passed_to_the_connector(self):
        """A tenant's host-staged ASR mount path must reach asr_backfill.py.

        An empty value remains safe for generic fixtures, but a tenant env such
        as ASR_AUDIO_PATH_PREFIX=/tenant-audio/tenant-b must not be discarded by the
        explicit Compose environment list.
        """
        self.assertIn(
            "ASR_AUDIO_PATH_PREFIX: ${ASR_AUDIO_PATH_PREFIX:-}",
            COMPOSE.read_text())

    def test_a_writable_diarization_model_mount_is_rejected(self):
        mutated = self.mutate(
            COMPOSE,
            "${DIARIZATION_MODELS_DIR:-/srv/jazzuu/models/diarization}:/models:ro",
            "${DIARIZATION_MODELS_DIR:-/srv/jazzuu/models/diarization}:/models",
        )
        self.assertRejected(self.validator.check_compose(mutated),
                            "diarization model")

    def test_removing_shared_asr_audio_path_prefix_is_rejected(self):
        mutated = self.mutate(
            COMPOSE,
            "      ASR_AUDIO_PATH_PREFIX: ${ASR_AUDIO_PATH_PREFIX:-}\n",
            "")
        self.assertRejected(self.validator.check_compose(mutated),
                            "ASR_AUDIO_PATH_PREFIX")

    def test_control_token_parent_directory_mount_is_rejected(self):
        """Only the token file may override /creds; a second directory mount
        would expose unrelated viewer credentials to the connector."""
        mutated = self.mutate(
            COMPOSE,
            "${TENANT_CONTROL_TOKEN_FILE:?TENANT_CONTROL_TOKEN_FILE must be set}:/run/secrets/recordings-control-token:ro",
            "${TENANT_CONTROL_TOKEN_FILE:?TENANT_CONTROL_TOKEN_FILE must be set}:/run/secrets:ro")
        self.assertRejected(self.validator.check_compose(mutated),
                            "exact file")

    def test_an_additional_control_token_parent_directory_mount_is_rejected(self):
        """A valid exact-file mount must not let a copied whole-directory mount
        slip through alongside it."""
        exact = "${TENANT_CONTROL_TOKEN_FILE:?TENANT_CONTROL_TOKEN_FILE must be set}:/run/secrets/recordings-control-token:ro"
        mutated = self.mutate(
            COMPOSE, exact, exact + "\n      - ${TENANT_CONTROL_TOKEN_FILE:?TENANT_CONTROL_TOKEN_FILE must be set}:/run/secrets:ro")
        self.assertRejected(self.validator.check_compose(mutated),
                            "parent directory")

    def test_duplicate_mount_destination_is_rejected(self):
        exact = "${TENANT_CONTROL_TOKEN_FILE:?TENANT_CONTROL_TOKEN_FILE must be set}:/run/secrets/recordings-control-token:ro"
        mutated = self.mutate(
            COMPOSE, exact,
            exact + "\n      - ${TENANT_CREDS_DIR:?TENANT_CREDS_DIR must be set}:/run/secrets/recordings-control-token:ro")
        self.assertRejected(self.validator.check_compose(mutated),
                            "duplicate mount destination")

    def test_reintroducing_a_direct_plaud_oauth_token_file_is_rejected(self):
        """The connector must not go back to holding PLAUD account
        credentials: the tenant's MCP service owns that lifecycle now, and a
        second refresher revokes the first one's tokens."""
        mutated = self.mutate(
            COMPOSE, "PLAUD_MCP_TOKEN_FILE: /creds/plaud-mcp-token",
            "PLAUD_MCP_TOKEN_FILE: /creds/plaud-mcp-token\n"
            "      PLAUD_TOKENS_FILE: /creds/tokens-mcp.json")
        self.assertRejected(self.validator.check_compose(mutated),
                            "PLAUD_TOKENS_FILE")

    def test_an_ambient_mcp_url_in_compose_is_rejected(self):
        """A bare PLAUD_MCP_URL applies to every tenant that starts from this file."""
        mutated = self.mutate(
            COMPOSE, "PLAUD_MCP_TOKEN_FILE: /creds/plaud-mcp-token",
            "PLAUD_MCP_TOKEN_FILE: /creds/plaud-mcp-token\n"
            "      PLAUD_MCP_URL: ${PLAUD_MCP_URL:-http://127.0.0.1:62380/mcp}")
        self.assertRejected(self.validator.check_compose(mutated),
                            "PLAUD_MCP_URL")

    def test_a_caller_token_passed_as_an_env_var_is_rejected(self):
        mutated = self.mutate(
            COMPOSE, "PLAUD_MCP_TOKEN_FILE: /creds/plaud-mcp-token",
            "PLAUD_MCP_TOKEN: ${PLAUD_MCP_TOKEN:-}\n"
            "      PLAUD_MCP_TOKEN_FILE: /creds/plaud-mcp-token")
        self.assertRejected(self.validator.check_compose(mutated),
                            "mounted as a file")


class TenantEnvTargetTests(ValidatorTestCase):
    def check(self, path):
        return self.validator.check_env_file(path, self.validator.parse_env(path))

    def test_each_sample_selects_its_mapped_service(self):
        for path in TENANT_ENVS:
            env = self.validator.parse_env(path)
            mapping = self.validator.parse_tenant_urls(env["PLAUD_MCP_TENANT_URLS_JSON"])
            self.assertEqual(env["PLAUD_MCP_EXPECTED_URL"], mapping[env["TENANT_ID"]])

    def test_unknown_tenant_is_rejected(self):
        mutated = self.mutate(TENANT_ALPHA_ENV, "TENANT_ID=tenant-alpha", "TENANT_ID=tenant-gamma")
        self.assertRejected(self.check(mutated), "tenant-gamma")

    def test_malformed_mapping_is_rejected(self):
        mutated = self.mutate(TENANT_ALPHA_ENV, "PLAUD_MCP_TENANT_URLS_JSON={\"tenant-alpha\":\"https://mcp-alpha.example/mcp\",\"tenant-beta\":\"https://mcp-beta.example/mcp\"}", "PLAUD_MCP_TENANT_URLS_JSON={")
        self.assertRejected(self.check(mutated), "valid JSON object")

    def test_expected_url_mismatch_is_rejected(self):
        mutated = self.mutate(TENANT_ALPHA_ENV, "PLAUD_MCP_EXPECTED_URL=https://mcp-alpha.example/mcp", "PLAUD_MCP_EXPECTED_URL=https://other.example/mcp")
        self.assertRejected(self.check(mutated), "does not match")

    def test_two_tenants_may_not_share_one_service(self):
        mutated = self.mutate(TENANT_ALPHA_ENV, "\"tenant-beta\":\"https://mcp-beta.example/mcp\"", "\"tenant-beta\":\"https://mcp-alpha.example/mcp\"")
        self.assertRejected(self.check(mutated), "exactly one tenant")

    def test_samples_use_one_identical_reviewed_mapping(self):
        alpha = self.copy(TENANT_ALPHA_ENV)
        beta = self.mutate(TENANT_BETA_ENV, "\"tenant-beta\":\"https://mcp-beta.example/mcp\"", "\"tenant-beta\":\"https://other.example/mcp\"")
        errors = self.validator.check_cross_tenant([(alpha, self.validator.parse_env(alpha)), (beta, self.validator.parse_env(beta))])
        self.assertRejected(errors, "PLAUD_MCP_TENANT_URLS_JSON")

    def test_control_token_files_are_distinct_absolute_paths(self):
        token_files = [self.validator.parse_env(path)["TENANT_CONTROL_TOKEN_FILE"] for path in TENANT_ENVS]
        self.assertTrue(all(path.startswith("/") for path in token_files))
        self.assertEqual(len(token_files), len(set(token_files)))
