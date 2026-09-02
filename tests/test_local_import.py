"""Local filenames remain private provenance, never semantic titles."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "archive"))

import local_import


ADDRESS_FILENAME = "101 Example Ave, Ste 200 9.m4a"


def test_local_import_uses_neutral_title_seed_instead_of_filename():
    seed = local_import.semantic_seed(ADDRESS_FILENAME)

    assert seed == "Локальная аудиозапись"
    assert "Example Ave" not in seed


def test_local_import_keeps_original_filename_only_in_private_provenance():
    item = {
        "original_name": ADDRESS_FILENAME,
        "source_sha256": "a" * 64,
        "source_size": 123,
        "source_duration_seconds": 9.5,
        "voice_memo_uuid": "private-id",
    }

    metadata = local_import.provenance_metadata(item)

    assert metadata["source_kind"] == "nextcloud_external_import"
    assert metadata["original_name"] == ADDRESS_FILENAME
    assert ADDRESS_FILENAME not in json.dumps(
        {"semantic_title_seed": local_import.semantic_seed(ADDRESS_FILENAME)},
        ensure_ascii=False,
    )


def test_local_import_database_values_never_seed_filename_into_semantic_name():
    item = {
        "id": "nc_abc123",
        "original_name": ADDRESS_FILENAME,
        "source_sha256": "a" * 64,
        "source_size": 123,
        "source_duration_seconds": 9.5,
        "creation_time": "2026-08-19T12:00:00-0500",
    }

    values = local_import.recording_values(
        item,
        duration_seconds=9.5,
        final_audio_path="/archive/audio/nc_abc123.mp3",
        archived_at="2026-08-19T12:01:00-0500",
    )

    assert values[0] == "nc_abc123"
    assert values[1] == "Локальная аудиозапись"
    assert ADDRESS_FILENAME not in values[1]
    assert json.loads(values[-1])["original_name"] == ADDRESS_FILENAME
