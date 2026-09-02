"""Build a mind-map tree (indented text) out of what the archive already stores.

Read-only: takes the recording's markdown summary (PLAUD's own auto-summary) or,
if there is none, the stored transcript, and turns it into the two-spaces-per-level
format that mindmap.py renders. Returns None when there is not enough text — the
caller then simply shows no map.

The output is deliberately capped (~7 branches, ~26 nodes) so the PNG stays
readable on a phone.
"""
import re

MAX_BRANCHES = 7
MAX_KIDS = 4
MAX_NODES = 26
MIN_SUMMARY_CHARS = 400
ROOT_WORDS = 9
BRANCH_WORDS = 6
LEAF_WORDS = 11

# PLAUD's marker for "too short to summarise" — such a note is not a mind map
_NO_SUMMARY = re.compile(r"no summary is needed|краткое содержание не тр", re.I)

# headings PLAUD emits that carry no information on their own
_SKIP_HEADINGS = {"overview", "summary", "сводка вашей беседы", "содержание"}


def _strip_links(s):
    """Images/links only — emphasis markers stay so bullets keep their shape."""
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)            # images
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)        # links -> text
    return s


def _strip_md(s):
    s = _strip_links(s)
    s = re.sub(r"`{1,3}", "", s)
    s = re.sub(r"[*_]{1,3}", "", s)
    return s


def _clean(s):
    s = _strip_md(s)
    s = re.sub(r"^\s*\[[ xX]?\]\s*", "", s)                # to-do checkboxes
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(" \t-–—:;·•")


def _short(s, max_words):
    """Shorten to max_words words, marking the cut with an ellipsis."""
    s = _clean(s)
    if not s:
        return ""
    words = s.split()
    if len(words) <= max_words:
        return s.rstrip(".")
    return " ".join(words[:max_words]).rstrip(",.;:") + "…"


# template/meta lines PLAUD leaves in English summaries — never a mind-map node
_NOISE = re.compile(
    r"^\s*(date\s*&?\s*time|location|customer|participants|attendees|дата|место)\s*:",
    re.I,
)


def _drop_noise(lines):
    return [l for l in lines if not _NOISE.match(l) and "[insert" not in l.lower()]


def _sentences(text):
    parts = re.split(r"(?<=[.!?])\s+", _clean(text))
    return [p for p in (p.strip() for p in parts) if len(p) > 12]


_NUM = re.compile(r"\d")


def _pick_sentences(text, limit):
    """First sentence + the most concrete ones (numbers/decisions) after it."""
    sents = _sentences(text)
    if not sents:
        return []
    picked = [sents[0]]
    rest = sents[1:]
    hot = [s for s in rest if _NUM.search(s)]
    for s in hot:
        if len(picked) >= limit:
            break
        picked.append(s)
    for s in rest:
        if len(picked) >= limit:
            break
        if s not in picked:
            picked.append(s)
    return picked[:limit]


def _blocks(md):
    """Split the summary into (heading|None, body-lines) blocks."""
    md = _strip_links(md)
    md = re.sub(r"^\s*-{3,}\s*$", "", md, flags=re.M)     # PLAUD's ---- rules
    blocks, cur = [], (None, [])
    for raw in md.split("\n"):
        line = raw.rstrip()
        m = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
        if m:
            blocks.append(cur)
            cur = (m.group(1).strip(), [])
        else:
            cur[1].append(line)
    blocks.append(cur)
    return [(h, b) for h, b in blocks if h or any(x.strip() for x in b)]


def _body_children(lines, budget):
    """Bullets first (they are already the distilled points); else key sentences."""
    lines = _drop_noise(lines)
    text = "\n".join(lines)
    bullets, quotes = [], []
    for line in lines:
        m = re.match(r"^\s{0,4}(?:[-*+]|\d+\.)\s+(.*)$", line)
        if m and m.group(1).strip():
            bullets.append(m.group(1).strip())
            continue
        m = re.match(r"^\s{0,3}>\s*(.*)$", line)
        if m and m.group(1).strip():
            quotes.append(m.group(1).strip())
    if bullets:
        return [_short(b, LEAF_WORDS) for b in bullets[:budget]]
    if quotes:
        return [_short(q.strip('"«»'), LEAF_WORDS) for q in quotes[:budget]]
    prose = "\n".join(l for l in text.split("\n") if not l.strip().startswith(">"))
    return [_short(s, LEAF_WORDS) for s in _pick_sentences(prose, budget)]


def _from_summary(name, summary):
    blocks = _blocks(summary)
    if not blocks:
        return None
    lead = ""
    branches = []
    for head, lines in blocks:
        if head is None:
            lead = "\n".join(_drop_noise(lines))
            continue
        if _clean(head).lower() in _SKIP_HEADINGS:
            lead = lead or "\n".join(_drop_noise(lines))
            continue
        branches.append((head, lines))

    nodes = []          # list of (branch_label, [kids])
    if lead.strip():
        kids = [_short(s, LEAF_WORDS) for s in _pick_sentences(lead, 2 if branches else 4)]
        kids = [k for k in kids if k]
        if kids:
            nodes.append(("О чём", kids))
    for head, lines in branches[:MAX_BRANCHES - len(nodes)]:
        kids = [k for k in _body_children(lines, MAX_KIDS) if k]
        nodes.append((_short(head, BRANCH_WORDS), kids))

    if not nodes:
        return None
    # no headings at all -> promote the lead points to branches
    if len(nodes) == 1 and nodes[0][0] == "О чём" and len(nodes[0][1]) >= 3:
        nodes = [(k, []) for k in nodes[0][1][:MAX_BRANCHES]]
    return nodes


def _from_transcript(transcript):
    """Fallback for recordings PLAUD never summarised: sample the conversation
    in time order so the map still shows the shape of it."""
    text = re.sub(r"^\[[^\]]{0,40}\]\s*", "", transcript, flags=re.M)
    sents = _sentences(text)
    if len(sents) < 6:
        return None
    n = min(MAX_BRANCHES, 5)
    step = max(1, len(sents) // n)
    nodes = []
    for i in range(n):
        chunk = sents[i * step:(i + 1) * step] or sents[-1:]
        chunk = sorted(chunk, key=len, reverse=True)[:MAX_KIDS - 1]
        label = _short(chunk[0], BRANCH_WORDS) if chunk else ""
        kids = [_short(c, LEAF_WORDS) for c in chunk[1:]]
        if label:
            nodes.append((label, [k for k in kids if k]))
    return nodes or None


def root_label(name):
    n = _clean(name or "") or "Запись"
    n = re.sub(r"^\d{2}-\d{2}\s+", "", n)                       # "08-05 …"
    n = re.sub(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$", "Запись", n)
    return _short(n, ROOT_WORDS) or "Запись"


def _item_text(item):
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    text = next((item.get(k) for k in ("task", "text", "title", "decision", "risk", "question", "summary") if item.get(k)), "")
    meta = [item.get("owner"), item.get("due") or item.get("deadline")]
    suffix = " · ".join(str(x).strip() for x in meta if x)
    return f"{text} ({suffix})" if text and suffix else str(text or "").strip()


def _structured_nodes(data):
    """Convert generated summary_json into Russian-labelled nodes."""
    if not isinstance(data, dict):
        return []
    nodes = []
    overview = [data.get("overview"), data.get("brief_summary")]
    overview = [_short(str(x), LEAF_WORDS) for x in overview if x]
    if overview:
        nodes.append(("Общий вывод", overview[:2]))

    themes = data.get("themes") or []
    for theme in themes if isinstance(themes, list) else []:
        if isinstance(theme, str):
            nodes.append((_short(theme, BRANCH_WORDS), []))
            continue
        if not isinstance(theme, dict):
            continue
        label = _short(str(theme.get("title") or theme.get("name") or "Тема"), BRANCH_WORDS)
        raw_kids = theme.get("points") or theme.get("key_points") or []
        if not isinstance(raw_kids, list):
            raw_kids = [raw_kids]
        if theme.get("summary"):
            raw_kids = [theme["summary"], *raw_kids]
        kids = [_short(_item_text(x), LEAF_WORDS) for x in raw_kids]
        nodes.append((label, [x for x in kids if x][:MAX_KIDS]))

    for key, label in (
        ("decisions", "Решения"),
        ("action_items", "Задачи"),
        ("risks", "Риски"),
        ("open_questions", "Открытые вопросы"),
    ):
        raw = data.get(key) or []
        if not isinstance(raw, list):
            raw = [raw]
        kids = [_short(_item_text(x), LEAF_WORDS) for x in raw]
        kids = [x for x in kids if x][:MAX_KIDS]
        if kids:
            nodes.append((label, kids))
    return nodes


def _render_nodes(name, nodes):
    if not nodes:
        return None
    lines = [root_label(name)]
    total = 1
    for label, kids in nodes[:MAX_BRANCHES]:
        if not label or total >= MAX_NODES:
            break
        lines.append("  " + label)
        total += 1
        for k in kids:
            if total >= MAX_NODES:
                break
            lines.append("    " + k)
            total += 1
    return "\n".join(lines) if total >= 3 else None


def build_structured_tree(name, data, transcript=None):
    """Build from generated summary_json; transcript is fallback only."""
    tree = _render_nodes(name, _structured_nodes(data))
    return tree or build_tree(name, None, transcript)


def build_tree(name, summary=None, transcript=None):
    """Return indented mind-map text, or None if there is nothing to draw."""
    summary = (summary or "").strip()
    transcript = (transcript or "").strip()
    nodes = None
    if len(_clean(summary)) >= MIN_SUMMARY_CHARS and not _NO_SUMMARY.search(summary):
        nodes = _from_summary(name, summary)
    if not nodes and len(_clean(transcript)) >= 800:
        nodes = _from_transcript(transcript)
    if not nodes:
        return None

    return _render_nodes(name, nodes)
