"""Static fail-closed deployment gate for recordings-mcp artifacts."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
FILES = [ROOT / "recordings_mcp" / "Dockerfile", ROOT / "recordings_mcp" / "docker-compose.yml", ROOT / "recordings_mcp" / "recordings-mcp.env.example"]
FORBIDDEN = ("\n    privileged:", "network_mode: host", "/var/run/docker.sock", "tenant-b", "tenant-c", "family", "0.0.0.0:")

def validate():
    text = "\n".join(path.read_text(encoding="utf-8").lower() for path in FILES)
    problems = [term for term in FORBIDDEN if term in text]
    compose = FILES[1].read_text(encoding="utf-8")
    required = ("recordings-mcp:", "TENANT_ID:", ":/archive:ro", ":/state:rw", "read_only: true")
    if any(term not in compose for term in required):
        problems.append("generic tenant/archive/state contract")
    if "HEALTHCHECK" not in FILES[0].read_text(encoding="utf-8"):
        problems.append("healthcheck")
    return problems

if __name__ == "__main__":
    problems = validate()
    if problems:
        print("deployment validation failed: " + ", ".join(problems), file=sys.stderr)
        raise SystemExit(1)
    print("recordings-mcp deployment validation passed")
