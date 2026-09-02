#!/usr/bin/env python3
"""Jazzuu's local mind map renderer. Outputs PNG or SVG.

This implementation is maintained in this repository and distributed under
Jazzuu's MIT license. Pillow is installed from viewer/requirements.txt; the
bundled DejaVu fonts retain their own license in ./fonts/LICENSE-DejaVu.txt.

Input: indented plain text on stdin or via --file. Two spaces (or a tab)
per level. First non-indented line is the root.

    Встреча с VP Mastercard
      Участники
        VP Mastercard, консалтинг СНГ
      Задачи
        Интро на country manager (Алматы)

Usage:
    python3 -m viewer.app.mindmap --title "Тема" --out mindmap.png --file tree.txt
    echo "..." | python3 -m viewer.app.mindmap

Format is chosen by the --out extension (.png default, .svg supported).
Pillow and the bundled fonts make rendering independent of system fonts.
"""
import argparse
import html
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vendor"))

OUT_DIR = os.environ.get("MINDMAP_DIR", "/cache/mindmaps")
FONT_REG = os.path.join(HERE, "fonts", "DejaVuSans.ttf")
FONT_BOLD = os.path.join(HERE, "fonts", "DejaVuSans-Bold.ttf")

# Level palette: root, branch, sub-branch, leaf, deeper
COLORS = ["#111827", "#1d4ed8", "#0e7490", "#7c3aed", "#be185d"]
TINTS = ["#111827", "#eff6ff", "#ecfeff", "#f5f3ff", "#fdf2f8"]

SCALE = 2          # render at 2x for a crisp result on phone screens
FS = [22, 18, 15, 14, 14]   # font size per level
PAD_X = 14
PAD_Y = 9
GAP_Y = 12
GAP_X = 54
WRAP_PX = [520, 420, 380, 340, 340]   # max text width per level


def load_fonts():
    from PIL import ImageFont
    reg = {s: ImageFont.truetype(FONT_REG, s) for s in set(FS)}
    bold = {s: ImageFont.truetype(FONT_BOLD, s) for s in set(FS) | {26}}
    return reg, bold


class Node:
    def __init__(self, text, level):
        self.text = text
        self.level = level
        self.kids = []
        self.lines = []
        self.w = self.h = 0.0
        self.x = self.y = 0.0
        self.subtree_h = 0.0

    def font(self, reg, bold):
        lvl = min(self.level, len(FS) - 1)
        size = FS[lvl]
        return (bold if self.level <= 1 else reg)[size]

    def layout(self, reg, bold, measure):
        lvl = min(self.level, len(FS) - 1)
        f = self.font(reg, bold)
        self.lines = wrap_px(self.text, f, WRAP_PX[lvl], measure)
        line_h = FS[lvl] + 8
        self.w = max(measure(l, f) for l in self.lines) + 2 * PAD_X
        self.h = len(self.lines) * line_h + 2 * PAD_Y
        self.line_h = line_h
        for k in self.kids:
            k.layout(reg, bold, measure)


def wrap_px(text, font, max_w, measure):
    words, lines, cur = text.split(), [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if measure(cand, font) > max_w and cur:
            lines.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines or [" "]


def parse(text):
    root, stack = None, []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        expanded = raw.replace("\t", "  ")
        indent = len(expanded) - len(expanded.lstrip(" "))
        label = expanded.strip().lstrip("-*• ").strip()
        if not label:
            continue
        level = indent // 2
        node = Node(label, level)
        if root is None:
            node.level = 0
            root = node
            stack = [root]
            continue
        level = max(1, min(level, len(stack)))
        stack[level - 1].kids.append(node)
        node.level = level
        stack = stack[:level] + [node]
    return root


def measure_tree(n):
    if not n.kids:
        n.subtree_h = n.h
    else:
        n.subtree_h = max(n.h, sum(measure_tree(k) for k in n.kids) + GAP_Y * (len(n.kids) - 1))
    return n.subtree_h


def place(n, x, y_top):
    n.x = x
    n.y = y_top + n.subtree_h / 2 - n.h / 2
    cy = y_top
    for k in n.kids:
        place(k, x + n.w + GAP_X, cy)
        cy += k.subtree_h + GAP_Y


def collect(n, acc):
    acc.append(n)
    for k in n.kids:
        collect(k, acc)
    return acc


def build(text, title):
    from PIL import ImageDraw, Image
    reg, bold = load_fonts()
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    def measure(s, f):
        return probe.textlength(s, font=f)

    root = parse(text)
    if root is None:
        sys.exit("empty input")
    root.layout(reg, bold, measure)
    measure_tree(root)
    place(root, 0, 0)
    nodes = collect(root, [])
    return root, nodes, reg, bold


def bezier(p0, p1, p2, p3, steps=24):
    pts = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        x = u**3 * p0[0] + 3 * u*u*t * p1[0] + 3 * u*t*t * p2[0] + t**3 * p3[0]
        y = u**3 * p0[1] + 3 * u*u*t * p1[1] + 3 * u*t*t * p2[1] + t**3 * p3[1]
        pts.append((x, y))
    return pts


def render_png(text, title, out):
    from PIL import Image, ImageDraw, ImageFont
    root, nodes, reg, bold = build(text, title)

    margin = 28
    top = margin + (48 if title else 0)
    W = int(max(n.x + n.w for n in nodes) + 2 * margin)
    H = int(max(n.y + n.h for n in nodes) + top + margin)

    im = Image.new("RGB", (W * SCALE, H * SCALE), "#ffffff")
    d = ImageDraw.Draw(im)

    def S(v):
        return v * SCALE

    if title:
        d.text((S(margin), S(margin - 6)), title,
               font=ImageFont.truetype(FONT_BOLD, 26 * SCALE), fill="#111827")

    for n in nodes:
        for k in n.kids:
            x1, y1 = n.x + n.w + margin, n.y + n.h / 2 + top
            x2, y2 = k.x + margin, k.y + k.h / 2 + top
            mx = (x1 + x2) / 2
            color = COLORS[min(k.level, len(COLORS) - 1)]
            pts = [(S(px), S(py)) for px, py in
                   bezier((x1, y1), (mx, y1), (mx, y2), (x2, y2))]
            d.line(pts, fill=color, width=max(2, SCALE * 2), joint="curve")

    for n in nodes:
        lvl = min(n.level, len(COLORS) - 1)
        color = COLORS[lvl]
        fill = TINTS[lvl]
        text_fill = "#ffffff" if n.level == 0 else "#111827"
        x0, y0 = S(n.x + margin), S(n.y + top)
        x1, y1 = S(n.x + n.w + margin), S(n.y + n.h + top)
        d.rounded_rectangle([x0, y0, x1, y1], radius=S(10), fill=fill,
                            outline=color, width=max(2, int(SCALE * 1.5)))
        f = n.font(reg, bold)
        f = ImageFont.truetype(FONT_BOLD if n.level <= 1 else FONT_REG,
                               FS[min(n.level, len(FS) - 1)] * SCALE)
        for i, line in enumerate(n.lines):
            d.text((x0 + S(PAD_X), y0 + S(PAD_Y + n.line_h * i)), line,
                   font=f, fill=text_fill)

    im.save(out, "PNG", optimize=True)


def render_svg(text, title, out):
    root, nodes, reg, bold = build(text, title)
    margin = 28
    top = margin + (48 if title else 0)
    W = max(n.x + n.w for n in nodes) + 2 * margin
    H = max(n.y + n.h for n in nodes) + top + margin
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" '
        f'viewBox="0 0 {W:.0f} {H:.0f}" font-family="DejaVu Sans, Arial, sans-serif">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
    ]
    if title:
        parts.append(f'<text x="{margin}" y="{margin + 20}" font-size="26" font-weight="700" '
                     f'fill="#111827">{html.escape(title)}</text>')
    for n in nodes:
        for k in n.kids:
            x1, y1 = n.x + n.w + margin, n.y + n.h / 2 + top
            x2, y2 = k.x + margin, k.y + k.h / 2 + top
            mx = (x1 + x2) / 2
            color = COLORS[min(k.level, len(COLORS) - 1)]
            parts.append(f'<path d="M{x1:.1f},{y1:.1f} C{mx:.1f},{y1:.1f} {mx:.1f},{y2:.1f} '
                         f'{x2:.1f},{y2:.1f}" fill="none" stroke="{color}" stroke-width="2"/>')
    for n in nodes:
        lvl = min(n.level, len(COLORS) - 1)
        color, fill = COLORS[lvl], TINTS[lvl]
        text_fill = "#ffffff" if n.level == 0 else "#111827"
        weight = "700" if n.level <= 1 else "400"
        size = FS[min(n.level, len(FS) - 1)]
        parts.append(f'<rect x="{n.x + margin:.1f}" y="{n.y + top:.1f}" width="{n.w:.1f}" '
                     f'height="{n.h:.1f}" rx="10" fill="{fill}" stroke="{color}" stroke-width="1.6"/>')
        for i, line in enumerate(n.lines):
            parts.append(f'<text x="{n.x + margin + PAD_X:.1f}" '
                         f'y="{n.y + top + PAD_Y + n.line_h * i + size:.1f}" font-size="{size}" '
                         f'font-weight="{weight}" fill="{text_fill}">{html.escape(line)}</text>')
    parts.append("</svg>")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="indented text file (default: stdin)")
    ap.add_argument("--title")
    ap.add_argument("--out", help="output path; .png (default) or .svg")
    a = ap.parse_args()

    text = open(a.file, encoding="utf-8").read() if a.file else sys.stdin.read()
    out = a.out or os.path.join(OUT_DIR, f"mindmap_{time.strftime('%Y%m%d_%H%M%S')}.png")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    if out.lower().endswith(".svg"):
        render_svg(text, a.title, out)
    else:
        render_png(text, a.title, out)
    print(out)


if __name__ == "__main__":
    main()
