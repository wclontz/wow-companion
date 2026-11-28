"""Live screen reading for the companion: bar fill fractions and action-button cooldowns.

Read-only: captures pixels of the WoW window, never sends input. Geometry lives in
screen_layout.json (defaults below were measured on a 1920x1080 borderless window
with the player frame moved to center-left).
"""
import base64
import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import threading
import time

import numpy as np
import fastgrab

HERE = os.path.dirname(os.path.abspath(__file__))
LAYOUT_FILE = os.path.join(HERE, "screen_layout.json")

DEFAULT_LAYOUT = {
    "window_title": "World of Warcraft",
    "base_size": [1920, 1080],                      # coordinates below are for this window size
    "hp":   {"x0": 753, "x1": 873, "y": 787},
    "mana": {"x0": 755, "x1": 873, "y": 803},
    "xp":   {"x0": 364, "x1": 1546, "y": 1017},
    "target_bar": {"x0": 1284, "x1": 1405, "y": 780},
    # OCR regions: [x0, y0, x1, y1]
    "quest_tracker": [1640, 375, 1920, 960],
    "target_name": [1280, 753, 1402, 770],
    "coords": [1700, 305, 1830, 330],
    "zone": [1685, 4, 1815, 22],
    "level": [683, 792, 716, 826],
    "target_level": [1448, 794, 1474, 813],
    "minimap": {"cx": 1742, "cy": 209, "r": 111},   # the minimap circle (map part, inside the ring)
    "minimap_zones": ["Ironforge"],             # non-dungeon places to stitch too (by the coords text)     # the number beside the target portrait
    "buttons": {"x0": 362, "step": 47, "w": 40, "y0": 1034, "y1": 1074, "count": 12,
                "keys": ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-", "="]},
}

user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass


_LAYOUT_CACHE: dict = {}
_LAYOUT_MTIME: float = 0.0


def region(name):
    """An OCR region from screen_layout.json (falls back to the defaults above)."""
    global _LAYOUT_CACHE, _LAYOUT_MTIME
    try:
        mt = os.path.getmtime(LAYOUT_FILE)
    except OSError:
        mt = 0.0
    if mt != _LAYOUT_MTIME or not _LAYOUT_CACHE:
        lay = dict(DEFAULT_LAYOUT)
        try:
            with open(LAYOUT_FILE, encoding="utf-8") as f:
                lay.update(json.load(f))
        except (OSError, ValueError):
            pass
        _LAYOUT_CACHE = lay
        _LAYOUT_MTIME = mt
    v = _LAYOUT_CACHE[name]
    return dict(v) if isinstance(v, dict) else tuple(v)


def find_window(title):
    h = user32.FindWindowW(None, title)
    if not h or user32.IsIconic(h):
        return None, None
    r = wt.RECT()
    pt = wt.POINT(0, 0)
    user32.GetClientRect(h, ctypes.byref(r))
    user32.ClientToScreen(h, ctypes.byref(pt))
    return h, (pt.x, pt.y, pt.x + r.right, pt.y + r.bottom)


# --- colour tests per bar (RGB arrays)
def is_hp(p):   return (p[..., 1] > 110) & (p[..., 1] > p[..., 0] + 30) & (p[..., 2] < 110)
def is_mana(p): return (p[..., 2] > 140) & (p[..., 0] < 100)
def is_xp(p):
    """XP earned: purple normally, BRIGHT blue while you're rested. Never the dark-blue rested overlay."""
    r, g, b = p[..., 0], p[..., 1], p[..., 2]
    purple = (r > 70) & (b > 90) & (g < r)
    blue = (b > 150) & (b > 1.8 * r) & (g > r)
    return purple | blue


def is_rested(p):
    """The dark-blue overlay past your XP: how far the rested bonus reaches."""
    r, g, b = p[..., 0], p[..., 1], p[..., 2]
    return (b >= 45) & (b <= 140) & (b > 1.6 * r) & (b > 1.4 * g)


def fill_fraction(row, test):
    """row: (N,3) pixels along a bar. Fraction = rightmost filled pixel / length."""
    m = test(row)
    if not m.any():
        return 0.0
    # rightmost run that connects back toward the left edge (ignore stray specks)
    idx = np.nonzero(m)[0]
    last = idx[-1]
    # tolerate the anti-aliased tip: require at least 60% filled up to `last`
    if m[:last + 1].mean() < 0.6:
        last = idx[int(len(idx) * 0.9) - 1] if len(idx) > 1 else idx[0]
    return float(min(1.0, (last + 1) / len(row)))


def fill_run(row, test, gap=8):
    """Fraction filled, following the fill from the left edge and stopping at the first gap wider than
    `gap` px (the bar's tick separators are ~3 px). A stray matching pixel further along (the rested
    marker, a highlight) can't stretch it."""
    m = test(row)
    idx = np.nonzero(m)[0]
    if not len(idx) or idx[0] > 12:
        return 0.0
    end = idx[0]
    for i in idx[1:]:
        if i - end > gap:
            break
        end = i
    return float(min(1.0, (end + 1) / len(row)))


class ScreenReader(threading.Thread):
    def __init__(self, hz=5):
        super().__init__(daemon=True)
        self.hz = hz
        self.layout = self._load()
        self.lock = threading.Lock()
        self.state = {"ok": False, "reason": "starting"}
        self.btn_ref = None             # per-button "ready" brightness reference
        self.btn_rgb = None             # per-button usual colour
        self.icons = None               # base64 PNGs of button icons
        self.icons_t = 0
        self.xp_hist = []               # [t, frac]
        self.level_ups = 0
        self.reaction = None

    def _load(self):
        lay = json.loads(json.dumps(DEFAULT_LAYOUT))
        try:
            with open(LAYOUT_FILE, encoding="utf-8") as f:
                lay.update(json.load(f))
        except (OSError, ValueError):
            pass
        return lay

    def _save(self):
        try:
            with open(LAYOUT_FILE, "w", encoding="utf-8") as f:
                json.dump(self.layout, f, indent=2)
        except OSError:
            pass

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def run(self):
        while True:
            t0 = time.time()
            try:
                st = self._read()
            except Exception as e:      # never let a bad frame kill the thread
                st = {"ok": False, "reason": f"error: {e}"}
            st["t"] = time.time()
            with self.lock:
                self.state = st
            time.sleep(max(0.05, 1 / self.hz - (time.time() - t0)))

    def _read(self):
        lay = self.layout
        h, rect = find_window(lay["window_title"])
        if not h:
            return {"ok": False, "reason": "WoW window not found (minimized?)"}
        wx, wy, wr, wb = rect
        bw, bh = lay["base_size"]
        sx, sy = (wr - wx) / bw, (wb - wy) / bh
        img = np.asarray(fastgrab.screen(rect).convert("RGB")).astype(np.int16)

        def row(b):
            y = int(round(b["y"] * sy))
            return img[y, int(b["x0"] * sx):int(b["x1"] * sx) + 1]

        # sanity: the left end of the HP bar is green (or dark if you're dead) and the XP bar
        # starts purple; if neither, something is covering the game (another window, a menu)
        hp_row, xp_row = row(lay["hp"]), row(lay["xp"])
        if not is_hp(hp_row[2:8]).any() and not is_xp(xp_row[1:6]).any():
            return {"ok": False, "reason": "game UI not visible (covered, menu open, or layout moved)"}
        hp = fill_fraction(hp_row, is_hp)
        mana = fill_fraction(row(lay["mana"]), is_mana)
        xp = fill_run(xp_row, is_xp)
        rested_to = fill_run(xp_row, lambda q: is_xp(q) | is_rested(q))
        rested = max(0.0, rested_to - xp) if rested_to > xp + 0.005 else 0.0
        now = time.time()
        if self.xp_hist and self.xp_hist[-1][1] > 0.85 and xp < 0.15:
            self._low_reads = getattr(self, "_low_reads", 0) + 1
            if self._low_reads < 2:
                return {"ok": False, "reason": "XP bar changed, confirming"}   # one bad frame isn't a level-up
            self.level_ups += 1
        self._low_reads = 0
        if not self.xp_hist or abs(self.xp_hist[-1][1] - xp) > 1e-4:
            self.xp_hist.append([now, xp])
            self.xp_hist = self.xp_hist[-2000:]

        target = self._read_target(img, sx, sy)
        aggro_glow = self._aggro_glow(img, sx, sy)
        party = self._read_party(img, sx, sy)
        buttons = self._buttons(img, sx, sy)
        return {"ok": True, "hp": round(hp, 4), "mana": round(mana, 4), "xp": round(xp, 4), "rested": round(rested, 4),
                "target": target, "reaction": self.reaction if target is not None else None,
                "aggro_glow": aggro_glow, "party": party, "buttons": buttons, "level_ups": self.level_ups,
                "icons": self.icons}

    def _read_party(self, img, sx, sy):
        """Party frames: [{slot, hp, mana}] for each frame that's showing (HP/mana as fractions)."""
        P = self.layout.get("party")
        if not P:
            return []
        x0, x1 = int(P["x0"] * sx), int(P["x1"] * sx) + 1
        power = lambda p: is_mana(p) | ((p[..., 0] > 150) & (p[..., 1] < 70) & (p[..., 2] < 70)) | \
            ((p[..., 0] > 180) & (p[..., 1] > 160) & (p[..., 2] < 80))          # mana / rage / energy
        out = []
        for i, (hy, my) in enumerate(zip(P["hp_y"], P["mana_y"])):
            hrow = img[int(hy * sy), x0:x1]
            mrow = img[int(my * sy), x0:x1]
            hp_on, mp_on = is_hp(hrow[2:8]).any(), power(mrow[2:8]).any()
            # a frame with an empty (dead) HP bar still has its power bar track; no frame = neither
            if not hp_on and not mp_on:
                continue
            out.append({"slot": i, "hp": float(round(fill_fraction(hrow, is_hp), 3)) if hp_on else 0.0,
                        "mana": float(round(fill_fraction(mrow, power), 3)) if mp_on else None})
        return out

    def _aggro_glow(self, img, sx, sy):
        """Blizzard rims the player frame in red when you have threat. Look for strong red around
        (not inside) the HP/mana bars. Returns True/False; threshold tuned against a real pull."""
        hp = self.layout["hp"]
        x0, x1 = int((hp["x0"] - 75) * sx), int((hp["x1"] + 12) * sx)
        y0, y1 = int((hp["y"] - 35) * sy), int((hp["y"] + 42) * sy)
        box = img[max(0, y0):y1, max(0, x0):x1].reshape(-1, 3)
        red = (box[:, 0] > 170) & (box[:, 1] < 70) & (box[:, 2] < 70)
        self.glow_frac = float(red.mean()) if len(box) else 0.0
        return self.glow_frac > self.layout.get("aggro_glow_thr", 0.03)

    def _read_target(self, img, sx, sy):
        """Target health fraction, or None when nothing (alive) is targeted."""
        tb = self.layout.get("target_bar")
        if not tb:
            return None
        y = int(round(tb["y"] * sy))
        r = img[y, int(tb["x0"] * sx):int(tb["x1"] * sx) + 1]
        bar = lambda p: is_hp(p) | ((p[..., 0] > 150) & (p[..., 1] < 90) & (p[..., 2] < 90))   # green or red
        if not bar(r[1:7]).any():
            self.reaction = None
            return None
        self.reaction = self._reaction(img, tb, sx, sy)
        return float(round(fill_fraction(r, bar), 4))

    def _reaction(self, img, tb, sx, sy):
        """Colour of the band behind the target's name: friendly / neutral / hostile / player."""
        y0, y1 = int((tb["y"] - 22) * sy), int((tb["y"] - 12) * sy)
        x0, x1 = int((tb["x0"] + 4) * sx), int((tb["x1"] - 4) * sx)
        band = img[y0:y1, x0:x1].reshape(-1, 3)
        r, g, b = band[:, 0], band[:, 1], band[:, 2]
        bg = band[~((r > 110) & (g > 90) & (r > b + 40))]     # drop the yellow name text
        if len(bg) < 20:
            return None
        R, G, B = (float(v) for v in bg.mean(axis=0))
        if B > R + 25 and B > G:
            return "player"
        if G > R + 40 and G > B + 40:
            return "friendly"
        if R > G + 50 and R > B + 50:
            return "hostile"
        if R > 120 and G > 100 and B < 80:
            return "neutral"
        return None

    _bar_set, _relearn = None, False

    def bar_is(self, key):
        """The add-on's word on which buttons the bar is showing (None: it does not say). When that changes
        (another page, a spell put on a slot) what each button looked like when ready is learned again:
        against another page's icons a darker icon read as cooling down."""
        if key is not None and key != self._bar_set:
            self._relearn = self._bar_set is not None
            self._bar_set = key

    def _buttons(self, img, sx, sy):
        if self._relearn:
            self._relearn, self.btn_ref, self.btn_rgb, self.icons_t = False, None, None, 0
        b = self.layout["buttons"]
        out = []
        crops = []
        for i in range(b["count"]):
            x = int((b["x0"] + i * b["step"]) * sx)
            w = int(b["w"] * sx)
            y0, y1 = int(b["y0"] * sy), int(b["y1"] * sy)
            crop = img[y0:y1, x:x + w]
            crops.append(crop)
            inner = crop[int(crop.shape[0] * .25):int(crop.shape[0] * .75), int(w * .2):int(w * .8)]
            rgb = inner.reshape(-1, 3).mean(axis=0)
            # an empty slot shows the bar's own dark, flat backing (measured 2026-11-25: about 41, 34, 25): it is
            # not a spell cooling down, however bright that slot was when it held something
            bare = bool(np.abs(rgb - np.array(b.get("empty_rgb", [41, 34, 25]))).max() < 9
                        and inner.mean(axis=2).std() < 25)
            out.append({"slot": i + 1, "key": b["keys"][i] if i < len(b["keys"]) else "",
                        "lum": float(rgb.mean()), "rgb": rgb, "bare": bare})
        lums = np.array([o["lum"] for o in out])
        if self.btn_ref is None:
            self.btn_ref = lums.copy()
        # reference = slowly-decaying max (ready icons are the brightest we see)
        self.btn_ref = np.maximum(self.btn_ref * 0.9995, lums)
        if self.btn_rgb is None:
            self.btn_rgb = [o["rgb"].copy() for o in out]
        for i, (o, ref) in enumerate(zip(out, self.btn_ref)):
            ratio = o["lum"] / max(1.0, ref)
            if ratio > 0.95:                          # looks normal: learn its usual colour
                self.btn_rgb[i] = self.btn_rgb[i] * 0.9 + o["rgb"] * 0.1
            rel = o["rgb"] / np.maximum(self.btn_rgb[i], 8)   # colour shift vs. its own normal look
            if ref < 25 or o["bare"]:
                o["state"] = "empty"
                self.btn_ref[i] = o["lum"]            # (what it held is gone: forget how bright that was)
            elif ratio < 0.62:
                o["state"] = "cooldown"
            elif rel[0] > 1.1 and rel[1] < 0.75 and rel[2] < 0.75:
                o["state"] = "range"                  # WoW tints unusable-range icons red
            elif rel[2] > 1.05 and rel[0] < 0.7:
                o["state"] = "mana"                   # and not-enough-mana icons blue
            else:
                o["state"] = "ready"
            o["dim"] = float(round(1 - min(1.0, ratio), 3))
            del o["lum"], o["rgb"], o["bare"]
        # all buttons dark together = global cooldown, not real cooldowns
        if sum(o["state"] == "cooldown" for o in out) >= max(3, len(out) // 2):
            for o in out:
                if o["state"] == "cooldown":
                    o["state"] = "gcd"
        if time.time() - self.icons_t > 30 and not any(o["state"] in ("cooldown", "gcd") for o in out):
            self.icons = [self._png(c) for c in crops]
            self.icons_t = time.time()
        return out

    @staticmethod
    def _png(arr):
        from PIL import Image
        buf = io.BytesIO()
        Image.fromarray(arr.astype(np.uint8)).save(buf, "PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


if __name__ == "__main__":       # quick check: print a few readings
    sr = ScreenReader()
    for _ in range(5):
        st = sr._read()
        st.pop("icons", None)
        print({k: v for k, v in st.items() if k != "buttons"},
              " ".join(f"{b['key']}:{b['state']}" for b in st.get("buttons", [])))
        time.sleep(0.5)
