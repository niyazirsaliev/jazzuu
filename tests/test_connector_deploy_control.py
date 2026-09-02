"""Deployment control-plane isolation is statically enforceable."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "deploy" / "connector" / "scripts" / "validate_connector.py"
spec = importlib.util.spec_from_file_location("validate_connector", PATH)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


def test_validator_requires_private_control_mount_and_distinct_tenant_path(tmp_path):
    compose = tmp_path / "compose.yml"
    compose.write_text((ROOT / "archive" / "docker-compose.yml").read_text())
    tenant_a = tmp_path / "tenant-alpha.env"
    tenant_b = tmp_path / "tenant-beta.env"
    base = (ROOT / "archive" / "connector.env.example").read_text()
    tenant_a.write_text(base)
    tenant_b.write_text(
        base.replace("TENANT_ID=tenant-alpha", "TENANT_ID=tenant-beta")
            .replace("COMPOSE_PROJECT_NAME=jazzuu-connector-tenant-alpha",
                     "COMPOSE_PROJECT_NAME=jazzuu-connector-tenant-beta")
            .replace("/tmp/jazzuu/", "/tmp/jazzuu-beta/")
            .replace("PLAUD_MCP_EXPECTED_URL=https://mcp-alpha.example/mcp",
                     "PLAUD_MCP_EXPECTED_URL=https://mcp-beta.example/mcp"))
    broken = compose.read_text().replace("${TENANT_CONTROL_DIR:?TENANT_CONTROL_DIR must be set}:/run/recordings", "${TENANT_ARCHIVE_DIR:?TENANT_ARCHIVE_DIR must be set}:/run/recordings")
    compose.write_text(broken)
    errors = validator.check_compose(compose)
    env_a, env_b = validator.parse_env(tenant_a), validator.parse_env(tenant_b)
    errors += validator.check_env_file(tenant_a, env_a)
    errors += validator.check_cross_tenant([(tenant_a, env_a), (tenant_b, env_b)])
    assert any("control" in error.lower() for error in errors)


def test_reauth_errors_never_print_remote_token_payloads():
    script = (ROOT / "deploy/connector/scripts/plaud_reauth.py").read_text()
    assert "e.read()" not in script
    assert "json.dumps(payload)" not in script
