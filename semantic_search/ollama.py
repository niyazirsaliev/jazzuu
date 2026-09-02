from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence


class OllamaUnavailable(RuntimeError):
    pass


class OllamaBackend:
    """Secret-free adapter to operator-managed Ollama stateless compute."""

    _ALLOWED_HOSTS = frozenset({"host.docker.internal", "localhost", "127.0.0.1", "::1"})
    _TAILNET = ipaddress.ip_network("100.64.0.0/10")

    @classmethod
    def _allowed_host(cls, host: str | None) -> bool:
        if not host:
            return False
        if host in cls._ALLOWED_HOSTS:
            return True
        try:
            return ipaddress.ip_address(host) in cls._TAILNET
        except ValueError:
            return False

    def __init__(self, *, endpoint: str, model: str, digest: str, dimension: int,
                 timeout_seconds: float = 0.75, opener: Callable = urllib.request.urlopen):
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.username or parsed.password:
            raise ValueError("Ollama endpoint must not contain credentials")
        if parsed.scheme != "http" or not self._allowed_host(parsed.hostname) or parsed.path not in ("", "/"):
            raise ValueError("Ollama must use a private local endpoint")
        if parsed.query or parsed.fragment:
            raise ValueError("Ollama endpoint must be an origin only")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid model digest")
        self.endpoint = endpoint.rstrip("/")
        self.model = str(model)
        self.digest = digest
        self.dimension = int(dimension)
        self.timeout_seconds = max(0.05, float(timeout_seconds))
        self._opener = opener

    def _json(self, request: urllib.request.Request) -> dict:
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise OllamaUnavailable("embedding response too large")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("not an object")
            return value
        except OllamaUnavailable:
            raise
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
            raise OllamaUnavailable(f"embedding service unavailable ({type(exc).__name__})") from None

    def _verify_digest(self) -> None:
        request = urllib.request.Request(f"{self.endpoint}/api/tags", method="GET")
        payload = self._json(request)
        matched = [item for item in payload.get("models", [])
                   if isinstance(item, dict) and item.get("name") == self.model]
        if not matched or matched[0].get("digest") != self.digest:
            raise OllamaUnavailable("embedding model digest mismatch")

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self._verify_digest()
        body = json.dumps({
            "model": self.model,
            "input": [str(text) for text in texts],
            "truncate": False,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.endpoint}/api/embed", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        payload = self._json(request)
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list):
            raise OllamaUnavailable("embedding response missing vectors")
        return embeddings
