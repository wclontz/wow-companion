"""Live location (zone + map coordinates) read off the minimap, and zone-map snapshots
taken from the in-game Map & Quest Log window."""
import json
import os
import re
import threading
import time

import numpy as np
from PIL import Image

import fastgrab

from quests import ocr

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_DIR = os.path.join(HERE, "maps")
COORD_REGION = (1735, 258, 1850, 279)        # "60.3, 72.5" under the minimap (1920x1080)
ZONE_REGION = (1712, 2, 1845, 20)            # zone name above the minimap
# every open-world zone / city map (the minimap label shows these names when you're not in a named area)
ZONES = (
    "Alterac Mountains", "Arathi Highlands", "Badlands", "Blasted Lands", "Burning Steppes", "Deadwind Pass",
    "Dun Morogh", "Duskwood", "Eastern Plaguelands", "Elwynn Forest", "Hillsbrad Foothills", "Ironforge",
    "Loch Modan", "Redridge Mountains", "Searing Gorge", "Silverpine Forest", "Stormwind City",
    "Stranglethorn Vale", "Swamp of Sorrows", "The Hinterlands", "Tirisfal Glades", "Undercity",
    "Western Plaguelands", "Westfall", "Wetlands",
    "Ashenvale", "Azshara", "Darkshore", "Darnassus", "Desolace", "Durotar", "Dustwallow Marsh", "Felwood",
    "Feralas", "Moonglade", "Mulgore", "Orgrimmar", "Silithus", "Stonetalon Mountains", "Tanaris", "Teldrassil",
    "The Barrens", "Thousand Needles", "Thunder Bluff", "Un'Goro Crater", "Winterspring",
    "Eversong Woods", "Ghostlands", "Silvermoon City", "Azuremyst Isle", "Bloodmyst Isle", "The Exodar",
)
COORD_RE = re.compile(r"(\d{1,2}(?:[.,]\d)?)\D{1,3}(\d{1,2}(?:[.,]\d)?)")
# Map & Quest Log: map canvas and breadcrumb relative to the window-title text
CANVAS_OFF = (-472.5, 59.5, 217.5, 521.5)    # x0, y0, x1, y1
CRUMB_OFF = (-412, 24, 172, 46)             # the whole "World > Eastern Kingdoms > Zone > ..." bar


# what Windows OCR makes of some names, every time (this font's l' and the cut-off "..." at the bar's end)
LABEL_FIX = {
    "stenderrs pond": "Stendel's Pond", "stende(ts pond": "Stendel's Pond", "stendel is pond": "Stendel's Pond",
    "stendel rs pond": "Stendel's Pond", "stendelrs pond": "Stendel's Pond", "stendel's pond": "Stendel's Pond",
    "the maclure vineya": "The Maclure Vineyards", "the maclure vineya...": "The Maclure Vineyards",
    "westfarr": "Westfall", "westfajj": "Westfall", "moonbroolc": "Moonbrook", "sentlnel hiii": "Sentinel Hill",
    "the bead aere": "The Dead Acre", "ajexston farmstead": "Alexston Farmstead", "the molsen farm": "The Molsen Farm",
}


LABEL_DIR = os.path.join(HERE, "label_images")
_GALLERY = None


def label_bits(img):
    """The label's text as a picture: label-coloured pixels, cut to the text, scaled to 16 px high.
    -> float array, or None if there's no text."""
    a = np.asarray(img.convert("RGB")).astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    t = ((r > 150) & (g > 110) & (b < 120)) | ((g > 150) & (r < 150) & (b < 130)) | ((r > 170) & (g < 90) & (b < 90))
    ys, xs = np.nonzero(t)
    if len(xs) < 20:
        return None
    t = t[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h = 16
    w = max(8, int(round(t.shape[1] * h / t.shape[0])))
    return np.asarray(Image.fromarray((t * 255).astype("uint8")).resize((w, h), Image.BILINEAR), dtype=float) / 255


def _gallery():
    global _GALLERY
    if _GALLERY is None:
        _GALLERY = {}
        try:
            idx = json.load(open(os.path.join(LABEL_DIR, "index.json"), encoding="utf-8"))
        except (OSError, ValueError):
            idx = {}
        for f, name in idx.items():
            try:
                _GALLERY.setdefault(name, []).append(np.asarray(Image.open(os.path.join(LABEL_DIR, f)), dtype=float) / 255)
            except OSError:
                pass
    return _GALLERY


def match_label_image(bits):
    """Best (name, score) among the saved label pictures (same text = same picture, whatever OCR says)."""
    best = (None, 0.0)
    if bits is None:
        return best
    for name, pics in _gallery().items():
        for p in pics:
            if abs(p.shape[1] - bits.shape[1]) > 0.12 * p.shape[1]:
                continue                                  # a different length of text
            q = np.asarray(Image.fromarray((bits * 255).astype("uint8")).resize((p.shape[1], p.shape[0]), Image.BILINEAR), dtype=float) / 255
            x, y = p - p.mean(), q - q.mean()
            c = float((x * y).sum() / (np.sqrt((x * x).sum() * (y * y).sum()) + 1e-9))
            if c > best[1]:
                best = (name, c)
    return best


def remember_label_image(img, name):
    """Keep up to 3 pictures of each confirmed label name."""
    bits = label_bits(img)
    if bits is None or not name:
        return
    g = _gallery()
    pics = g.setdefault(name, [])
    if len(pics) >= 3:
        return
    if pics and (match_label_image(bits)[1] > 0.97 and match_label_image(bits)[0] == name):
        return                                            # we already have this exact picture
    os.makedirs(LABEL_DIR, exist_ok=True)
    try:
        with open(os.path.join(LABEL_DIR, "index.json"), encoding="utf-8") as _f:
            idx = json.load(_f)
    except (OSError, ValueError):
        idx = {}
    f = f"{safe_name(name) or 'label'}_{len(idx) + 1}.png"
    Image.fromarray((bits * 255).astype("uint8")).save(os.path.join(LABEL_DIR, f))
    idx[f] = name
    with open(os.path.join(LABEL_DIR, "index.json"), "w", encoding="utf-8") as _f:
        json.dump(idx, _f, indent=1)
    pics.append(bits)


FIX_FILE = os.path.join(HERE, "label_fixes.json")      # the user's own corrections (HUD ✎)


def fix_label(name):
    try:
        with open(FIX_FILE, encoding="utf-8") as _f:
            user = json.load(_f)
    except (OSError, ValueError):
        user = {}
    key = (name or "").strip().lower()
    return user.get(key) or LABEL_FIX.get(key, name)


def rename_label(old, new):
    """The user's correction: every saved picture of `old` is now `new`, and a reading of `old` becomes `new`."""
    global _GALLERY
    old, new = (old or "").strip(), (new or "").strip()
    if not old or not new or old == new:
        return False
    try:
        with open(FIX_FILE, encoding="utf-8") as _f:
            user = json.load(_f)
    except (OSError, ValueError):
        user = {}
    user[old.lower()] = new
    for k, v in list(user.items()):               # earlier corrections that pointed at the old name
        if v == old:
            user[k] = new
    with open(FIX_FILE, "w", encoding="utf-8") as _f:
        json.dump(user, _f, indent=1, sort_keys=True)
    try:
        p = os.path.join(LABEL_DIR, "index.json")
        with open(p, encoding="utf-8") as _f:
            idx = json.load(_f)
        for f, n in idx.items():
            if n == old:
                idx[f] = new
        with open(p, "w", encoding="utf-8") as _f:
            json.dump(idx, _f, indent=1)
    except (OSError, ValueError):
        pass
    _GALLERY = None
    return True


def read_label(img):
    """The zone/area name above the minimap. Windows OCR reads this thin text differently at every
    setting ('Westfarr', 'West far r', 'Westfall'), so it's read four ways and snapped to a known zone
    or learned area name when one clearly fits; otherwise the most common raw reading."""
    import json
    from difflib import SequenceMatcher
    name, score = match_label_image(label_bits(img))
    if name and score >= 0.92:
        return name                                       # the picture of the text beats OCR
    texts = []
    for sc, bold in ((3, 0), (3, 5), (2, 3), (4, 5)):
        t = " ".join(ln["text"] for ln in ocr(label_mask(img, sc, bold), False)).strip()
        t = re.sub(r"\s*\d{1,2}:\d{2}.*$", "", t)            # the clock, if it crept in
        if len(t) >= 3:
            texts.append(t)
    if not texts:
        return ""
    names = {z.lower(): z for z in ZONES}
    try:
        for k in json.load(open(os.path.join(HERE, "subzones.json"), encoding="utf-8")):
            names.setdefault(k.lower(), " ".join(w if w in ("of", "the", "and") else w[:1].upper() + w[1:]
                                                  for w in k.split()))
    except (OSError, ValueError):
        pass
    sq = lambda s: re.sub(r"[^a-z']", "", s.lower())
    score = {k: max(SequenceMatcher(None, sq(t), sq(k)).ratio() for t in texts) for k in names}
    ranked = sorted(score, key=score.get, reverse=True)
    if ranked and score[ranked[0]] >= 0.8 and (len(ranked) < 2 or score[ranked[0]] - score[ranked[1]] >= 0.1):
        return fix_label(names[ranked[0]])
    return fix_label(max(set(texts), key=texts.count))


def label_mask(img, scale=3, bold=0):
    """The minimap zone label: gold (contested), green (friendly) or red (hostile) text on a dark bar."""
    a = np.asarray(img.convert("RGB")).astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    t = ((r > 150) & (g > 110) & (b < 120)) | ((g > 150) & (r < 150) & (b < 130)) | ((r > 170) & (g < 90) & (b < 90))
    m = Image.fromarray(np.where(t, 0, 255).astype("uint8"))
    m = m.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    if bold:
        from PIL import ImageFilter
        m = m.filter(ImageFilter.MinFilter(bold))
    m = m.convert("RGB")
    pad = Image.new("RGB", (m.width + 60, m.height + 40), "white")
    pad.paste(m, (30, 20))
    return pad


COORD_FULL = re.compile(r"\s*(\d{1,2}[.,]\d)\s*[.,]?\s*(\d{1,2}[.,]\d)\s*")


def white_mask(img, scale=3, bold=3, blur=0):
    """The coords under the minimap: white text, drawn over sky OR bright ground. Brightness alone
    can't separate white text from a sunlit yellow field; 'bright AND colourless' can."""
    from PIL import ImageFilter
    a = np.asarray(img.convert("RGB")).astype(int)
    mn, mx = a.min(axis=2), a.max(axis=2)
    t = (mn > 170) & (mx - mn < 45)
    m = Image.fromarray(np.where(t, 0, 255).astype("uint8"))
    m = m.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    if bold:
        m = m.filter(ImageFilter.MinFilter(bold))
    if blur:
        m = m.filter(ImageFilter.GaussianBlur(blur))
    m = m.convert("RGB")
    pad = Image.new("RGB", (m.width + 60, m.height + 40), "white")
    pad.paste(m, (30, 20))
    return pad


def read_coords(img):
    """(x, y) map % from a crop of the coords text, or None. Windows OCR misreads single digits now and
    then (a 9 as 0, a 5 as 'J'), each image setting differently, so it's read three ways and only a
    result that at least two agree on exactly - and that is nothing but two 'NN.N' numbers - counts."""
    from quests import ocr
    votes = {}
    for sc, bold, blur in ((3, 3, 0), (2, 3, 0), (4, 3, 1)):
        txt = " ".join(ln["text"] for ln in ocr(white_mask(img, sc, bold, blur), False))
        m = COORD_FULL.fullmatch(txt)
        if not m:
            continue
        xy = tuple(float(v.replace(",", ".")) for v in m.groups())
        votes[xy] = votes.get(xy, 0) + 1
        if votes[xy] >= 2:
            return xy if all(0 <= v <= 100 for v in xy) else None
    return None


def text_mask(img, thr=110, scale=2, any_colour=False):
    """Light text on a dark background -> black on white, enlarged, with a margin for OCR.
    any_colour: bright text of any hue (the minimap label is green/yellow/red by zone type)."""
    a = np.asarray(img.convert("RGB")).astype(int)
    t = (a.max(axis=2) > thr) if any_colour else ((a[..., 0] > thr) & (a[..., 1] > thr - 20))
    m = Image.fromarray(np.where(t, 0, 255).astype("uint8")).convert("RGB")
    m = m.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    pad = Image.new("RGB", (m.width + 60, m.height + 40), "white")
    pad.paste(m, (30, 20))
    return pad


def safe_name(zone):
    return re.sub(r"[^A-Za-z0-9' -]", "", zone).strip().replace(" ", "_")


def map_file(zone):
    return os.path.join(MAP_DIR, safe_name(zone) + ".png") if zone else None


class LocationReader(threading.Thread):
    def __init__(self, screen, every=1.0):
        super().__init__(daemon=True)
        self.screen, self.every = screen, every
        self.zone, self.x, self.y, self.t = None, None, None, 0
        self.history = []                    # [t, x, y] in map percent, this map
        self._zone_t = 0
        self.map = None                      # zone map name from the combat log (MAP_CHANGE)
        self._jump = None                    # a big coordinate jump waiting for confirmation
        self.level = None                    # character level read off the portrait (confirmed)
        self._zone_cand = None
        self._level_seen = None
        self.target_level = None             # level shown on the target frame (None = no target / unreadable)

    def set_map(self, name):
        if name and name != self.map:
            self.map, self.history = name, []

    def snapshot(self):
        return {"subzone": self.zone, "zone": self.map or self.zone, "x": self.x, "y": self.y, "t": self.t,
                "trail": self.history[-600:]}

    def run(self):
        while True:
            try:
                st = self.screen.snapshot()
                if st.get("ok"):
                    from screenread import find_window
                    _, rect = find_window("World of Warcraft")
                    if rect:
                        self._read(rect)
                else:
                    # the screen readers don't know this layout (another character's) or the UI is covered:
                    # the add-on's strip alone still gives the name, level and coords
                    import beacon
                    if beacon.latest():
                        self._read(None)
            except Exception:
                pass
            time.sleep(self.every)

    def _read_level(self, rect):
        """The number on the portrait. Tries a few image settings; needs two matching reads."""
        from screenread import region
        img = self._grab(rect, region("level"))
        for thr, sc in ((150, 4), (190, 4), (170, 4), (130, 5)):
            lv = _level_from([ln["text"] for ln in ocr(text_mask(img, thr, sc), False)])
            if lv:
                break
        else:
            lv = read_small_level(img)   # a lone digit (levels 1-9 on the 2026-11 UI): OCR needs the "Level" prefix
        if lv:
            if lv == self._level_seen:
                self.level = lv
            self._level_seen = lv

    def _grab(self, rect, region):
        wx, wy, wr, wb = rect
        sx, sy = (wr - wx) / 1920, (wb - wy) / 1080
        x0, y0, x1, y1 = region
        box = (wx + int(x0 * sx), wy + int(y0 * sy), wx + int(x1 * sx), wy + int(y1 * sy))
        # just the box, and never PIL's ImageGrab: its failed grabs leak a bitmap each (see fastgrab.py)
        return fastgrab.screen(box)

    def _read(self, rect):
        now = time.time()
        # the add-on's strip (beacon.py) has the name above the minimap, your level and the target's level as
        # the game has them: with a fresh reading nothing is read off the pictures (they stay as the fallback)
        try:
            import beacon
            bc = beacon.latest()
        except Exception:
            bc = None
        exact = (bc.get("stats") if bc else None) or None
        name = ((bc.get("texts") or {}).get("label") or "").strip() if bc else ""
        if len(name) >= 3:
            if name != self.zone:
                self.zone_changed_t = now
            self.zone, self.zone_ok_t, self._zone_cand, self._zone_t = name, now, name, now
        if exact:
            self.level = self._level_seen = exact["level"]
            self._level_t = now
        if rect is None:                                         # strip only: nothing is read off the pictures
            if exact:
                tl = exact.get("target_level")
                self.target_level = tl if tl and tl > 0 else None
            mp = bc.get("map_pos") if bc else None
            if mp:
                self._coords(round(mp[0] * 100, 1), round(mp[1] * 100, 1), now)
            return
        if now - self._zone_t > 5 or self.zone is None:          # zone changes rarely
            from screenread import region
            limg = self._grab(rect, region("zone"))
            txt = read_label(limg)
            if len(txt) >= 3:
                if txt == self._zone_cand:       # same reading twice in a row: trust it
                    if txt != self.zone:
                        self.zone_changed_t = now
                    self.zone = txt
                    self.zone_ok_t = now
                    if re.fullmatch(r"[A-Za-z][A-Za-z']*(?: [A-Za-z][A-Za-z']*)*", txt):
                        remember_label_image(limg, txt)     # its picture: next time it's read by picture
                self._zone_cand = txt
            self._zone_t = now
        from screenread import region
        if now - getattr(self, "_level_t", 0) > 4:
            self._level_t = now
            self._read_level(rect)
        if exact:
            tl = exact.get("target_level")
            self.target_level = tl if tl and tl > 0 else None    # (skull = no number, as on the frame)
        else:
            try:
                self.target_level = read_small_level(self._grab(rect, region("target_level")))
            except Exception:
                self.target_level = None
        # your coords: from the add-on when it has them (the text under the minimap sits elsewhere on a
        # character with another layout, and the old reading then stays on the page for hours)
        mp = bc.get("map_pos") if bc else None
        if mp:
            xy = (round(mp[0] * 100, 1), round(mp[1] * 100, 1))
        else:
            xy = read_coords(self._grab(rect, region("coords")))
        if not xy:
            return
        self._coords(xy[0], xy[1], now)

    def _coords(self, x, y, now):
        # a jump of >8% isn't walking: either an OCR glitch (one frame) or a new area / teleport
        # (the new spot keeps reading the same). Two matching reads = new area: start a fresh trail.
        if self.x is not None and (abs(x - self.x) > 8 or abs(y - self.y) > 8):
            p = self._jump
            if not p or abs(p[0] - x) > 2 or abs(p[1] - y) > 2:
                self._jump = (x, y)
                return
            self._jump = None
            self.history = []
            self.jump_t = now                    # coords jumped: they may be on another zone's map now
        self._jump = None
        self.x, self.y, self.t = x, y, now
        if not self.history or abs(self.history[-1][1] - x) + abs(self.history[-1][2] - y) >= 0.15:
            self.history.append([now, x, y])
            self.history = self.history[-3000:]


def read_small_level(img):
    """The tiny level digits on the target frame. Windows OCR won't read 1-2 lone digits, so they're
    pasted after a rendered word "Level" for context. WoW's 1 has a flag OCR calls 7: no level is 70+."""
    from PIL import ImageDraw, ImageFont
    a = np.asarray(img.convert("RGB")).astype(int)
    if (a.max(axis=2) > 130).sum() < 6:
        return None
    for thr, sc in ((130, 4), (130, 3), (160, 3)):
        m = Image.fromarray(np.where(a.max(axis=2) > thr, 0, 255).astype("uint8"))
        m = m.resize((img.width * sc, img.height * sc), Image.LANCZOS)
        c = Image.new("L", (m.width + 260, m.height + 40), 255)
        ImageDraw.Draw(c).text((20, 20 + int(m.height * 0.2)), "Level",
                               font=ImageFont.truetype("arial.ttf", int(m.height * 0.55)), fill=0)
        c.paste(m, (200, 20))
        txt = " ".join(ln["text"] for ln in ocr(c.convert("RGB"), False))
        mm = re.search(r"(\d{1,2})\s*$", txt.replace("Level", " "))
        if mm:
            v = mm.group(1)
            if len(v) == 2 and v[0] == "7":
                v = "1" + v[1]
            if 1 <= int(v) <= 60:
                return int(v)
    return None


def _level_from(texts):
    for t in texts:
        t = t.replace("I", "1").replace("l", "1").replace("O", "0").replace(" ", "")
        if t.isdigit() and 1 <= int(t) <= 60:
            return int(t)
    return None


CONTINENTS = {"world", "azeroth", "eastern kingdoms", "kalimdor", "cosmic"}


def capture_map(replace=False, known_zones=(), current_zone=None):
    """If the Map & Quest Log is open, save its map picture for the zone it shows.
    Maps are kept forever: replace=False never touches an existing zone map; replace=True
    (the Save map button) overwrites it. Returns the zone name."""
    from screenread import find_window
    _, rect = find_window("World of Warcraft")
    if not rect:
        raise RuntimeError("WoW window not found")
    wx, wy, wr, wb = rect
    sx, sy = (wr - wx) / 1920, (wb - wy) / 1080
    full = fastgrab.screen(rect)
    probe = full.crop((0, 0, int(1500 * sx), int(760 * sy)))
    title = next((ln for ln in ocr(probe, False) if "quest log" in ln["text"].lower()), None)
    if not title:
        raise RuntimeError("Map & Quest Log isn't open (press M or L)")
    tx, ty = title["x"] / 2 / sx, title["y"] / 2 / sy
    cx0, cy0, cx1, cy1 = (tx + CANVAS_OFF[0], ty + CANVAS_OFF[1], tx + CANVAS_OFF[2], ty + CANVAS_OFF[3])
    crumb = full.crop((int((tx + CRUMB_OFF[0]) * sx), int((ty + CRUMB_OFF[1]) * sy),
                       int((tx + CRUMB_OFF[2]) * sx), int((ty + CRUMB_OFF[3]) * sy)))
    text = " ".join(ln["text"] for ln in ocr(text_mask(crumb, 100), False)).strip()
    text = re.sub(r"[^A-Za-z' ]", " ", text)
    # the deepest breadcrumb is the map being shown; prefer zone names the combat log has used
    low = " ".join(text.lower().split())
    hits = [(low.rfind(z.lower()), z) for z in known_zones
            if z and z.lower() in low and z.lower() not in CONTINENTS]
    if hits:
        zone = max(hits)[1]
    else:
        parts = [p for p in re.split(r"\s{2,}", text) if len(p.strip()) >= 3]
        zone = parts[-1].strip() if parts else ""
        if zone.lower() in CONTINENTS:
            zone = ""
    if len(zone) < 3 and current_zone and current_zone.lower() not in CONTINENTS:
        zone = current_zone                          # the map opens on where you are
    if len(zone) < 3:
        raise RuntimeError("couldn't read which zone the map shows")
    os.makedirs(MAP_DIR, exist_ok=True)
    dest = map_file(zone)
    if os.path.exists(dest) and not replace:
        return zone                                  # already have this zone: keep it
    full.crop((int(cx0 * sx), int(cy0 * sy), int(cx1 * sx), int(cy1 * sy))).save(dest)
    return zone
