"""Reads your buff bar off the screen and names each icon.

Icons are identified by comparing them with the real spell icons: every buff the combat log has
seen on you gives a spell id -> Wowhead's tooltip service gives its icon name -> the icon image is
downloaded once into icons/. Presence is instant (no waiting for the log to be written).
"""
import io
import json
import os
import threading
import time
import urllib.request

import numpy as np
from PIL import Image

import fastgrab

HERE = os.path.dirname(os.path.abspath(__file__))
ICON_DIR = os.path.join(HERE, "icons")
ICON_INDEX = os.path.join(ICON_DIR, "index.json")       # spell name -> icon file
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WoWCompanion/1.0 (personal second-screen tool)"


def _get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _norm(arr):
    """Icon interior -> 20x20 float, with the bottom-right corner (charge count) masked out."""
    im = Image.fromarray(arr.astype("uint8")).resize((20, 20), Image.LANCZOS)
    a = np.asarray(im).astype(float)
    a[11:, 11:] = np.nan
    return a


def _has_frame(icon):
    """Buff icons have a thin black frame: look for dark lines on at least 3 of the 4 edges."""
    lum = icon.mean(axis=2)
    sides = [lum[:3, :], lum[-3:, :], lum[:, :3].T, lum[:, -3:].T]
    dark = sum(1 for sd in sides if max((line < 60).mean() for line in sd) >= 0.6)
    return dark >= 3


def _corr(a, b):
    m = ~np.isnan(a) & ~np.isnan(b)
    x, y = a[m], b[m]
    x, y = x - x.mean(), y - y.mean()
    d = np.sqrt((x * x).sum() * (y * y).sum())
    return float((x * y).sum() / d) if d else 0.0


class BuffReader(threading.Thread):
    def __init__(self, screen, every=1.5):
        super().__init__(daemon=True)
        self.screen, self.every = screen, every
        self.buffs, self.t, self.count = [], 0, 0
        self.lib = {}                               # spell name -> normalised icon
        self.wanted = {}                            # spell name -> spell id (from the log)
        self.lock = threading.Lock()
        self.font = None
        self.last_left = {}                         # buff -> last good seconds-left reading
        os.makedirs(ICON_DIR, exist_ok=True)
        try:
            with open(ICON_INDEX, encoding="utf-8") as _f:
                self.index = json.load(_f)
        except (OSError, ValueError):
            self.index = {}

    def want(self, name, spell_id):
        """The log saw buff `name` (spell_id) on you: make sure its icon is in the library."""
        if name not in self.lib and name not in self.wanted and name not in getattr(self, "failed", ()):
            with self.lock:
                self.wanted[name] = spell_id

    def _fetch_icons(self):
        with self.lock:
            todo = dict(self.wanted)
            self.wanted.clear()
        for name, sid in todo.items():
            try:
                fn = self.index.get(name)
                if not fn:
                    j = json.loads(_get(f"https://nether.wowhead.com/forever/tooltip/spell/{sid}"))
                    icon = j.get("icon")
                    if not icon:
                        self.failed = getattr(self, "failed", set()) | {name}   # (no icon exists: don't ask again each time)
                        continue
                    fn = icon + ".jpg"
                    path = os.path.join(ICON_DIR, fn)
                    if not os.path.exists(path) or not os.path.getsize(path):
                        data = _get(f"https://wow.zamimg.com/images/wow/icons/medium/{fn}")   # (fetched BEFORE the file is
                        with open(path, "wb") as f:                                        #  made: a failed download left
                            f.write(data)                                                  #  an empty icon for good)
                    self.index[name] = fn
                    with open(ICON_INDEX, "w", encoding="utf-8") as _f:
                        json.dump(self.index, _f, indent=1)
                self._load(name, fn)
            except Exception:
                pass
            time.sleep(0.5)

    def _load(self, name, fn):
        a = np.asarray(Image.open(os.path.join(ICON_DIR, fn)).convert("RGB"))
        self.lib[name] = _norm(a[3:-3, 3:-3])

    def _layout(self):
        from screenread import region
        try:
            return dict(region("buffs"))
        except (KeyError, TypeError, ValueError):
            return {"right": 1331, "top": 873, "size": 29, "pitch": 34.5, "max": 12}

    def read(self, img_full=None):
        from screenread import find_window
        _, rect = find_window("World of Warcraft")
        if not rect:
            return None
        wx, wy, wr, wb = rect
        sx, sy = (wr - wx) / 1920, (wb - wy) / 1080
        L = self._layout()
        left = L["right"] - L["max"] * L["pitch"]
        x0, y0 = int((left - 4) * sx), int((L["top"] - 2) * sy)
        x1, y1 = int((L["right"] + 4) * sx), int((L["top"] + L["size"] + 2) * sy)
        img = np.asarray(fastgrab.screen((wx + x0, wy + y0, wx + x1, wy + y1)).convert("RGB")).astype(int)
        found = []
        for i in range(L["max"]):
            r = L["right"] - i * L["pitch"]
            ix0, ix1 = int((r - L["size"]) * sx) - x0, int(r * sx) - x0
            iy0, iy1 = int(L["top"] * sy) - y0, int((L["top"] + L["size"]) * sy) - y0
            icon = img[iy0:iy1, ix0:ix1]
            if icon.size == 0:
                break
            if not _has_frame(icon) or icon[4:-4, 4:-4].std() < 12:
                break                                   # no icon frame here: end of the buff bar
            # timer text under the icon ("9 m", "43 m", "45 s", "1 h")
            tx0, tx1 = int((r - L["size"] - 5) * sx), int((r + 4) * sx)
            ty0, ty1 = int((L["top"] + L["size"] + 1) * sy), int((L["top"] + L["size"] + 13) * sy)
            timer = np.asarray(fastgrab.screen((wx + tx0, wy + ty0, wx + tx1, wy + ty1)).convert("RGB"))
            n = _norm(icon[3:-3, 3:-3])
            best, score = None, 0.0
            for name, ref in self.lib.items():
                c = _corr(n, ref)
                if c > score:
                    best, score = name, c
            name = best if score >= 0.55 else None
            found.append({"slot": i, "name": name, "score": round(score, 2), "left": self._timer(name, timer)})
        # you can't have the same buff twice: other icons that only look like it (someone else's buffs
        # we have no picture for) lose the name; the best match keeps it
        best = {}
        for b in found:
            if b["name"] and (b["name"] not in best or b["score"] > best[b["name"]]["score"]):
                best[b["name"]] = b
        for b in found:
            if b["name"] and best[b["name"]] is not b:
                b["name"], b["left"] = None, None
        return found

    def _timer(self, name, rgb):
        """Seconds left from the timer text; teaches itself 's' and 'h' the first time it meets them."""
        import re
        import timerfont
        if not self.font:
            self.font = timerfont.load_font()
        text, unknown = timerfont.read_parts(rgb, self.font)
        m = re.search(r"(\d{1,2})([hms])", text)
        prev = self.last_left.get(name)
        if not m and unknown and re.fullmatch(r"\d{1,2}", text) and name:
            # digits followed by a letter we don't know yet. WoW draws the last minute in white.
            if timerfont.is_white(rgb):
                self.font = timerfont.teach(unknown[-1], "s")
            elif int(text) <= 3 and (prev is None or prev >= 3300):
                self.font = timerfont.teach(unknown[-1], "h")      # "1 h"/"2 h": long buff
            text, unknown = timerfont.read_parts(rgb, self.font)
            m = re.search(r"(\d{1,2})([hms])", text)
        if unknown and name and prev is not None:
            self._teach_digit(name, rgb, prev)                  # a digit drawn a bit differently: learn it
            text, unknown = timerfont.read_parts(rgb, self.font)
            m = re.search(r"(\d{1,2})([hms])", text)
        if not m or unknown:
            return prev           # keep the last good reading (an unread digit would make "43 m" read "4 m")
        left = int(m.group(1)) * {"h": 3600, "m": 60, "s": 1}[m.group(2)]
        if name:
            self.last_left[name] = left
            self.last_left_t = getattr(self, "last_left_t", {})
            self.last_left_t[name] = time.time()
        return left

    def _teach_digit(self, name, rgb, prev):
        """One unknown digit in a minutes timer: if the countdown since the last good reading allows
        exactly one value whose other characters match what we can read, that glyph is that digit."""
        import timerfont
        t_prev = getattr(self, "last_left_t", {}).get(name)
        if t_prev is None or time.time() - t_prev > 150 or prev < 120:
            return
        temps = [(timerfont.from_key(k), ch) for k, ch in self.font.items()]
        chars = []
        for x0, bm in timerfont.glyphs(rgb):
            best = min(temps, key=lambda t: timerfont._dist(bm, t[0])) if temps else None
            if best and timerfont._dist(bm, best[0]) <= 0.45:
                chars.append((best[1], bm))
            elif bm.sum() >= 6:
                chars.append((None, bm))
        if not chars or chars[-1][0] != "m" or sum(1 for c, _ in chars if c is None) != 1:
            return
        digits = chars[:-1]
        est = (prev - (time.time() - t_prev)) / 60.0
        fits = []
        for v in range(max(1, int(est) - 1), int(est) + 3):
            sv = str(v)
            if len(sv) == len(digits) and all(c is None or c == d for (c, _), d in zip(digits, sv)):
                fits.append(sv)
        if len(fits) == 1:
            i = next(i for i, (c, _) in enumerate(digits) if c is None)
            self.font = timerfont.teach(timerfont.key(digits[i][1]), fits[0][i])

    def run(self):
        while True:
            try:
                if self.wanted:
                    self._fetch_icons()
                st = self.screen.snapshot() if self.screen else {"ok": True}
                if st.get("ok"):
                    res = self.read()
                    if res is not None:
                        self.buffs, self.t, self.count = res, time.time(), len(res)
            except Exception:
                pass
            time.sleep(self.every)

    def left(self, name):
        for b in self.buffs:
            if b["name"] == name:
                return b.get("left")
        return None

    def names(self):
        """Names of buffs on you right now, or None if the bar wasn't read recently."""
        if time.time() - self.t > 6:
            return None
        return {b["name"] for b in self.buffs if b["name"]}
