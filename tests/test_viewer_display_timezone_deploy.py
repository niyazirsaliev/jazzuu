"""The per-tenant display time zone must reach the running viewer.

Displayed date/time is tenant-local presentation, configured explicitly per
deployment rather than guessed from the reader's browser. That configuration is
only real if the compose file actually forwards it into the container, so this
pins the plumbing rather than the Python default.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "viewer" / "docker-compose.yml"
ARCHIVE_COMPOSE = ROOT / "archive" / "docker-compose.yml"


def test_viewer_compose_forwards_the_per_tenant_display_timezone():
    compose = COMPOSE.read_text()

    assert "DISPLAY_TIMEZONE: ${DISPLAY_TIMEZONE:-}" in compose


def test_the_display_timezone_is_not_hardcoded_to_one_tenant():
    """One image serves many tenants; the zone is per-tenant env."""
    compose = COMPOSE.read_text()

    for zone in ("America/New_York", "Asia/Dhaka"):
        assert zone not in compose


def test_archive_timezone_is_explicit_and_not_location_specific():
    compose = ARCHIVE_COMPOSE.read_text()
    assert "ARCHIVE_TIMEZONE: ${ARCHIVE_TIMEZONE:-UTC}" in compose
    for source in (ROOT / "archive/archive_recording.py", ROOT / "archive/asr_backfill.py"):
        assert "America/New_York" not in source.read_text()
