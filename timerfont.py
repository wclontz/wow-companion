"""Reads the tiny buff-timer text ("9 m", "43 m", "45 s", "1 h") with a glyph font learned from
real screenshots of your buff bar. Windows OCR can't read text this small."""
import json
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_FILE = os.path.join(HERE, "timerfont.json")


def ink(rgb):
    """Gold timer text (yellow-orange, dark outline) -> bool mask."""
    r, g, b = rgb[..., 0].astype(int), rgb[..., 1].astype(int), rgb[..., 2].astype(int)
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    white = (mn > 135) & (mx - mn < 40)                       # under 1 minute the text turns white
    gold = ((r > 185) & (g > 135) & (b < 70)) | white
    # the text has a black outline; sunlit ground of the same colour doesn't. Keep gold pixels
    # within 2 px of a dark pixel.
    dark = (r + g + b) < 150
    near = dark.copy()
    for _ in range(2):
        n = near.copy()
        n[1:, :] |= near[:-1, :]; n[:-1, :] |= near[1:, :]
        n[:, 1:] |= near[:, :-1]; n[:, :-1] |= near[:, 1:]
        near = n
    return gold & near


def is_white(rgb):
    r, g, b = rgb[..., 0].astype(int), rgb[..., 1].astype(int), rgb[..., 2].astype(int)
    mx = np.maximum(np.maximum(r, g), b); mn = np.minimum(np.minimum(r, g), b)
    white = ((mn > 135) & (mx - mn < 40)).sum()
    gold = ((r > 185) & (g > 135) & (b < 70)).sum()
    return white > gold


def glyphs(rgb):
    """Split a timer crop into glyph bitmaps, left to right. Returns [(x0, bitmap)].
    Lowercase letters (shorter than digits) that come apart at a 1-px gap, like 'm', are rejoined."""
    out = _runs(ink(rgb))
    merged = []
    for x0, x1, bm in out:
        if merged and x0 - merged[-1][1] <= 2 and bm.shape[0] <= 6 and merged[-1][2].shape[0] <= 6:
            px0, px1, pbm = merged[-1]
            m = ink(rgb)[:, px0:x1]
            rows = np.nonzero(m.any(axis=1))[0]
            merged[-1] = (px0, x1, m[rows[0]:rows[-1] + 1])
        else:
            merged.append((x0, x1, bm))
    return [(x0, bm) for x0, x1, bm in merged]


def _runs(m):
    cols = m.any(axis=0)
    out, x = [], 0
    while x < len(cols):
        if not cols[x]:
            x += 1
            continue
        x0 = x
        while x < len(cols) and cols[x]:
            x += 1
        g = m[:, x0:x]
        rows = np.nonzero(g.any(axis=1))[0]
        if len(rows) and g.sum() >= 3:
            out.append((x0, x, g[rows[0]:rows[-1] + 1]))
    return out


def key(bm):
    return "|".join("".join("#" if v else "." for v in row) for row in bm)


def from_key(k):
    return np.array([[c == "#" for c in row] for row in k.split("|")])


def load_font():
    try:
        return {k: v for k, v in json.load(open(FONT_FILE, encoding="utf-8")).items()}
    except (OSError, ValueError):
        return {}


def _dist(a, b):
    """Shape difference (0 = same), trying 1-pixel shifts: WoW renders a digit a pixel differently
    depending on exactly where it lands on screen."""
    h, w = max(a.shape[0], b.shape[0]) + 2, max(a.shape[1], b.shape[1]) + 2
    pa = np.zeros((h, w), bool)
    pa[1:1 + a.shape[0], 1:1 + a.shape[1]] = a
    best = 1.0
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            if dy + b.shape[0] > h or dx + b.shape[1] > w:
                continue
            pb = np.zeros((h, w), bool)
            pb[dy:dy + b.shape[0], dx:dx + b.shape[1]] = b
            union = (pa | pb).sum()
            if union:
                best = min(best, (pa ^ pb).sum() / union)
    # very different widths/heights are different characters whatever the overlap says
    if abs(a.shape[0] - b.shape[0]) > 2 or abs(a.shape[1] - b.shape[1]) > 2:
        best = max(best, 0.9)
    return best


def read_parts(rgb, font):
    """-> (text of recognised chars, [unknown glyph keys]) for self-teaching."""
    temps = [(from_key(k), ch) for k, ch in font.items()]
    text, unknown = "", []
    for x0, bm in glyphs(rgb):
        best = min(temps, key=lambda t: _dist(bm, t[0])) if temps else None
        if best and _dist(bm, best[0]) <= 0.45:
            text += best[1]
        elif bm.sum() >= 6:
            unknown.append(key(bm))
    return text, unknown


def teach(k, ch):
    font = load_font()
    font[k] = ch
    json.dump(font, open(FONT_FILE, "w", encoding="utf-8"), indent=0)
    return font


def read(rgb, font):
    """-> seconds left (int) or None."""
    if not font:
        return None
    temps = [(from_key(k), ch) for k, ch in font.items()]
    text = ""
    for x0, bm in glyphs(rgb):
        best = min(temps, key=lambda t: _dist(bm, t[0]))
        if _dist(bm, best[0]) <= 0.45:
            text += best[1]                          # noise blobs that match nothing are skipped
    import re
    m = re.search(r"(\d{1,2})([hms])", text)
    if not m:
        return None
    return int(m.group(1)) * {"h": 3600, "m": 60, "s": 1}[m.group(2)]
