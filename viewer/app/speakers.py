"""The speaker model for one recording, derived from real segment evidence.

What this module is for
----------------------
PLAUD hands us structured segments (`plaud_segments_json`: speaker label +
start/end + text) and, for recordings we transcribed ourselves, ASR metadata.
Those segment labels are the *only* evidence that a recording has more than one
voice in it. This module turns that evidence into a speaker list, and refuses to
produce anything at all when the evidence is missing.

Deliberate properties, each learned from a way this could lie to a reader:

* **Never invents a speaker.** A transcript that says "Аня сказала…" is prose,
  not diarization. Only a segment carrying a non-empty `speaker` label counts,
  so a recording with no labels reports `unavailable` — with a reason — rather
  than a fabricated count of 1.
* **ASR windows are not speakers.** `asr_meta_json` carries the windows a long
  recording was transcribed in (`segments: [{index, start_sec, …}]`). Those
  entries have no speaker label and are skipped by construction. Only an
  explicit `diarization.segments` block, which a future engine would write, is
  read as speaker evidence.
* **Stable ids, so a typed name outlives a regeneration.** The id is derived
  from the *normalised* label, so "Speaker 1", "speaker  1" and "SPEAKER_01"
  from three different passes are one speaker and keep the name a reader gave
  them. The raw label is preserved alongside, never overwritten.
* **Snippets are real audio.** A representative interval is always a
  subinterval of a segment that actually exists, capped at MAX_SNIPPET_MS and
  clamped to the recording. A segment with unusable timestamps yields no
  snippet rather than a guessed one.
* **Presentation only.** Names are substituted when text is *rendered*
  (`apply_names_to_text`, `label_segments`); the stored PLAUD/ASR candidates are
  never rewritten. The archive mount is read-only and these labels are the raw
  record.

Has no fastapi and no sqlite dependency on purpose, so the rules stay unit
testable without the app's runtime stack (see tests/test_speakers.py).
"""

import hashlib
import re

# Where a speaker list came from. Reported to the UI so a reader can tell
# PLAUD's own diarization from ours.
SOURCE_PLAUD = "plaud_segments"
SOURCE_ASR_META = "asr_meta"

# A snippet exists to let someone recognise a voice, not to re-listen to the
# meeting. Long enough to identify, short enough that it is never a way to
# stream the whole recording one "snippet" at a time.
MAX_SNIPPET_MS = 12_000
# Below this a segment is a poor representative, so a longer one is preferred
# when the speaker has one. It is never padded to reach this length: padding
# would put audio the segment does not cover under a speaker's name.
PREFERRED_SNIPPET_MS = 1_500

# Bounds on what a mutation may carry and what the UI may be asked to render.
MAX_NAME_LEN = 60
MAX_SPEAKERS = 24
MAX_SPEAKER_ID_LEN = 48

# Characters a display name may not contain. Control characters break the
# single-line layout and the CSV-ish shapes this text ends up in; the markup
# characters are rejected at the boundary as well as escaped at render time, so
# a name can never be a payload even if some future caller forgets to escape.
_NAME_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f<>&\"\\`]")
_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,%d}" % (MAX_SPEAKER_ID_LEN - 1))

# "Speaker 1", "speaker_01", "Спикер 2", "S3" — the same anonymous voice under
# every spelling the two upstreams and their future versions use.
_ANON_RE = re.compile(
    r"(?:speaker|спикер|говорящий|голос|spk|s)[\s_:#.\-]*(\d{1,3})")

# A transcript line as the archive writes it: "[Speaker 1] текст".
_LINE_LABEL_RE = re.compile(r"^(\s*)\[([^\]\n]*)\](.*)$")

DIARIZATION_UNAVAILABLE_RU = (
    "Разметка спикеров недоступна: в записи нет сегментов с метками спикеров."
)
DIARIZATION_PENDING_RU = "Разметка спикеров в очереди на обработку."


def _norm_label(label) -> str:
    """A label reduced to what identity should not depend on: case and spacing."""
    if not isinstance(label, str):
        return ""
    return re.sub(r"\s+", " ", label).strip().casefold()


def source_speaker_id(label) -> str:
    """Stable id for one source label. Same voice, same id, across passes.

    Anonymous numbered labels collapse onto `speaker-N` so a re-transcription
    that renames "Speaker 1" to "SPEAKER_01" does not orphan the name a reader
    typed. Anything else gets an ascii slug plus a short digest of the
    normalised label: the digest keeps two different names apart even when their
    slugs are both empty (every Cyrillic label), and it is derived, so the id is
    reproducible without storing a counter anywhere.
    """
    norm = _norm_label(label)
    if not norm:
        return ""
    anon = _ANON_RE.fullmatch(norm)
    if anon:
        return f"speaker-{int(anon.group(1))}"
    slug = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")[:24].strip("-")
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}" if slug else f"spk-{digest}"


def valid_speaker_id(speaker_id) -> bool:
    """Whether a client-supplied id is one this module could have produced.

    Enforced at every mutation boundary: an id is used as a dict key, a SQL
    parameter and an HTML attribute, so the safe answer is a narrow character
    set rather than escaping at each use.
    """
    return isinstance(speaker_id, str) and bool(_ID_RE.fullmatch(speaker_id))


def clean_name(name):
    """The display name to store, or None to clear it. Raises on invalid input.

    None/blank means "no name" — that is how a reader deletes one — and is not
    an error. Everything else must be a single short line of ordinary text.
    """
    if name is None:
        return None
    if not isinstance(name, str):
        raise ValueError("name must be a string")
    # Spaces (including the NBSP an iOS keyboard inserts) collapse; tabs and
    # newlines do NOT — they are control characters and are refused below
    # rather than quietly becoming a name the reader never typed.
    collapsed = re.sub("[ \u00a0]+", " ", name).strip(" \u00a0")
    if not collapsed:
        return None
    if _NAME_FORBIDDEN.search(collapsed):
        raise ValueError("name contains characters that are not allowed")
    if len(collapsed) > MAX_NAME_LEN:
        raise ValueError(f"name is longer than {MAX_NAME_LEN} characters")
    return collapsed


def name_ru(source_label, display_name=None) -> str:
    """What to call this speaker in the UI: the reader's name, else the label.

    An anonymous numbered label is localised ("Speaker 1" → "Спикер 1"); a real
    name PLAUD already resolved is shown as it stands.
    """
    if display_name:
        return display_name
    anon = _ANON_RE.fullmatch(_norm_label(source_label))
    if anon:
        return f"Спикер {int(anon.group(1))}"
    return (source_label or "").strip() or "Спикер"


def _interval(segment):
    """(start_ms, end_ms) of a segment, or None when the pair is unusable.

    Anything non-integral, negative, reversed or empty is dropped: a snippet
    built from it would point at audio the segment does not describe.
    """
    start, end = segment.get("start_ms"), segment.get("end_ms")
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    start, end = int(start), int(end)
    if start < 0 or end <= start:
        return None
    return start, end


def _snippet(intervals, duration_ms=None):
    """A representative interval for one speaker, or None.

    Chooses the longest real segment — the most likely to contain a full,
    recognisable sentence — capped at MAX_SNIPPET_MS from its own start and
    clamped to the recording length. Ties break on the earliest start so the
    same input always produces the same snippet.
    """
    if not intervals:
        return None
    start, end = max(intervals, key=lambda iv: (iv[1] - iv[0], -iv[0]))
    end = min(end, start + MAX_SNIPPET_MS)
    if duration_ms and int(duration_ms) > 0:
        end = min(end, int(duration_ms))
    if end <= start:
        return None  # the segment lies outside the audio we actually have
    return {"start_ms": start, "end_ms": end}


def _labelled_segments(segments):
    """Only the segments that carry a real speaker label, in order."""
    out = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        label = segment.get("speaker")
        if not isinstance(label, str) or not label.strip():
            continue
        out.append((label.strip(), segment))
    return out


def _asr_meta_segments(asr_meta):
    """Diarization segments a future engine wrote into `asr_meta_json`.

    Narrow on purpose. `asr_meta_json` already holds the transcription *windows*
    of a long recording, which describe time, not voices; reading those as
    speakers would report "9 speakers" for a nine-window monologue. Only an
    explicit `diarization.segments` block counts, and only entries in it that
    carry a label.
    """
    if not isinstance(asr_meta, dict):
        return []
    block = asr_meta.get("diarization")
    if not isinstance(block, dict):
        return []
    segments = block.get("segments")
    return segments if isinstance(segments, list) else []


def extract_speakers(segments, duration_ms=None, names=None):
    """[speaker] for one recording's segments, in order of first appearance.

    Bounded at MAX_SPEAKERS: a corrupted label field must not turn into a
    thousand rows in the UI. The caller is told when that bound was hit.
    """
    names = names or {}
    order = []
    grouped = {}
    for label, segment in _labelled_segments(segments):
        speaker_id = source_speaker_id(label)
        if not speaker_id:
            continue
        if speaker_id not in grouped:
            order.append(speaker_id)
            grouped[speaker_id] = {"source_label": label, "intervals": [],
                                   "segment_count": 0}
        entry = grouped[speaker_id]
        entry["segment_count"] += 1
        interval = _interval(segment)
        if interval:
            entry["intervals"].append(interval)

    truncated = len(order) > MAX_SPEAKERS
    speakers = []
    for speaker_id in order[:MAX_SPEAKERS]:
        entry = grouped[speaker_id]
        display_name = names.get(speaker_id) or None
        speakers.append({
            "speaker_id": speaker_id,
            "source_label": entry["source_label"],
            "display_name": display_name,
            "name_ru": name_ru(entry["source_label"], display_name),
            "segment_count": entry["segment_count"],
            "total_ms": sum(end - start for start, end in entry["intervals"]),
            "snippet": _snippet(entry["intervals"], duration_ms),
        })
    return speakers, truncated


def speaker_model(*, segments=None, asr_meta=None, duration_ms=None, names=None,
                  transcript=None, pending=False):
    """The whole truthful speaker payload for one recording.

    `transcript` is accepted and deliberately unused: callers have it to hand,
    and taking it makes the refusal to mine prose for names explicit rather than
    an omission somebody later "fixes".
    """
    del transcript  # never evidence of who spoke; see the module docstring

    speakers, truncated = extract_speakers(segments, duration_ms, names)
    source = SOURCE_PLAUD
    if not speakers:
        meta_segments = _asr_meta_segments(asr_meta)
        speakers, truncated = extract_speakers(meta_segments, duration_ms, names)
        source = SOURCE_ASR_META if speakers else None

    if speakers:
        return {
            "state": "ready",
            "source": source,
            "reason": None,
            "count": len(speakers),
            "speakers": speakers,
            "truncated": truncated,
            "note_ru": None,
        }

    had_segments = bool(_labelled_segments(segments)) or bool(segments) or \
        bool(_asr_meta_segments(asr_meta))
    reason = "no_labels" if had_segments else "no_segments"
    return {
        "state": "pending" if pending else "unavailable",
        "source": None,
        "reason": "pending" if pending else reason,
        "count": 0,
        "speakers": [],
        "truncated": False,
        "note_ru": DIARIZATION_PENDING_RU if pending else DIARIZATION_UNAVAILABLE_RU,
    }


def name_by_id(speakers) -> dict:
    """{speaker_id: display name} for the speakers a reader has named."""
    return {s["speaker_id"]: s["display_name"]
            for s in speakers or [] if s.get("display_name")}


def display_map(speakers) -> dict:
    """{raw source label: display name}, for substitution at render time."""
    return {s["source_label"]: s["display_name"]
            for s in speakers or [] if s.get("display_name")}


def label_segments(segments, names=None):
    """Segments with `speaker_id`/`display_name` added, raw label preserved.

    Returns new dicts. The caller's list — which came straight out of
    `plaud_segments_json` — is never mutated: the raw candidate is the record,
    and the names are a view of it.
    """
    names = names or {}
    out = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        label = (segment.get("speaker") or "").strip()
        speaker_id = source_speaker_id(label) if label else ""
        out.append({
            **segment,
            "speaker_id": speaker_id or None,
            "display_name": (names.get(speaker_id) or None) if speaker_id else None,
        })
    return out


def apply_names_to_text(text, label_names):
    """Substitute assigned names into "[Speaker 1] …" transcript lines.

    Only a bracketed label at the start of a line is touched, which is exactly
    the shape the archive writes (`archive_recording._segment_text`). Prose that
    happens to mention a label is left alone, and the input string is returned
    unchanged when there is nothing to substitute — this is a presentation
    helper, never a writer.
    """
    if text is None or not label_names:
        return text
    by_id = {}
    for label, name in label_names.items():
        speaker_id = source_speaker_id(label)
        if speaker_id and name:
            by_id[speaker_id] = name

    def rewrite(line):
        match = _LINE_LABEL_RE.match(line)
        if not match:
            return line
        indent, label, rest = match.groups()
        name = by_id.get(source_speaker_id(label))
        return f"{indent}[{name}]{rest}" if name else line

    return "\n".join(rewrite(line) for line in text.split("\n"))
