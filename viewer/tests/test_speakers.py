"""The speaker model, derived from real segment evidence only.

Everything here is pure: parsed segments in, a speaker list out. No DB, no
clock, no network — the same reason asr_progress.py is testable on its own.

The property under test throughout is *honesty*. A recording with no
diarization labels must report that it has none, never a speaker count guessed
from prose; a snippet interval must be a piece of audio a segment really
covers; and a stable id must survive a transcript regeneration so the name a
reader typed still belongs to the same voice.
"""

from app import speakers


def seg(speaker, start_ms, end_ms, text="реплика"):
    return {"speaker": speaker, "start_ms": start_ms, "end_ms": end_ms, "text": text}


# ---------------- extraction: 0 / 1 / many real labels ----------------

def test_no_segments_at_all_is_unavailable_not_zero_speakers():
    model = speakers.speaker_model(segments=[])

    assert model["state"] == "unavailable"
    assert model["count"] == 0
    assert model["speakers"] == []
    assert model["source"] is None
    # An honest reason, in Russian, that never claims a number it cannot prove.
    assert model["reason"] == "no_segments"
    assert "спикер" in model["note_ru"].lower()


def test_segments_without_speaker_labels_are_unavailable():
    model = speakers.speaker_model(
        segments=[seg("", 0, 4000), seg(None, 4000, 9000), seg("   ", 9000, 12000)]
    )

    assert model["state"] == "unavailable"
    assert model["reason"] == "no_labels"
    assert model["count"] == 0
    assert model["speakers"] == []


def test_single_real_speaker_is_reported_as_one():
    model = speakers.speaker_model(segments=[seg("Speaker 1", 0, 5000),
                                            seg("Speaker 1", 5000, 9000)])

    assert model["state"] == "ready"
    assert model["count"] == 1
    assert model["source"] == "plaud_segments"
    only = model["speakers"][0]
    assert only["source_label"] == "Speaker 1"
    assert only["speaker_id"] == "speaker-1"
    assert only["segment_count"] == 2
    assert only["total_ms"] == 9000
    assert only["display_name"] is None
    assert only["name_ru"] == "Спикер 1"


def test_multiple_speakers_keep_first_appearance_order_and_distinct_ids():
    model = speakers.speaker_model(segments=[
        seg("Speaker 2", 0, 3000),
        seg("Speaker 1", 3000, 6000),
        seg("Speaker 2", 6000, 8000),
        seg("Аня", 8000, 14000),
    ])

    assert model["state"] == "ready"
    assert model["count"] == 3
    assert [s["source_label"] for s in model["speakers"]] == [
        "Speaker 2", "Speaker 1", "Аня"]
    ids = [s["speaker_id"] for s in model["speakers"]]
    assert len(set(ids)) == 3
    assert ids[0] == "speaker-2"
    # A non-ASCII label still yields a URL-safe, bounded, deterministic id.
    assert ids[2] == speakers.source_speaker_id("Аня")
    assert ids[2] == ids[2].lower()
    assert all(c.isalnum() or c == "-" for c in ids[2])


def test_stable_ids_survive_relabelling_noise_so_names_survive_regeneration():
    # The same voice, spelled differently by a later ASR/PLAUD pass.
    for label in ("Speaker 1", "speaker 1", "  SPEAKER   1 ", "Speaker  #1",
                  "Спикер 1"):
        assert speakers.source_speaker_id(label) == "speaker-1", label
    assert speakers.source_speaker_id("Speaker 2") != "speaker-1"
    assert speakers.source_speaker_id("Аня") == speakers.source_speaker_id(" аня ")


def test_prose_never_invents_a_speaker():
    # A flat transcript mentioning names is not evidence of diarization.
    model = speakers.speaker_model(
        segments=[],
        transcript="Аня сказала, что Борис согласен. Speaker 1: точно.",
    )

    assert model["state"] == "unavailable"
    assert model["count"] == 0
    assert model["speakers"] == []


def test_asr_metadata_diarization_is_used_only_when_it_really_carries_labels():
    windows_only = {"segmented": True, "segments": [
        {"index": 0, "start_sec": 0, "duration_sec": 1800, "marker": "00:00:00"},
    ]}
    model = speakers.speaker_model(segments=[], asr_meta=windows_only)
    assert model["state"] == "unavailable", "ASR windows are not speakers"
    assert model["count"] == 0

    diarized = {"diarization": {"segments": [
        {"speaker": "SPEAKER_00", "start_ms": 0, "end_ms": 4000},
        {"speaker": "SPEAKER_01", "start_ms": 4000, "end_ms": 9000},
    ]}}
    model = speakers.speaker_model(segments=[], asr_meta=diarized)
    assert model["state"] == "ready"
    assert model["source"] == "asr_meta"
    assert model["count"] == 2


def test_speaker_count_is_bounded_so_a_broken_label_field_cannot_flood_the_ui():
    many = [seg(f"Speaker {i}", i * 1000, i * 1000 + 900) for i in range(200)]
    model = speakers.speaker_model(segments=many)

    assert model["count"] == len(model["speakers"])
    assert len(model["speakers"]) == speakers.MAX_SPEAKERS
    assert model["truncated"] is True


# ---------------- bounded snippet selection ----------------

def test_snippet_is_a_subinterval_of_a_real_segment_capped_at_the_maximum():
    model = speakers.speaker_model(segments=[
        seg("Speaker 1", 1000, 1400),                 # too short to represent
        seg("Speaker 1", 60_000, 60_000 + 600_000),   # 10 minutes
    ])

    snippet = model["speakers"][0]["snippet"]
    assert snippet["start_ms"] == 60_000
    assert snippet["end_ms"] == 60_000 + speakers.MAX_SNIPPET_MS
    assert snippet["end_ms"] - snippet["start_ms"] <= speakers.MAX_SNIPPET_MS


def test_snippet_prefers_the_longest_valid_segment_and_never_pads_past_it():
    model = speakers.speaker_model(segments=[
        seg("Speaker 1", 0, 900),
        seg("Speaker 1", 5_000, 8_000),
        seg("Speaker 1", 20_000, 20_500),
    ])

    snippet = model["speakers"][0]["snippet"]
    assert snippet == {"start_ms": 5_000, "end_ms": 8_000}


def test_snippet_is_clamped_to_the_recording_and_dropped_when_impossible():
    clamped = speakers.speaker_model(
        segments=[seg("Speaker 1", 9_000, 30_000)], duration_ms=12_000)
    assert clamped["speakers"][0]["snippet"] == {"start_ms": 9_000, "end_ms": 12_000}

    outside = speakers.speaker_model(
        segments=[seg("Speaker 1", 30_000, 40_000)], duration_ms=12_000)
    assert outside["speakers"][0]["snippet"] is None


def test_unusable_timestamps_yield_no_snippet_rather_than_a_guess():
    for start, end in ((None, 5000), (5000, None), (5000, 5000), (7000, 3000),
                       (-2000, -1000), ("abc", "def"), (0, 0)):
        model = speakers.speaker_model(segments=[seg("Speaker 1", start, end)])
        assert model["state"] == "ready", (start, end)
        assert model["speakers"][0]["snippet"] is None, (start, end)


def test_a_speaker_with_one_usable_and_one_broken_segment_still_gets_a_snippet():
    model = speakers.speaker_model(segments=[
        seg("Speaker 1", None, None),
        seg("Speaker 1", 4_000, 7_000),
    ])

    assert model["speakers"][0]["snippet"] == {"start_ms": 4_000, "end_ms": 7_000}
    assert model["speakers"][0]["segment_count"] == 2


# ---------------- names ----------------

def test_assigned_names_are_attached_to_matching_stable_ids_only():
    model = speakers.speaker_model(
        segments=[seg("Speaker 1", 0, 5000), seg("Speaker 2", 5000, 9000)],
        names={"speaker-1": "Аня", "speaker-9": "Никого"},
    )

    first, second = model["speakers"]
    assert first["display_name"] == "Аня"
    assert first["name_ru"] == "Аня"
    assert second["display_name"] is None
    assert second["name_ru"] == "Спикер 2"


def test_display_map_keys_raw_labels_so_presentation_can_substitute_them():
    model = speakers.speaker_model(
        segments=[seg("Speaker 1", 0, 5000), seg("Speaker 2", 5000, 9000)],
        names={"speaker-1": "Аня"},
    )

    assert speakers.display_map(model["speakers"]) == {"Speaker 1": "Аня"}


def test_name_validation_trims_bounds_and_refuses_control_characters():
    assert speakers.clean_name("  Анна   Петровна  ") == "Анна Петровна"
    assert speakers.clean_name("") is None
    assert speakers.clean_name("   ") is None
    assert speakers.clean_name(None) is None

    for bad in ("Аня\nБорис", "Аня\tБорис", "Аня\x00", "a" * (speakers.MAX_NAME_LEN + 1),
                "<script>alert(1)</script>", 42, {"name": "Аня"}):
        try:
            speakers.clean_name(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid name {bad!r}")


def test_speaker_id_validation_rejects_injection_and_traversal_shapes():
    assert speakers.valid_speaker_id("speaker-1")
    assert speakers.valid_speaker_id("ania-1f2e3d4c")
    for bad in ("../../etc/passwd", "speaker 1", "speaker-1'; DROP TABLE x;--",
                "<b>", "", "x" * 80, None, 7, "СПИКЕР"):
        assert not speakers.valid_speaker_id(bad), bad


# ---------------- presentation substitution (raw text untouched) ----------------

def test_transcript_presentation_substitutes_names_without_touching_raw_text():
    raw = "[Speaker 1] Привет\n[Speaker 2] Как дела\n[Speaker 1] Нормально"
    shown = speakers.apply_names_to_text(raw, {"Speaker 1": "Аня"})

    assert shown == "[Аня] Привет\n[Speaker 2] Как дела\n[Аня] Нормально"
    assert raw == "[Speaker 1] Привет\n[Speaker 2] Как дела\n[Speaker 1] Нормально"
    assert speakers.apply_names_to_text(raw, {}) == raw
    assert speakers.apply_names_to_text(None, {"Speaker 1": "Аня"}) is None


def test_segments_gain_ids_and_display_names_but_keep_their_raw_label():
    segments = [seg("Speaker 1", 0, 5000), seg("Speaker 2", 5000, 9000)]
    labelled = speakers.label_segments(segments, {"speaker-1": "Аня"})

    assert labelled[0]["speaker"] == "Speaker 1", "raw candidate is preserved"
    assert labelled[0]["speaker_id"] == "speaker-1"
    assert labelled[0]["display_name"] == "Аня"
    assert labelled[1]["display_name"] is None
    assert segments[0] == seg("Speaker 1", 0, 5000), "input list not mutated"
