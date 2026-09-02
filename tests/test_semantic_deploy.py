from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_all_runtime_images_package_one_shared_semantic_core():
    viewer = (ROOT / "viewer/Dockerfile").read_text()
    connector = (ROOT / "archive/Dockerfile").read_text()
    mcp = (ROOT / "recordings_mcp/Dockerfile").read_text()

    assert "COPY semantic_search/" in viewer
    assert "COPY semantic_search/" in connector
    assert "COPY semantic_search/" in mcp
    assert "\nRUN pip install" not in mcp


def test_semantic_index_is_tenant_local_and_readonly_for_consumers():
    viewer = (ROOT / "viewer/docker-compose.yml").read_text()
    connector = (ROOT / "archive/docker-compose.yml").read_text()
    mcp = (ROOT / "recordings_mcp/docker-compose.yml").read_text()

    for compose in (viewer, connector, mcp):
        assert "SEMANTIC_INDEX_PATH: /archive/semantic/semantic.db" in compose
        assert "host.docker.internal:host-gateway" in compose
        assert "api.openai.com" not in compose
        assert "api.cohere.com" not in compose
    assert ":/archive:ro" in viewer
    assert ":/archive:ro" in mcp
    assert ":/archive\n" in connector


def test_recordings_mcp_release_requires_an_immutable_image_pin():
    compose = (ROOT / "recordings_mcp/docker-compose.yml").read_text()

    assert "image: ${RECORDINGS_MCP_IMAGE:?RECORDINGS_MCP_IMAGE must be set}" in compose
    assert "recordings-mcp:latest" not in compose


def test_viewer_build_context_supports_the_deployed_compose_location():
    compose = (ROOT / "viewer/docker-compose.yml").read_text()

    assert "context: ${RECORDINGS_SOURCE_ROOT:-..}" in compose


def test_pwa_cache_cannot_capture_queries_indices_or_model_artifacts():
    sw = (ROOT / "viewer/app/static/sw.js").read_text()
    manifest = (ROOT / "semantic_search/model_manifest.json").read_text()

    assert "url.search" in sw
    assert "SHELL_PATHS.has(url.pathname)" in sw
    assert "semantic" not in sw.split("const SHELL =", 1)[1].split(";", 1)[0]
    assert "bge-m3" in manifest
    assert "semantic_search/model_manifest.json" not in sw
