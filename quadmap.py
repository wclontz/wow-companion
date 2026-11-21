"""Open-world maps from minimap shots, placed by the coords under the minimap ("quadrant" maps).

The rules (agreed with the user, 2026-10-09):
  - The coords say exactly where every shot goes. Pictures never move anything.
  - A zone map is cut into fixed squares; each square shows the shots whose centre is nearest to it
    (the middle of a minimap is the trustworthy part). A bad shot can only touch the few squares it
    is nearest to, and is outvoted/dropped when other shots of the same spot disagree with it.
  - Scale: px per map-% east-west; north-south is always that / 1.5 (every zone map is 3:2).
    The minimap's zoom is NOT assumed: each stretch of continuous walking ("run") measures its own
    from picture shift vs coords change. A zoom change or the indoor view breaks the run, so a run
    has one zoom. Shots are resampled to the zone's reference scale when drawn.
  - Layers (stacked floors, caves, Ironforge's levels): a run inherits its layer from the shots it
    overlaps (picture agrees at the coords' spot). A run that disagrees with what's mapped there at
    2+ spots becomes a new layer. The label above the minimap is only a hint for runs over new ground.
  - Nothing from an undecided run is drawn. A run of a single shot is never drawn.

Files: maps/quad/<place>/meta.json + clips/*.png + <place>.png (layer 0) / <place>_L<n>.png.
Raw clips stay in maps/minimap_raw (the minimapper archives them), so this can always be rebuilt.
"""
import json
import math
import os
import threading
import time

import numpy as np
from PIL import Image, ImageFilter

from minimapper import HERE, safe, phase_shift, overlap_similarity, is_blank, disc_mask

QROOT = os.path.join(HERE, "maps", "quad")
ASPECT = 1.5            # px per % north-south = east-west / 1.5
G = 40                  # square size in map px at the zone's reference scale
PER_SQ = 4              # shots kept per square (best centred)
RUN_GAP = 8.0           # s: shots further apart than this can't be one continuous run
SAME = 0.6              # feature agreement that means "same place, same layer" (same spot scores 0.95+)
UNLIKE = 0.3            # below this: something different is mapped here
VIEW_RATIO = 2.3 / 0.83 # indoor minimap view (2.3 px/yd) vs outdoor (0.83 px/yd)


_GM = {}


def _gmat(n, sigma):
    """n x n Gaussian blur matrix (blur = M @ v @ M.T); cached."""
    M = _GM.get((n, sigma))
    if M is None:
        i = np.arange(n)
        M = np.exp(-0.5 * ((i[:, None] - i[None, :]) / sigma) ** 2)
        M /= M.sum(axis=1, keepdims=True)
        _GM[(n, sigma)] = M
    return M


def _mblur(v, w, sigma):
    """Blur that ignores pixels outside the shot (normalized: blur(v*w) / blur(w))."""
    M = _gmat(v.shape[0], sigma)
    return (M @ (v * w) @ M.T) / np.maximum(M @ w @ M.T, 1e-3)


def detail(img, lo=1.5, hi=6.0):
    """Mid-size features only: big soft blobs match anything by chance once a shot is blown up to
    another zoom, and pixel speckle doesn't survive resizing. Blurs ignore the area outside the shot."""
    a = np.asarray(img, dtype=float)
    g = a[..., :3].mean(axis=2)
    ok = (a[..., 3] > 0) & (g > 12)
    w = ok.astype(float)
    hp = _mblur(g, w, lo) - _mblur(g, w, hi)
    okk = np.asarray(Image.fromarray((ok * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(9))) > 0
    return hp, okk


def similar(a_img, b_img, dx, dy, give=5):
    """Fine-detail agreement (-1..1) of two same-size shots with b placed (dx, dy) px from a, a few px
    of give. None if they barely overlap."""
    ga, oka = detail(a_img)
    gb, okb = detail(b_img)
    n = ga.shape[0]
    best = None
    for ox in range(int(round(dx)) - give, int(round(dx)) + give + 1):
        for oy in range(int(round(dy)) - give, int(round(dy)) + give + 1):
            ay0, ay1 = max(0, oy), min(n, n + oy)
            ax0, ax1 = max(0, ox), min(n, n + ox)
            if ay1 - ay0 < 20 or ax1 - ax0 < 20:
                continue
            ok = oka[ay0:ay1, ax0:ax1] & okb[ay0 - oy:ay1 - oy, ax0 - ox:ax1 - ox]
            if ok.sum() < 500:
                continue
            x = ga[ay0:ay1, ax0:ax1][ok]
            y = gb[ay0 - oy:ay1 - oy, ax0 - ox:ax1 - ox][ok]
            x, y = x - x.mean(), y - y.mean()
            c = float((x * y).sum() / (np.sqrt((x * x).sum() * (y * y).sum()) + 1e-9))
            best = c if best is None else max(best, c)
    return best


BOUNDS_FILE = os.path.join(HERE, "zone_bounds.json")
_BOUNDS = None


def zone_bounds():
    """Zone -> [north X, south X, west Y, east Y] (world yards), from the log's MAP_CHANGE lines."""
    global _BOUNDS
    if _BOUNDS is None:
        try:
            _BOUNDS = json.load(open(BOUNDS_FILE, encoding="utf-8"))
        except (OSError, ValueError):
            _BOUNDS = {}
    return _BOUNDS


def note_bounds(zone, b):
    bb = zone_bounds()
    if bb.get(zone) != b:
        bb[zone] = b
        try:
            with open(BOUNDS_FILE + ".tmp", "w", encoding="utf-8") as f:
                json.dump(bb, f, indent=1)
            os.replace(BOUNDS_FILE + ".tmp", BOUNDS_FILE)
        except OSError:
            pass


IDS_FILE = os.path.join(HERE, "map_ids.json")
_IDS = None


def map_ids():
    """Zone map id (as text) -> zone name, from the log's MAP_CHANGE lines (for the position add-on)."""
    global _IDS
    if _IDS is None:
        try:
            _IDS = json.load(open(IDS_FILE, encoding="utf-8"))
        except (OSError, ValueError):
            _IDS = {}
    return _IDS


def note_map_id(mid, zone):
    ids = map_ids()
    if ids.get(str(mid)) != zone:
        ids[str(mid)] = zone
        try:
            with open(IDS_FILE + ".tmp", "w", encoding="utf-8") as f:
                json.dump(ids, f, indent=1)
            os.replace(IDS_FILE + ".tmp", IDS_FILE)
        except OSError:
            pass


def pct_to_pct(src, dst):
    """Linear map from zone src's map % to zone dst's map %: (ax, bx, ay, by) with
    dst_x = ax * src_x + bx, dst_y = ay * src_y + by. None without both zones' world positions."""
    bb = zone_bounds()
    S, D = bb.get(src), bb.get(dst)
    if not S or not D or D[0] == D[1] or D[2] == D[3]:
        return None
    ax = (S[2] - S[3]) / (D[2] - D[3])
    bx = (D[2] - S[2]) * 100 / (D[2] - D[3])
    ay = (S[0] - S[1]) / (D[0] - D[1])
    by = (D[0] - S[0]) * 100 / (D[0] - D[1])
    return ax, bx, ay, by


CORE_H = 14                               # (set by minimapper._grab_at: wider on a character with another layout)


def gray_core(img):
    """Middle of the minimap for picture-shift tracking (arrow area flattened)."""
    a = np.asarray(img.convert("L"), dtype=float)
    n = a.shape[0]
    s = int(n * 0.65)
    c = n // 2
    g = a[c - s // 2:c - s // 2 + s, c - s // 2:c - s // 2 + s].copy()
    h = max(14, CORE_H)                       # your arrow (~10 px from the centre, any way it faces)
    g[s // 2 - h:s // 2 + h, s // 2 - h:s // 2 + h] = g.mean()
    return g


def scaled(img, f, n):
    """img resized by f, then centre-cropped / padded to n x n (so it lines up with an n x n shot)."""
    if abs(f - 1) > 0.01:
        w = max(8, int(round(img.width * f)))
        img = img.resize((w, w), Image.BILINEAR)
    out = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    out.paste(img, ((n - img.width) // 2, (n - img.height) // 2))
    return out


class QuadMap:
    # units: zone map % (x, y); the world map overrides these with yards
    ASPECT = ASPECT         # y units per x unit at the same px (zone maps are 3:2)
    MOVE_MIN = 0.3          # a move this big with no picture slide = a false picture lock
    QUANT = 0.15            # coords rounding slack (one decimal of %)
    K_DEFAULT = 25.0        # px per unit before anything is measured
    K_MIN = 2.0             # zooms below this are nonsense
    FAR_MIN, FAR_SURE = 0.7, 2.0     # run length to estimate / to trust a zoom on its own
    SPOT = 0.6              # "different spot" for disagreeing shots

    def __init__(self, root=QROOT):
        self.root = root
        self.places = {}
        self.last = {}               # place -> the previous shot (in memory, for run tracking + scale)
        self.cur_layer = {}          # place -> layer you're on
        self.dirty = {}
        self.lock = threading.RLock()
        self.dbg = {}
        self._img = {}               # (place, file) -> image cache
        self.status = ""

    # ------------------------------------------------------------------ storage
    def _dir(self, place):
        return os.path.join(self.root, safe(place))

    def meta(self, place):
        m = self.places.get(place)
        if m is None:
            try:
                m = json.load(open(os.path.join(self._dir(place), "meta.json"), encoding="utf-8"))
            except (OSError, ValueError):
                m = {"place": place, "ref_k": None, "views": [], "clips": [], "runs": {}, "labels": {}}
            self.places[place] = m
        return m

    def save(self, place):
        d = self._dir(place)
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "meta.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.places[place], f)
        for i in range(8):                               # (a page may be reading it this instant: Windows refuses
            try:                                         #  the swap - "Access is denied", and the save was lost)
                os.replace(tmp, os.path.join(d, "meta.json"))
                break
            except PermissionError:
                if i == 7:
                    raise
                time.sleep(0.05)

    def img(self, place, c):
        key = (place, c["file"])
        im = self._img.get(key)
        if im is None:
            try:
                im = Image.open(os.path.join(self._dir(place), "clips", c["file"])).convert("RGBA")
            except OSError:
                return None
            if len(self._img) > 3000:
                self._img.clear()
            self._img[key] = im
        return im

    # ------------------------------------------------------------------ adding a shot
    def add(self, place, img, x, y, t=None, label=None, keep=True, mapping=False, k_hint=None, frame=None):
        """A minimap shot (RGBA, already masked, 2r x 2r) taken at coords (x, y) %.
        Returns what happened (for the status line / tests)."""
        t = time.time() if t is None else t
        with self.lock:
            dbg = self.dbg.setdefault(place, {})
            dbg["seen"] = dbg.get("seen", 0) + 1
            if is_blank(img):
                dbg["blank"] = dbg.get("blank", 0) + 1
                self.last.pop(place, None)
                return "blank"
            try:
                import restitch
                if restitch.has_menu(img):
                    dbg["menu"] = dbg.get("menu", 0) + 1
                    self.last.pop(place, None)
                    return "menu"
            except Exception:
                pass
            m = self.meta(place)
            g = gray_core(img)
            prev = self.last.get(place)
            run = None
            same_view = not (k_hint and prev and prev.get("k_hint") and abs(k_hint - prev["k_hint"]) > 0.15 * prev["k_hint"])
            if prev and same_view and 0 < t - prev["t"] <= RUN_GAP and prev["n"] == img.width:
                dx, dy, pk = phase_shift(prev["g"], g)
                dx, dy = int(dx), int(dy)
                moved = math.hypot(x - prev["x"], (y - prev["y"]) / self.ASPECT)
                if dx == 0 and dy == 0 and moved >= self.MOVE_MIN:
                    pass                      # picture "didn't move" but the coords did: a false lock, break
                elif pk >= 10 and math.hypot(dx, dy) < 0.4 * g.shape[0]:
                    run = prev["run"]
                    R = m["runs"][run]
                    k = R.get("k")
                    dX, dY = x - prev["x"], y - prev["y"]
                    if k and math.hypot(dx + k * dX, dy + k * dY / self.ASPECT) > 8 + 0.2 * math.hypot(dx, dy) + k * self.QUANT:
                        run = None            # the picture moved, but not the way the coords did: not the same view
                    else:
                        # add up how far the picture slid along the whole run (coords only have one
                        # decimal; over a long walk that rounding becomes tiny next to the distance)
                        PX, PY = prev["PX"] + dx, prev["PY"] + dy
                        R.setdefault("p", []).append([PX, PY, round(x, 2), round(y, 2)])
                        R["p"] = R["p"][-400:]
            if run is None:
                run = f"r{len(m['runs']) + 1}"
                while run in m["runs"]:
                    run += "x"
                m["runs"][run] = {"k": None, "p": [[0, 0, round(x, 2), round(y, 2)]], "layer": None, "n": 0,
                                  "t0": round(t, 1), "label": label}
                if k_hint:                    # Mapping: the position is known exactly; use the map's measured zoom
                    m["runs"][run]["k"], m["runs"][run]["k_hint"] = round(k_hint, 3), True
                PX = PY = 0
            R = m["runs"][run]
            R["n"] += 1
            R["t1"] = round(t, 1)
            if label:
                R["label"] = label
            self.last[place] = {"t": t, "g": g, "x": x, "y": y, "run": run, "n": img.width, "PX": PX, "PY": PY,
                                "k_hint": k_hint}

            c = {"id": max([o["id"] for o in m["clips"]] or [0]) + 1, "x": round(x, 2), "y": round(y, 2), "t": round(t, 1), "run": run, "label": label}
            if frame:
                c["frame"] = frame                    # the zone (coords map) it was taken in
            if mapping:
                c["mapping"] = True           # taken in Mapping mode: wins over every other shot of the same spot
            stored = keep and self._worth_keeping(m, c, R)
            if stored:
                c["file"] = f"c{c['id']:05d}.png"
                d = os.path.join(self._dir(place), "clips")
                os.makedirs(d, exist_ok=True)
                img.save(os.path.join(d, c["file"]))
                self._img[(place, c["file"])] = img
                m["clips"].append(c)
            self._resolve_run(place, m, run)
            self.dirty[place] = self.dirty.get(place) or time.time()
            if stored:
                self.save(place)
            dbg["stored"] = dbg.get("stored", 0) + int(bool(stored))
            return "stored" if stored else "skipped"

    def _worth_keeping(self, m, c, R):
        """Keep it if its square has room, or it's better centred than the worst kept there."""
        k = m.get("ref_k") or R.get("k") or self.K_DEFAULT
        sq = (int(k * c["x"] // G), int(k / self.ASPECT * c["y"] // G))
        # (Mapping shots get their own room, and so does every layer: a square that was full of outdoor shots
        #  turned away the indoor view of the same ground - a cave, a city street - unless it was better centred)
        same = [o for o in m["clips"] if (int(k * o["x"] // G), int(k / self.ASPECT * o["y"] // G)) == sq
                and bool(o.get("mapping")) == bool(c.get("mapping"))
                and self._same_room(R, m["runs"].get(o["run"]) or {})]
        if len(same) < PER_SQ:
            return True
        cxy = ((sq[0] + 0.5) * G, (sq[1] + 0.5) * G)
        dist = lambda o: math.hypot(k * o["x"] - cxy[0], k / self.ASPECT * o["y"] - cxy[1])
        return dist(c) < max(dist(o) for o in same) - 3

    @staticmethod
    def _same_room(R, Ro):
        """Do two runs' shots share a square's room (PER_SQ)? Same layer; while a layer is undecided, the same
        minimap view (zooms within 1.35x: the indoor view is 1.8x the outdoor one on today's layout, 2.8x on
        the oldest); unknown: yes."""
        if R.get("layer") is not None and Ro.get("layer") is not None:
            return R["layer"] == Ro["layer"]
        if R.get("k") and Ro.get("k"):
            return 1 / 1.35 < R["k"] / Ro["k"] < 1.35
        return True

    # ------------------------------------------------------------------ scale
    def _run_k(self, m, R):
        """The run's zoom: least-squares fit of picture slide vs coords change over the whole run.
        -> (k, sure). sure = the run moved far enough (2 %+) to measure it on its own; otherwise the
        estimate is only good for picking one of the zone's known zooms."""
        p = R.get("p") or []
        if len(p) < 3:
            return None, False
        P0 = p[0]
        num = den = 0.0
        far = 0.0
        for PX, PY, x, y in p[1:]:
            dX, dY = x - P0[2], (y - P0[3]) / self.ASPECT
            num += -((PX - P0[0]) * dX + (PY - P0[1]) * dY)
            den += dX * dX + dY * dY
            far = max(far, math.hypot(dX, dY))
        if far < self.FAR_MIN or den <= 0:
            return None, False
        k = num / den
        if k <= self.K_MIN:
            return None, False
        res = [math.hypot((PX - P0[0]) + k * (x - P0[2]), (PY - P0[1]) + k * (y - P0[3]) / self.ASPECT) for PX, PY, x, y in p[1:]]
        if float(np.sqrt(np.mean(np.square(res)))) > 5 + 0.06 * k * far:
            return None, False                    # picture and coords don't move together: don't trust it
        if far >= self.FAR_SURE:
            return k, True
        # short run: snap to a known zoom of this zone (or its indoor/outdoor partner) if it's close - or, in
        # a zone with none yet, to the zooms other zones show in px per YARD (the same everywhere)
        cands = [v["k"] for v in m["views"]] + [v["k"] * VIEW_RATIO for v in m["views"]] + [v["k"] / VIEW_RATIO for v in m["views"]]
        if not m["views"]:
            cands += self.prior_ks(m.get("place"))
        for kc in cands:
            if abs(k - kc) <= 0.1 * kc:
                return kc, False
        return None, False

    def prior_ks(self, place):
        """Likely zooms (px per map-%) for a zone with none measured yet: every other zone's zooms in px per
        yard, times this zone's width in yards."""
        bb = zone_bounds()
        me = bb.get(place)
        if not me:
            return []
        out = []
        for d in os.listdir(self.root):
            try:
                o = self.places.get(d.replace("_", " ")) or json.load(open(os.path.join(self.root, d, "meta.json"), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            ob = bb.get(o.get("place"))
            if not ob or o.get("place") == place:
                continue
            for v in o.get("views") or []:
                k = v["k"] / ((ob[2] - ob[3]) / 100) * ((me[2] - me[3]) / 100)
                if all(abs(k - q) > 0.04 * q for q in out):
                    out.append(round(k, 3))
        return out

    def _refresh_views(self, m):
        """Zone zooms = clusters of the well-measured runs' zooms (weighted by how far each run went)."""
        ks = sorted(R["k"] for R in m["runs"].values() if R.get("view_added") and R.get("k"))
        views = []
        for k in ks:
            if views and abs(k - views[-1]["k"]) <= 0.06 * views[-1]["k"]:
                v = views[-1]
                v["k"] = round((v["k"] * v["n"] + k) / (v["n"] + 1), 3)
                v["n"] += 1
            else:
                views.append({"k": round(k, 3), "n": 1})
        m["views"] = views
        if views:
            m["ref_k"] = max(views, key=lambda v: v["n"])["k"]

    def _add_view(self, m, k):
        for v in m["views"]:
            if abs(k - v["k"]) <= 0.06 * v["k"]:
                v["k"] = round((v["k"] * v["n"] + k) / (v["n"] + 1), 3)
                v["n"] += 1
                return
        m["views"].append({"k": round(k, 3), "n": 1})
        if not m.get("ref_k"):
            m["ref_k"] = round(k, 3)

    # ------------------------------------------------------------------ layers
    def _neighbours(self, m, c, k, n):
        """Kept shots from OTHER decided runs whose picture overlaps c's a lot (centres < 0.55 minimap apart)."""
        out = []
        for o in m["clips"]:
            if o["run"] == c["run"] or "file" not in o:
                continue
            Ro = m["runs"].get(o["run"])
            if not Ro or Ro.get("layer") is None or not Ro.get("k") or o.get("bad"):
                continue
            ox, oy = k * (o["x"] - c["x"]), k / self.ASPECT * (o["y"] - c["y"])
            dd = math.hypot(ox, oy)
            if dd < 0.55 * n:
                out.append((dd, o["id"], o, Ro, ox, oy))
        out.sort(key=lambda z: z[:2])
        return [z[2:] for z in out]

    def matches(self, place, img, x, y):
        """Does this live minimap picture look like this zone's map at coords (x, y)? -> best agreement
        (0..1) over the zone's known zooms, or None when nothing is mapped there."""
        with self.lock:
            m = self.meta(place)
            best = None
            for v in m.get("views") or []:
                _, sim = self._vote(place, m, {"id": -1, "x": x, "y": y, "run": "_probe"}, v["k"], im=img)
                if sim is not None and (best is None or sim > best):
                    best = sim
            return best

    def _vote(self, place, m, c, k, im=None, same_view=False):
        """Which layer this shot's picture agrees with at its spot: a layer id, 'none' (something is
        mapped here and it doesn't match) or None (nothing mapped here yet)."""
        im = im if im is not None else self.img(place, c)
        if im is None:
            return None, None
        sims = {}
        for o, Ro, ox, oy in self._neighbours(m, c, k, im.width)[:12]:
            if same_view and abs(Ro["k"] - k) > 0.2 * k:
                continue
            oi = self.img(place, o)
            if oi is None:
                continue
            # compare at the coarser of the two zooms: shrinking keeps detail honest, enlarging smears it
            kc = 0.8 * min(k, Ro["k"])        # BOTH shrunk (resampling blurs; this way they blur alike)
            mine_ = scaled(im, kc / k, im.width)
            other = scaled(oi, kc / Ro["k"], im.width)
            sm = similar(mine_, other, ox * kc / k, oy * kc / k, give=4)
            if sm is not None:
                L = Ro["layer"]
                sims[L] = max(sims.get(L, -1.0), sm)
        if not sims:
            return None, None
        best = max(sims, key=sims.get)
        if sims[best] >= SAME:
            return best, round(sims[best], 2)
        if sims[best] < UNLIKE:
            return "none", round(sims[best], 2)
        return None, round(sims[best], 2)            # in between: no say

    def _resolve_run(self, place, m, run):
        R = m["runs"][run]
        if R.get("k_hint"):
            k, sure = R["k"], False               # (Mapping, fixed zoom: the zoom is known, not measured)
        else:
            k, sure = self._run_k(m, R)
        if k is None and not R.get("k") and m["views"]:
            k = self._k_by_picture(place, m, run)
        if k is None and not R.get("k"):
            return
        if k is not None and (not R.get("k") or abs(k - R["k"]) > 0.01 * R["k"]):
            R["k"] = round(k, 3)
            for c in m["clips"]:                  # its pictures get compared again at the new zoom
                if c["run"] == run:
                    c.pop("vote", None)
            if sure:
                R["view_added"] = True
                self._refresh_views(m)
        k = R["k"]
        if R.get("k_hint") and k < 1.0:
            # Mapping, outdoor view: exact position + fixed zoom -> the main level
            if R.get("layer") != 0:
                R["layer"] = 0
                self.dirty[place] = self.dirty.get(place) or time.time()
            self.cur_layer[place] = 0
            return
        if R.get("k_hint"):
            return self._indoor_layer(place, m, run, R, k)
        mine = [c for c in m["clips"] if c["run"] == run and "file" in c]
        for c in mine:
            # vote once; "nothing mapped here" is re-checked as more of the zone gets mapped
            if "vote" not in c or (c["vote"] is None and len(m["clips"]) - c.get("vote_n", 0) >= 10):
                v, s = self._vote(place, m, c, k)
                c["vote"], c["sim"], c["vote_n"] = v, s, len(m["clips"])
        layers = [c["vote"] for c in mine if c["vote"] not in (None, "none")]
        nones = [c for c in mine if c["vote"] == "none"]
        old = R.get("layer")
        if layers:
            cnt = {}
            for L in layers:
                cnt[L] = cnt.get(L, 0) + 1
            L = max(cnt, key=lambda L: (cnt[L], max(c["sim"] or 0 for c in mine if c["vote"] == L)))
            if len(nones) >= 2 and len(nones) > 2 * cnt[L]:
                L = self._new_layer(m)            # mostly unlike what's mapped here: a different level
        elif len(nones) >= 2 and len({(round(c["x"] / self.SPOT), round(c["y"] / self.SPOT)) for c in nones}) >= 2:
            L = old if old is not None and old not in self._layers_used(m, exclude=run) else self._new_layer(m)
        elif nones and self._family_layers(m, k, exclude=run):
            L = None                              # one disagreeing spot: wait for a second before deciding
        elif nones:
            L = old if old is not None and old not in self._layers_used(m, exclude=run) else self._new_layer(m)
        else:
            # new ground (nothing mapped here to compare with): the zoom decides the family. The indoor
            # view (caves, buildings) is ~2.8x the outdoor zoom; manual zoom steps are smaller. Same family:
            # the label's layer if it has one, else the family's most recent layer; none yet: a new layer.
            L = old
            if L is None:
                fam = self._family_layers(m, k, exclude=run)
                lab = (R.get("label") or "").lower()
                hint = m["labels"].get(lab) if lab else None
                if hint in fam:
                    L = hint
                elif fam:
                    L = fam[0]
                else:
                    L = self._new_layer(m)
        R["layer"] = L
        for c in mine:                            # a shot unlike its own layer's map where both exist: bad shot
            c["bad"] = bool(L is not None and c["vote"] == "none" and layers)
        if L is not None:
            self.cur_layer[place] = L
            if R.get("label"):
                m["labels"][R["label"].lower()] = L
        if old != L:
            self.dirty[place] = self.dirty.get(place) or time.time()

    def _indoor_layer(self, place, m, run, R, k):
        """Mapping, indoor view (cities, caves): the city level (1) by default; a stretch whose picture is
        clearly unlike the indoor map already at those spots (2+ spots) is another floor (2, 3, ...). Only
        compared with other INDOOR shots - the outdoor view of the same ground looks different by nature."""
        mine = [c for c in m["clips"] if c["run"] == run and "file" in c]
        for c in mine:
            if "vote" not in c or (c["vote"] is None and len(m["clips"]) - c.get("vote_n", 0) >= 10):
                v, s = self._vote(place, m, c, k, same_view=True)
                c["vote"], c["sim"], c["vote_n"] = v, s, len(m["clips"])
        layers = [c["vote"] for c in mine if c["vote"] not in (None, "none")]
        nones = [c for c in mine if c["vote"] == "none"]
        old = R.get("layer")
        if layers:
            cnt = {}
            for L in layers:
                cnt[L] = cnt.get(L, 0) + 1
            L = max(cnt, key=lambda L: (cnt[L], max(c["sim"] or 0 for c in mine if c["vote"] == L)))
        elif len(nones) >= 2 and len({(round(c["x"] / self.SPOT), round(c["y"] / self.SPOT)) for c in nones}) >= 2:
            used = {Ro.get("layer") for Ro in m["runs"].values() if Ro.get("layer") is not None}
            L = old if old is not None and old >= 2 else max(used | {1}) + 1
        else:
            L = old if old is not None else 1
        if R.get("layer") != L:
            R["layer"] = L
            self.dirty[place] = self.dirty.get(place) or time.time()
        self.cur_layer[place] = L

    def _family_layers(self, m, k, exclude=None):
        """Layers drawn at a zoom of the same family as k (ratio under 2), most recently used first."""
        info = {}
        for rid, R in m["runs"].items():
            if rid == exclude or R.get("layer") is None or not R.get("k"):
                continue
            ks, t1 = info.get(R["layer"], ([], 0))
            ks.append(R["k"])
            info[R["layer"]] = (ks, max(t1, R.get("t1", 0)))
        out = []
        for L, (ks, t1) in info.items():
            r = k / float(np.median(ks))
            if 0.5 < r < 2.0:
                out.append((t1, L))
        return [L for t1, L in sorted(out, reverse=True)]

    def _layers_used(self, m, exclude=None):
        return {R["layer"] for rid, R in m["runs"].items() if rid != exclude and R.get("layer") is not None}

    def _new_layer(self, m):
        used = self._layers_used(m)
        return max(used) + 1 if used else 0

    def _k_by_picture(self, place, m, run):
        """A run that never moved enough to measure its zoom: try the zone's known zooms, keep the one
        whose picture lines up with what's mapped there."""
        mine = [c for c in m["clips"] if c["run"] == run and "file" in c]
        best = None
        for v in m["views"]:
            for c in mine[-3:]:
                vote, sim = self._vote(place, m, c, v["k"])
                if vote not in (None, "none") and sim >= 0.5 and (best is None or sim > best[1]):
                    best = (v["k"], sim)
        return best[0] if best else None

    # ------------------------------------------------------------------ drawing
    def drawable(self, m):
        out = []
        for c in m["clips"]:
            R = m["runs"].get(c["run"])
            if "file" not in c or not R or not R.get("k") or R.get("layer") is None or c.get("bad")                     or (R["n"] < 2 and not c.get("mapping")):        # (a Mapping shot's place is known: one is enough)
                continue
            out.append((c, R))
        return out

    def compose(self, place):
        with self.lock:
            self.dirty[place] = None
            m = self.meta(place)
            for rid, R in list(m["runs"].items()):         # runs still without a zoom/level: check again
                if R.get("n", 0) >= 2 and (not R.get("k") or R.get("layer") is None):
                    self._resolve_run(place, m, rid)
            items = self.drawable(m)
            # until a long walk has measured the zone's zoom, draw at the (snapped) zoom of the first run
            ref = m.get("ref_k") or (items[0][1]["k"] if items else None)
            m["draw_k"] = ref
            d = self._dir(place)
            if not ref or not items:
                return None
            kx, ky = ref, ref / self.ASPECT
            items = [(c, R) for c, R in items if self.img(place, c) is not None]
            if not items:
                return None
            pts = [(kx * c["x"], ky * c["y"], (self.img(place, c).width / 2) * ref / R["k"]) for c, R in items]
            X0 = int(min(p[0] - p[2] for p in pts)) - 2
            Y0 = int(min(p[1] - p[2] for p in pts)) - 2
            W = int(max(p[0] + p[2] for p in pts)) + 2 - X0
            H = int(max(p[1] + p[2] for p in pts)) + 2 - Y0
            if W * H > 9000 * 9000:
                self.status = f"{place}: map too big to draw ({W}x{H})"
                return None
            layers = sorted({R["layer"] for c, R in items})
            out = {}
            for L in layers:
                canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                shots = []
                for c, R in items:
                    if R["layer"] != L:
                        continue
                    im = self.img(place, c)
                    if im is None:
                        continue
                    f = ref / R["k"]
                    if abs(f - 1) > 0.01:
                        w = max(8, int(round(im.width * f)))
                        im = im.resize((w, w), Image.BILINEAR)
                    # usable radius: inside the frame, minus a few px of rim
                    shots.append((kx * c["x"] - X0, ky * c["y"] - Y0, im, (im.width / 2) - 6 * f))
                # squares -> nearest shots that fully cover them
                sq = {}
                for i, (px, py, im, rr) in enumerate(shots):
                    reach = rr - G * 0.72
                    if reach <= 0:
                        continue
                    for qx in range(int((px - reach) // G), int((px + reach) // G) + 1):
                        for qy in range(int((py - reach) // G), int((py + reach) // G) + 1):
                            dd = math.hypot((qx + 0.5) * G - px, (qy + 0.5) * G - py)
                            if dd <= reach:
                                sq.setdefault((qx, qy), []).append((dd, i))
                for (qx, qy), cand in sq.items():
                    cand.sort()
                    for dd, i in reversed(cand[:8]):          # nearest last = on top; the others fill its holes (arrow)
                        px, py, im, rr = shots[i]
                        bx0, by0 = qx * G, qy * G
                        ix, iy = int(round(bx0 - (px - im.width / 2))), int(round(by0 - (py - im.height / 2)))
                        tile = im.crop((ix, iy, ix + G, iy + G))
                        canvas.alpha_composite(tile, (bx0, by0))
                name = safe(place) + (f"_L{L}" if L else "") + ".png"
                canvas.save(os.path.join(d, name + ".tmp"), "PNG")
                os.replace(os.path.join(d, name + ".tmp"), os.path.join(d, name))
                out[L] = name
            for f in os.listdir(d):                          # drop pictures of layers that no longer exist
                if f.startswith(safe(place) + "_L") and f.endswith(".png") and f not in out.values():
                    try:
                        os.remove(os.path.join(d, f))
                    except OSError:
                        pass
            m["origin"], m["size"], m["drawn"] = [X0, Y0], [W, H], {str(k): v for k, v in out.items()}
            self.save(place)
            nl = len(layers)
            self.status = f"{place}: {len(items)} shots drawn, scale {ref:.1f} px/%" + (f", {nl} levels" if nl > 1 else "")
            return out

    # ------------------------------------------------------------------ self-correction
    def refile(self, src, clip_ids, dst):
        """Shots filed under the wrong zone (the log, written later, says when you were really in dst):
        move them to dst. Their coords are right - they're dst's coords. dst=None: drop them (e.g. the
        coords were on a continent map). Each moved run starts undecided in dst and is re-checked there."""
        import shutil
        with self.lock:
            ms = self.meta(src)
            ids = set(clip_ids)
            moving = [c for c in ms["clips"] if c["id"] in ids]
            if not moving:
                return 0
            ms["clips"] = [c for c in ms["clips"] if c["id"] not in ids]
            sd = os.path.join(self._dir(src), "clips")
            if dst:
                md = self.meta(dst)
                dd = os.path.join(self._dir(dst), "clips")
                os.makedirs(dd, exist_ok=True)
                for c in sorted(moving, key=lambda c: c["t"]):
                    rid = "rf-" + safe(src) + "-" + c["run"]
                    R0 = ms["runs"].get(c["run"]) or {}
                    R = md["runs"].setdefault(rid, {"k": R0.get("k"), "p": [], "layer": None, "n": 0,
                                                    "t0": c["t"], "label": c.get("label"), "refiled_from": src})
                    R["n"] += 1
                    R["t1"] = c["t"]
                    nc = {k: v for k, v in c.items() if k not in ("vote", "sim", "vote_n", "bad", "file")}
                    nc["id"] = (max([o["id"] for o in md["clips"]] or [0])) + 1
                    nc["run"] = rid
                    if "file" in c and os.path.exists(os.path.join(sd, c["file"])):
                        nc["file"] = f"c{nc['id']:05d}.png"
                        shutil.move(os.path.join(sd, c["file"]), os.path.join(dd, nc["file"]))
                        self._img.pop((src, c["file"]), None)
                    md["clips"].append(nc)
                for rid in {"rf-" + safe(src) + "-" + c["run"] for c in moving}:
                    self._resolve_run(dst, md, rid)
                self.save(dst)
                self.dirty[dst] = self.dirty.get(dst) or time.time()
            else:
                for c in moving:
                    if "file" in c:
                        try:
                            os.remove(os.path.join(sd, c["file"]))
                        except OSError:
                            pass
            for rid in {c["run"] for c in moving}:
                if not any(c["run"] == rid for c in ms["clips"]):
                    ms["runs"].pop(rid, None)
            self.last.pop(src, None)
            self.save(src)
            self.dirty[src] = self.dirty.get(src) or time.time()
            return len(moving)

    # ------------------------------------------------------------------ for the pages
    def neighbours(self, place):
        """Other zones' maps, as rectangles in THIS zone's map % (the seamless map: crossing a zone line
        just keeps going). Only zones whose world rectangle is near this one."""
        out = []
        bb = zone_bounds()
        me = bb.get(place)
        if not me:
            return out
        try:
            dirs = [x for x in os.listdir(self.root) if os.path.isfile(os.path.join(self.root, x, "meta.json"))]
        except OSError:
            dirs = []
        for x in dirs:
            try:
                m = self.places.get(x.replace("_", " ")) or json.load(open(os.path.join(self.root, x, "meta.json"), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            z = m.get("place") or x.replace("_", " ")
            if z == place or not m.get("drawn") or not (m.get("draw_k") or m.get("ref_k")) or "0" not in m["drawn"]:
                continue
            f = pct_to_pct(z, place)
            if not f:
                continue
            ax, bx, ay, by = f
            k = m.get("draw_k") or m["ref_k"]
            X0, Y0 = m["origin"]
            W, H = m["size"]
            x0, x1 = ax * (X0 / k) + bx, ax * ((X0 + W) / k) + bx
            y0, y1 = ay * (Y0 / (k / ASPECT)) + by, ay * ((Y0 + H) / (k / ASPECT)) + by
            if x1 < -60 or x0 > 160 or y1 < -60 or y0 > 160:
                continue                                   # far away
            p = os.path.join(self.root, x, m["drawn"]["0"])
            if not os.path.exists(p):
                continue
            out.append({"place": z, "url": f"/quad/{x}/{m['drawn']['0']}?v={int(os.path.getmtime(p))}",
                        "rect": [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)]})
        return out

    def info(self, place):
        m = self.meta(place)
        drawn = m.get("drawn") or {}
        if not drawn or not (m.get("ref_k") or m.get("draw_k")):
            nb = self.neighbours(place)
            if not nb:
                return None
            # nothing of this zone yet: still show the zones around it (same yards per pixel as the first)
            bb = zone_bounds()
            k = 29.0
            try:
                n0 = self.meta(nb[0]["place"])
                k = n0["ref_k"] * (bb[place][2] - bb[place][3]) / (bb[nb[0]["place"]][2] - bb[nb[0]["place"]][3])
            except Exception:
                pass
            return {"url": None, "unit": "coords", "floors": [], "floor": 0, "pinned": True, "k": k, "kx": k,
                    "ky": k / ASPECT, "origin": [0, 0], "size": [0, 0], "snaps": 0, "quad": True, "neighbors": nb}
        d = self._dir(place)

        def url(name):
            p = os.path.join(d, name)
            return f"/quad/{safe(place)}/{name}?v={int(os.path.getmtime(p))}" if os.path.exists(p) else None
        floors = [{"id": int(L), "url": url(n)} for L, n in sorted(drawn.items(), key=lambda kv: int(kv[0])) if url(n)]
        if not floors:
            return None
        cur = self.cur_layer.get(place, floors[0]["id"])
        if cur not in [f["id"] for f in floors]:
            cur = floors[0]["id"]
        k = m.get("draw_k") or m["ref_k"]
        return {"url": next(f["url"] for f in floors if f["id"] == cur), "unit": "coords", "floors": floors,
                "floor": cur, "pinned": True, "k": k, "kx": k, "ky": k / ASPECT, "origin": m["origin"],
                "size": m["size"], "snaps": len(self.drawable(m)), "quad": True,
                "neighbors": self.neighbours(place)}

    def places_list(self):
        out = []
        try:
            dirs = [x for x in os.listdir(self.root) if os.path.isfile(os.path.join(self.root, x, "meta.json"))]
        except OSError:
            dirs = []
        for x in dirs:
            try:
                m = json.load(open(os.path.join(self.root, x, "meta.json"), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"place": m.get("place") or x.replace("_", " "), "unit": "coords",
                        "shots": len(m.get("clips") or []), "pinned": bool(m.get("drawn")), "size": m.get("size"),
                        "updated": max([c.get("t", 0) for c in m.get("clips") or []] or [0])})
        return out


def rebuild(place, root=QROOT, raw_root=None):
    """Rebuild a zone's quadrant map from the raw clips (every session, in time order)."""
    from minimapper import RAW_ROOT
    import glob
    import shutil
    raw_root = raw_root or RAW_ROOT
    d = os.path.join(root, safe(place))
    if os.path.isdir(d):
        shutil.rmtree(d)
    q = QuadMap(root)
    recs = []
    for sess in sorted(glob.glob(os.path.join(raw_root, safe(place), "*"))):
        try:
            for ln in open(os.path.join(sess, "clips.jsonl"), encoding="utf-8"):
                r = json.loads(ln)
                if r.get("unit") == "coords":
                    r["path"] = os.path.join(sess, r["file"])
                    recs.append(r)
        except OSError:
            continue
    recs.sort(key=lambda r: r["t"])
    res = {}
    for r in recs:
        try:
            im = Image.open(r["path"]).convert("RGBA")
        except OSError:
            continue
        n = im.width // 2
        a = np.minimum(np.asarray(im)[..., 3], np.asarray(disc_mask(n)))
        im.putalpha(Image.fromarray(a))
        what = q.add(place, im, r["x"], r["y"], r["t"], r.get("label"))
        res[what] = res.get(what, 0) + 1
    q.compose(place)
    return q, res
