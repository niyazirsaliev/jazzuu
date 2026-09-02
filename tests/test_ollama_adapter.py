import io
import json
import urllib.error

import pytest

from semantic_search.embedder import PrefixingNormalizedEmbedder
from semantic_search.ollama import OllamaBackend, OllamaUnavailable


DIGEST = "7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab"


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_ollama_backend_verifies_pinned_digest_before_embedding_and_sends_no_auth():
    requests = []

    def open_request(request, timeout):
        requests.append((request, timeout))
        if request.full_url.endswith("/api/tags"):
            return Response(json.dumps({"models": [{"name": "bge-m3:latest", "digest": DIGEST}]}).encode())
        assert request.full_url.endswith("/api/embed")
        payload = json.loads(request.data)
        assert payload == {"model": "bge-m3:latest", "input": ["текст"], "truncate": False}
        return Response(json.dumps({"embeddings": [[1.0, 0.0, 0.0]]}).encode())

    backend = OllamaBackend(
        endpoint="http://host.docker.internal:11434",
        model="bge-m3:latest",
        digest=DIGEST,
        dimension=3,
        timeout_seconds=0.5,
        opener=open_request,
    )
    embedder = PrefixingNormalizedEmbedder(backend, query_prefix="", passage_prefix="")

    assert embedder.embed_passages(["текст"]) == [[1.0, 0.0, 0.0]]
    assert [request.headers.get("Authorization") for request, _ in requests] == [None, None]
    assert [timeout for _, timeout in requests] == [0.5, 0.5]


def test_ollama_backend_fails_closed_before_content_when_digest_drifted():
    requests = []

    def open_request(request, timeout):
        requests.append(request.full_url)
        return Response(json.dumps({"models": [{"name": "bge-m3:latest", "digest": "f" * 64}]}).encode())

    backend = OllamaBackend(
        endpoint="http://host.docker.internal:11434",
        model="bge-m3:latest",
        digest=DIGEST,
        dimension=1024,
        opener=open_request,
    )

    with pytest.raises(OllamaUnavailable, match="digest"):
        backend.embed(["private content"])
    assert requests == ["http://host.docker.internal:11434/api/tags"]


def test_ollama_backend_rejects_public_or_credential_bearing_endpoint():
    with pytest.raises(ValueError, match="private local endpoint"):
        OllamaBackend(endpoint="http://example.com:11434", model="bge-m3", digest=DIGEST, dimension=1024)
    with pytest.raises(ValueError, match="credentials"):
        OllamaBackend(endpoint="http://user:pw@localhost:11434", model="bge-m3", digest=DIGEST, dimension=1024)


def test_ollama_backend_accepts_a_tailnet_endpoint():
    backend = OllamaBackend(
        endpoint="http://127.0.0.1:11434",
        model="bge-m3",
        digest=DIGEST,
        dimension=1024,
    )

    assert backend.endpoint == "http://127.0.0.1:11434"


def test_ollama_backend_redacts_timeout_and_never_includes_content():
    def timeout(_request, timeout=None):
        raise urllib.error.URLError("private transcript fragment")

    backend = OllamaBackend(
        endpoint="http://localhost:11434", model="bge-m3:latest",
        digest=DIGEST, dimension=1024, opener=timeout,
    )

    with pytest.raises(OllamaUnavailable) as error:
        backend.embed(["private transcript fragment"])
    assert "private transcript fragment" not in str(error.value)
