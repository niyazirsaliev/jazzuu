"""Static least-privilege and coexistence checks for the viewer deployment."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "deploy" / "viewer" / "scripts" / "validate_viewer.py"
COMPOSE = ROOT / "viewer" / "docker-compose.yml"
DOCKERFILE = ROOT / "viewer" / "Dockerfile"


def module():
    spec = importlib.util.spec_from_file_location("validate_viewer", VALIDATOR)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_current_viewer_deployment_is_least_privilege_and_tenant_parameterized():
    check = module()
    assert check.check_compose(COMPOSE) == []
    assert check.check_dockerfile(DOCKERFILE) == []


def test_viewer_defaults_to_loopback_bind():
    assert "${VIEWER_BIND_ADDR:-127.0.0.1}" in COMPOSE.read_text()


def test_viewer_validator_rejects_whole_credentials_mount_and_root_runtime(tmp_path):
    check = module()
    compose = tmp_path / "compose.yml"
    compose.write_text(COMPOSE.read_text().replace(
        "${TENANT_CONTROL_TOKEN_FILE:?TENANT_CONTROL_TOKEN_FILE must be set}:/run/secrets/recordings-control-token:ro",
        "${TENANT_CREDS_DIR:?TENANT_CREDS_DIR must be set}:/creds:ro"))
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(DOCKERFILE.read_text().replace("USER recordings", "USER root"))
    assert any("whole credential directory" in error for error in check.check_compose(compose))
    assert any("non-root UID/GID 10000" in error for error in check.check_dockerfile(dockerfile))


def test_viewer_requires_the_same_exact_control_token_file_as_connector(tmp_path):
    check = module()
    compose = tmp_path / "compose.yml"
    compose.write_text(COMPOSE.read_text().replace(
        "${TENANT_CONTROL_TOKEN_FILE:?TENANT_CONTROL_TOKEN_FILE must be set}:/run/secrets/recordings-control-token:ro",
        "${TENANT_CREDS_DIR:?TENANT_CREDS_DIR must be set}/recordings-control-token:/creds/recordings-control-token:ro"))
    errors = check.check_compose(compose)
    assert any("control token file" in error for error in errors)
