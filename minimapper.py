"""Builds maps of places with no map picture (dungeons) by stitching minimap snapshots.

Every second the minimap circle is grabbed into a short buffer. A snapshot is KEPT only when it adds
something: the spot is in a grid cell we have no snapshot for yet, or it's better centred in its cell
than the one we have. Placement needs a position at the moment of the snapshot:
  - "world": your position from the combat log (yards). The log arrives late, so snapshots are kept
    ~3 min and matched to log events by timestamp. Dungeons use this; it also lines the picture up
    with pulls/route drawn from the log.
  - "coords": the coordinates under the minimap (map %), read live. Used where they exist (cities).
The scale (minimap pixels per unit) is learned by itself: the picture shift between two snapshots
(phase correlation) divided by how far you moved between them.

Saved per place in maps/minimap/<place>/: meta.json, kept snapshots, and <place>.png (the stitched map).
Read-only: screen pixels in, files out.
"""
import io
import json
import math
import os
import re
import threading
import time
from collections import deque

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "maps", "minimap")
# raw clips: every minimap picture taken + where it was, untouched by stitching, so any map can be rebuilt.
# maps/minimap_raw/<place>/<session YYYY-MM-DD_HHMM>/clipNNNN.png + clips.jsonl
RAW_ROOT = os.path.join(HERE, "maps", "minimap_raw")
DEFAULT = {"cx": 1677, "cy": 279, "r": 166}


# Things drawn ON the minimap that must never become map: my arrow (middle) and the day/night sun button
# (top right). Layout of 2026-10-09 (bigger minimap): sun centre +88,-133 px from the minimap centre.
ARROW_R = 13                            # (the user shrank the arrow: it reaches ~10 px from the centre)
SUN = (110, -158, 34)                   # (layout of 2026-10-09 evening: minimap radius ~180)


ARROW_BIG = 0                           # a character with another layout: its arrow in the layout's px (_grab_at)


def disc_mask(r):
    mask = Image.new("L", (2 * r, 2 * r), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse((2, 2, 2 * r - 2, 2 * r - 2), fill=255)
    ar = max(ARROW_R, ARROW_BIG)
    d.ellipse((r - ar, r - ar, r + ar, r + ar), fill=0)
    sx, sy, sr = r + SUN[0], r + SUN[1], SUN[2]
    d.ellipse((sx - sr, sy - sr, sx + sr, sy + sr), fill=0)
    return mask
MODE = "shots"          # "shots": only minimap shots taken at kills, placed by your logged location
                        # "track": also follow the minimap second by second and fill in between
K_GUESS = 1.24          # minimap px per yard in dungeons (indoor view, fully zoomed out); see restitch.k_indoor
K_GUESS_COORDS = 20.0   # px per map-% (zones differ a lot; only a first guess, the solve finds the real one)      # minimap circle on a 1920x1080 window (screen_layout "minimap")


def safe(name):
    return re.sub(r"[^A-Za-z0-9' -]", "", name).strip().replace(" ", "_") or "unknown"


def phase_shift(a, b):
    """Pixel shift that takes picture a to picture b, plus how sure we are (peak sharpness)."""
    a = a - a.mean()
    b = b - b.mean()
    win = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    fa, fb = np.fft.fft2(a * win), np.fft.fft2(b * win)
    r = fb * np.conj(fa)
    r /= np.abs(r) + 1e-9
    c = np.fft.ifft2(r).real
    iy, ix = np.unravel_index(np.argmax(c), c.shape)
    peak = c[iy, ix] / (c.std() + 1e-9)
    h, w = c.shape
    dy = iy - h if iy > h // 2 else iy
    dx = ix - w if ix > w // 2 else ix
    return dx, dy, peak


def phase_shift_sub(a, b):
    """phase_shift to a fraction of a pixel (a parabola through the peak and its neighbours). For a small
    minimap, where a step is about one pixel and whole pixels are too coarse to measure a scale with."""
    a = a - a.mean()
    b = b - b.mean()
    win = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    fa, fb = np.fft.fft2(a * win), np.fft.fft2(b * win)
    r = fb * np.conj(fa)
    r /= np.abs(r) + 1e-9
    c = np.fft.ifft2(r).real
    iy, ix = np.unravel_index(np.argmax(c), c.shape)
    peak = c[iy, ix] / (c.std() + 1e-9)
    h, w = c.shape

    def frac(lo, mid, hi):
        den = lo - 2 * mid + hi
        return 0.0 if abs(den) < 1e-12 else max(-0.5, min(0.5, 0.5 * (lo - hi) / den))
    fx = frac(c[iy, (ix - 1) % w], c[iy, ix], c[iy, (ix + 1) % w])
    fy = frac(c[(iy - 1) % h, ix], c[iy, ix], c[(iy + 1) % h, ix])
    return (ix - w if ix > w // 2 else ix) + fx, (iy - h if iy > h // 2 else iy) + fy, peak


def overlap_similarity(a_img, b_img, dx, dy, give=5):
    """How alike two minimap shots are where they overlap, with b placed (dx, dy) px from a, allowing
    a few px of give. Normalized correlation (-1..1) over pixels both show (not black, not off-circle).
    None if they barely overlap."""
    if a_img.size != b_img.size:                     # (shots from different minimap sizes: line their middles up)
        n_ = max(a_img.width, a_img.height, b_img.width, b_img.height)
        a_img, b_img = center_pad(a_img, n_), center_pad(b_img, n_)
    A = np.asarray(a_img.convert("RGBA"), dtype=float); B = np.asarray(b_img.convert("RGBA"), dtype=float)
    ga, gb = A[..., :3].mean(axis=2), B[..., :3].mean(axis=2)
    oka, okb = (A[..., 3] > 0) & (ga > 12), (B[..., 3] > 0) & (gb > 12)
    n = ga.shape[0]
    best = None
    for ox in range(int(round(dx)) - give, int(round(dx)) + give + 1, 2):
        for oy in range(int(round(dy)) - give, int(round(dy)) + give + 1, 2):
            # b pixel (i, j) sits on a pixel (i + oy, j + ox)
            ay0, ay1 = max(0, oy), min(n, n + oy)
            ax0, ax1 = max(0, ox), min(n, n + ox)
            if ay1 - ay0 < 20 or ax1 - ax0 < 20:
                continue
            pa, pb = ga[ay0:ay1, ax0:ax1], gb[ay0 - oy:ay1 - oy, ax0 - ox:ax1 - ox]
            ok = oka[ay0:ay1, ax0:ax1] & okb[ay0 - oy:ay1 - oy, ax0 - ox:ax1 - ox]
            if ok.sum() < 800:
                continue
            x, y = pa[ok] - pa[ok].mean(), pb[ok] - pb[ok].mean()
            c = float((x * y).sum() / (np.sqrt((x * x).sum() * (y * y).sum()) + 1e-9))
            best = c if best is None else max(best, c)
    return best


def is_blank(rgba):
    """A minimap showing nothing (open-world mines with no minimap picture, a black screen): an even
    field. Real minimap pictures have a brightness spread of 8+; blank ones under ~5."""
    a = np.asarray(rgba.convert("RGBA"), dtype=float)
    ok = a[..., 3] > 0
    if ok.sum() < 100:
        return True
    g = a[..., :3].mean(axis=2)[ok]
    # nearly every pixel the same shade (party/mob dots and your arrow are only a few pixels)
    return float((abs(g - np.median(g)) <= 3).mean()) > 0.85


# The minimap's indoor scale changed with the game's UI: shots kept from the old one (332 px across, minimap
# radius 166) were taken at 1.2563 px per yard (measured from the 2026-10-11 raw clips). A shot remembers its own
# scale ("kpx"); these are the ones saved before that existed, told apart by their width.
OLD_SHOT_K = {332: 1.2563}


def norm_place(name):
    """A place name as dungeon names are compared: lower case, no "the", single spaces."""
    return " ".join((name or "").lower().replace("the ", "").split()).strip(" .")


def k_indoor():
    """Minimap px per yard in dungeons right now (screen_layout minimap.px_per_yd_in)."""
    try:
        from restitch import k_indoor as _k
        return float(_k())
    except Exception:
        return K_GUESS


def shot_k(cell, width, now_k=None):
    """Px per yard a kept shot was taken at."""
    return float((cell or {}).get("kpx") or OLD_SHOT_K.get(width) or now_k or k_indoor())


def _rim_wedge(n, r0, a0, a1):
    ys, xs = np.mgrid[0:n, 0:n]
    dx, dy = xs - (n - 1) / 2, (n - 1) / 2 - ys
    ang = np.degrees(np.arctan2(dy, dx))
    return (np.hypot(dx, dy) > r0 * n / 2) & (np.hypot(dx, dy) <= n / 2) & (ang > a0) & (ang < a1)


def has_rim_button(im):
    """Is something drawn where the default layout's tracking button sits on the minimap's rim (measured on
    2026-11-21's clips: radius 0.89-0.99 of the disc, 49 to 66 degrees up from east)? Most of that patch lit."""
    a = np.asarray(im.convert("RGBA"))
    if a.shape[0] != a.shape[1]:
        return False
    w = _rim_wedge(a.shape[0], 0.9, 50, 65)
    return bool(w.any()) and float(((a[..., :3].max(axis=2) > 40) & (a[..., 3] > 0))[w].mean()) > 0.6


def cut_rim_button(im):
    """A shot from a character with the game's default layout has the tracking button's arc on its top-right
    rim (the button overlaps the minimap there); it was stamped along the dungeon map like a row of hooks.
    That bit of the rim is made see-through: radius over 0.72 of the disc, 38 to 82 degrees up from east."""
    a = np.asarray(im.convert("RGBA")).copy()
    n = a.shape[0]
    ys, xs = np.mgrid[0:n, 0:a.shape[1]]
    dx, dy = xs - (a.shape[1] - 1) / 2, (n - 1) / 2 - ys
    ang = np.degrees(np.arctan2(dy, dx))
    a[(np.hypot(dx, dy) > 0.72 * n / 2) & (ang > 38) & (ang < 82), 3] = 0
    return Image.fromarray(a)


def to_scale(im, k_from, k_to):
    """A shot resized from the scale it was taken at to another (old-UI shots onto today's map)."""
    if not k_from or not k_to or abs(k_to / k_from - 1) < 0.01:
        return im
    n = max(8, int(round(im.width * k_to / k_from / 2)) * 2)
    return im.resize((n, n), Image.LANCZOS)


def center_pad(im, n):
    """The picture in the middle of an n x n see-through square (bigger ones are trimmed to their middle)."""
    if im.width == n and im.height == n:
        return im
    out = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    out.paste(im.convert("RGBA"), ((n - im.width) // 2, (n - im.height) // 2))
    return out


def log_error(where):
    """An error in the mapper must never vanish: it used to be swallowed by the caller, and a whole evening of
    kill shots was dropped without a word. Written to minimap_errors.log (kept short)."""
    import traceback
    try:
        p = os.path.join(HERE, "minimap_errors.log")
        if os.path.exists(p) and os.path.getsize(p) > 400_000:
            with open(p, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-150_000:]
            with open(p, "w", encoding="utf-8") as f:
                f.write(tail)
        import time as _tm
        with open(p, "a", encoding="utf-8") as f:
            f.write(_tm.strftime("%Y-%m-%d %H:%M:%S ") + where + "\n" + traceback.format_exc() + "\n")
    except OSError:
        pass


def guarded(fn):
    """For the mapper's entry points called from the log thread: log the error, count it, carry on."""
    def wrap(self, *a, **k):
        try:
            return fn(self, *a, **k)
        except Exception as e:
            log_error(fn.__name__)
            self.dbg = getattr(self, "dbg", {})
            self.dbg["errors"] = self.dbg.get("errors", 0) + 1
            self.dbg["last_error"] = f"{fn.__name__}: {e}"
            self.status = f"map error in {fn.__name__}: {e} (see minimap_errors.log)"
    wrap.__name__ = fn.__name__
    wrap.__doc__ = fn.__doc__
    return wrap


class MinimapMapper(threading.Thread):
    """v2: movement comes from the pictures (each snapshot vs the one a second before = exact pixel
    shift), so the track is smooth even between log positions. Log positions / map coords only pin
    the track to the world (a fitted scale + offset), and re-pin it after a break (death run, loading)."""

    def __init__(self, grab, every=1.0):
        super().__init__(daemon=True)
        self._grab_raw, self.every = grab, every
        self.grab = self._grab_at             # (every grab goes to where the minimap is on THIS character)
        self.mm_spot = None                   # (cx, cy, r) the add-on's strip last gave for the minimap
        self.buf = deque(maxlen=900)          # dict(t, png, seg, lx, ly, r) — the last ~15 min (the log was 7.8 min late on 2026-11-21)
        self.dungeon_names = set()            # names (lower case, no "the") of dungeons and their rooms: see _near_dungeon
        self.places = {}
        self.lock = threading.Lock()
        self.meta_lock = threading.RLock()    # meta.json is written by the log thread and the mapper thread
        self.raw_lock = threading.Lock()      # ...and so is the raw archive (clip numbers)
        self.place_now = None
        self.unit_now = None
        self.coords_now = None
        self.seg = None                       # {"id", "place", "lx", "ly", "gray", "off": (ox, oy) | None}
        self.nseg = 0
        self.dirty = {}
        self.status = ""
        self.cgray = {}                       # place -> (gray canvas, x0, y0) for re-finding your spot
        self.here = {}                        # place -> (gx, gy) your last known spot on the canvas
        self.samples = {}                     # place -> recent logged positions with your track at that moment
        self.cur_layer = {}                   # place -> floor you're on (by picture)
        self.dot_fix = {}                     # (old) place -> your logged spot at the last kill + tracking then
        self.dead_t = {}                      # place -> when the log said you died there
        self.live = {}                        # place -> {x, y (world), seg, lx, ly, tfix}: your live spot
        self.raw_session = {}                 # place -> raw archive folder for this session
        self.raw_last = {}                    # place -> (x, y) of the last archived outdoor clip
        self.pending_floor = {}               # place -> a shot that matched no floor, waiting for a second
        self.track = deque(maxlen=1800)       # (t, seg, lx, ly, place): 30 min of where tracking had you
        from grids import Grids
        self.grids = Grids()                  # Mapping grids (the user's 0,0 design) and their tiled maps
        self.world = self.grids.world
        self.quad = self.world                # (older name)
        self.mapping_new = False
        self.label_now = None                 # the label above the minimap
        try:                                  # Mapping mode (user switch): no warps, only walking/flying
            self.mapping_on = bool(json.load(open(os.path.join(HERE, "mapping_mode.json"))).get("on"))
        except (OSError, ValueError):
            self.mapping_on = False
        self.mapping_lost = None
        self.flight = None                    # the path chain (Mapping mode / a flight)
        self.dbg = {}
        self.mapping_fast = True              # Mapping: ~4 position readings a second, shots by a worker
        import queue
        self.shot_q = queue.Queue(maxsize=30)
        self.use_beacon = False               # read the position add-on's strip (the companion switches it on)
        self.beacon = None                    # its newest reading
        threading.Thread(target=self._shot_worker, daemon=True).start()
        threading.Thread(target=self._compose_worker, daemon=True).start()
        self.rate = {"readings": 0, "shots": 0, "t0": time.time()}
        self._spool_load()

    def reload_maps(self):
        """After a restore / import: read the maps from disk again (Mapping is switched off first)."""
        from grids import Grids
        resolve, session = self.grids.resolve_label, self.grids.session
        self.grids = Grids(resolve_label=resolve)
        self.grids.session = session
        self.world = self.quad = self.grids.world
        for gid in self.grids.grids:
            self.world.dirty[gid] = time.time()          # draw their tiles

    def set_mapping(self, on, new=False):
        """Mapping mode: the user promises no warps (hearth, portals, relogs) - only walking and flying. Then
        every reading must continue the path on the world grid: a coords jump IS a zone line, at the same
        spot. A jump that continues nothing = a warp after all: mapping stops and says so."""
        self.mapping_on = bool(on)
        self.mapping_new = bool(new)
        self.mapping_lost = None
        self.flight = None
        self.grids.stop()
        self.grids.need = self.grids.lost = None
        self._locate_t = 0
        try:
            json.dump({"on": self.mapping_on}, open(os.path.join(HERE, "mapping_mode.json"), "w"))
        except OSError:
            pass

    def _spot(self):
        """Where the minimap really is, when that is clearly not where screen_layout "minimap" says: the
        add-on's strip gives it (beacon.minimap). None = use the layout (no add-on, or it agrees: the layout
        was measured by hand on the owner's main character and stays the reference for the scales)."""
        if not getattr(self, "use_beacon", False):
            return None
        try:
            import beacon
            m = beacon.minimap(beacon.latest(max_age=5))
            if m:
                self.mm_spot = m                       # (kept through a loading screen: it only changes at a relog)
        except Exception as e:
            self.dbg["spot_error"] = str(e)
        m, g = self.mm_spot, self.geom()
        # screen_layout "minimap" "frame" = what the add-on gives on the main character (1705, 245, r 168 on
        # 2026-11-19): the same frame = the layout as it is, untouched.
        fr = g.get("frame") or [g["cx"], g["cy"], g["r"] * 1.05]
        if not m or (abs(m[0] - fr[0]) <= 10 and abs(m[1] - fr[1]) <= 10 and abs(m[2] / fr[2] - 1) <= 0.08):
            return None
        return m

    def frame_r(self):
        """What ANOTHER layout's minimap radius is divided by to get the size of its pictures next to the main
        character's: the main character's own frame radius (screen_layout "minimap" "frame", 168 px). A minimap
        fully zoomed out shows 233 yd to its rim outdoors and 150 yd indoors on every character, so the radii
        say it all: measured 2026-11-20 on a 4-minute recording, the second character (r 99) slides 0.4235 px
        a yard outdoors = 99 / 233.7, and the main character's archived clips 0.72 = 168.3 / 233.7.
        (Until then this was 209 = 150 yd x 1.395: fitted in a city on the belief that both characters'
        indoor view had the same zoom - the main character's is one step in. It made every outdoor picture of
        the second character 24 % too big.) screen_layout "minimap" "frame_r" overrides it."""
        g = self.geom()
        try:
            fr = g.get("frame") or []
            return float(g.get("frame_r") or (fr[2] if len(fr) > 2 else 0) or 1.05 * g["r"])
        except (TypeError, ValueError, KeyError):
            return 168.0

    def _grab_at(self, box):
        """Every grab the mapper makes is asked for in the layout's terms (where and how big the minimap is on
        the main character). On a character whose minimap is somewhere else or another size - a new character
        has the game's default layout - the same box is taken where the minimap really is and brought to the
        layout's size, so the scales, masks and saved clips are the same on every character."""
        global ARROW_BIG
        import quadmap
        sp = self._spot()
        self._track = None
        if not sp:
            ARROW_BIG, quadmap.CORE_H = 0, 14
            return self._grab_raw(box)
        g = self.geom()
        f = sp[2] / self.frame_r()                     # this character's minimap next to the main character's
        # Your arrow is not shrunk on a default layout (it reaches ~9 px from the centre on the screen) and the
        # picture is enlarged by 1 / f, so the layout's 13-14 px masks leave most of it in: a thing that never
        # moves, and the picture's slide then reads 0 while you run (seen 2026-11-19: 313 of 353 steps read 0
        # with the 14 px mask, 87 with 34). Wider masks for the pictures and for the slide while this layout is on.
        ARROW_BIG, quadmap.CORE_H = int(round(10 / f)), int(round(16 / f))
        real = (int(round(sp[0] + (box[0] - g["cx"]) * f)), int(round(sp[1] + (box[1] - g["cy"]) * f)),
                int(round(sp[0] + (box[2] - g["cx"]) * f)), int(round(sp[1] + (box[3] - g["cy"]) * f)))
        img = self._grab_raw(real)
        if img is None:
            return None
        self.dbg["grab_moved"] = [list(sp), round(f, 3)]
        # The picture's slide is measured on the minimap as it is on the screen, not on the enlarged copy: on the
        # 2026-11-20 recording 82 % of 12-yd stretches read within 12 % of the scale at its own size (to a
        # fraction of a pixel), 13-50 % enlarged. _track = (the middle of the minimap, layout px per screen px).
        try:
            cx, cy, half = sp[0] - real[0], sp[1] - real[1], int(0.65 * g["r"] * f)
            x0, y0 = int(round(cx)) - half, int(round(cy)) - half
            if half >= 24 and x0 >= 0 and y0 >= 0 and x0 + 2 * half <= img.width and y0 + 2 * half <= img.height:
                t = np.asarray(img.convert("L"), dtype=float)[y0:y0 + 2 * half, x0:x0 + 2 * half].copy()
                t[half - 12:half + 12, half - 12:half + 12] = t.mean()     # your arrow (9 px from the centre)
                self._track = (t, 1.0 / f)
        except Exception as e:
            self.dbg["track_error"] = str(e)
        return img.resize((box[2] - box[0], box[3] - box[1]), Image.LANCZOS)

    def geom(self):
        try:
            from screenread import region
            g = region("minimap")
            return {**DEFAULT, **g} if isinstance(g, dict) else dict(DEFAULT)
        except Exception:
            return dict(DEFAULT)

    def meta(self, place):
        m = self.places.get(place)
        if m is None:
            mp = os.path.join(ROOT, safe(place), "meta.json")
            try:
                with open(mp, encoding="utf-8") as f:
                    m = json.load(f)
                if "cells" not in m:
                    raise ValueError
            except (OSError, ValueError) as e:
                if isinstance(e, ValueError):        # a broken file: keep it (never start an empty map over it)
                    try:
                        import time as _tm
                        os.replace(mp, mp + _tm.strftime(".broken_%Y%m%d_%H%M%S"))
                    except OSError:
                        pass
                m = {"place": place, "unit": None, "k": None, "k_samples": [], "r": None, "cells": {}, "wcells": {}}
            al = m.get("align") or {}
            if m.get("unit") == "world" and al.get("k"):
                try:
                    from restitch import k_indoor
                    ki = k_indoor()
                except Exception:
                    ki = K_GUESS
                if abs(al["k"] / ki - 1) > 0.03:
                    # solved at a zoom the minimap isn't at any more (2.30, the old zoomed-in minimap: every room
                    # came out twice; 1.26, the UI before 2026-11: new shots landed 11% off). Solve again at the
                    # real one - kept shots are resized to it (restitch.load).
                    m["align"] = None
                    self.dirty[place] = time.time() - 60
            self.places[place] = m
        return m

    def _save_meta(self, place):
        d = os.path.join(ROOT, safe(place))
        os.makedirs(d, exist_ok=True)
        txt = None
        for _ in range(4):                    # (another thread may add a shot while this one is written out)
            try:
                txt = json.dumps(self.places[place], indent=0)
                break
            except RuntimeError:
                continue
        if txt is None:
            return
        with self.meta_lock:                  # whole file to a side file first: a kill mid-write can't cut it short
            tmp = os.path.join(d, "meta.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(txt)
            for i in range(6):
                try:
                    os.replace(tmp, os.path.join(d, "meta.json"))
                    break
                except PermissionError:       # a page is reading it this instant (Windows)
                    if i == 5:
                        raise
                    import time as _tm
                    _tm.sleep(0.05)

    def run(self):
        while True:
            t0 = time.time()
            try:
                if not (self.place_now and self.unit_now != "coords") and t0 - getattr(self, "_keep_t", 0) >= self.every - 0.05:
                    # No dungeon run is known right now, so the branch below that keeps a frame a second for the
                    # kill shots is not running. But a run is often only known minutes later, when the late log
                    # says so (2026-11-21, the open-world Deadmines: 25 kills, 3 pictures). Near a dungeon the
                    # frames are kept anyway, so those kills find their picture when the log arrives.
                    self._keep_t = t0
                    if self._near_dungeon():
                        self._keep_frame()
                if self.mapping_on and self.unit_now == "coords" and self.mapping_fast:
                    if self.grids.cur:                     # a loading screen = a warp: the path ends there
                        g_ = self.geom()
                        im_ = self.grab((g_["cx"] - 20, g_["cy"] - 20, g_["cx"] + 20, g_["cy"] + 20))
                        if im_ is not None and np.asarray(im_.convert("L")).mean() < 6:
                            self.grids.loading()
                            self.mapping_lost = (self.grids.lost, t0)
                            self.status = self.grids.status
                    self._mapping_tick()
                elif self.mapping_on and self.unit_now == "coords":
                    # Mapping mode reads the coords itself: it doesn't need the companion to know the zone
                    if self.grids.cur:                     # a loading screen = a warp: the path ends there
                        g_ = self.geom()
                        im_ = self.grab((g_["cx"] - 20, g_["cy"] - 20, g_["cx"] + 20, g_["cy"] + 20))
                        if im_ is not None and np.asarray(im_.convert("L")).mean() < 6:
                            self.grids.loading()
                            self.mapping_lost = (self.grids.lost, t0)
                            self.status = self.grids.status
                    if t0 - getattr(self, "_coords_t", 0) >= (0.9 if getattr(self, "_fast", False) else 2):
                        self._coords_t = t0
                        self.coords_shot(self.place_now)
                elif self.place_now and self.unit_now == "coords":
                    pass                                   # outdoors without Mapping: nothing is mapped (user's call)
                elif self.place_now:
                    # once a second, whatever the loop's pace: with Mapping on the loop runs 4x a second, the
                    # 600 kept frames were then 2.5 minutes instead of 10, and a kill the log delivered later
                    # than that had no picture (the log is up to 5 minutes late in a dungeon)
                    if t0 - getattr(self, "_cap_t", 0) >= self.every - 0.05:
                        self._cap_t = t0
                        self._capture()
                    # a shot every 2 s; every second when moving fast (mounted, gryphon) so shots still overlap
                    if self.unit_now == "coords" and t0 - getattr(self, "_coords_t", 0) >= (0.9 if getattr(self, "_fast", False) else 2):
                        self._coords_t = t0
                        self.coords_shot(self.place_now)
                    self._mapped = self.place_now
                else:
                    self.seg = None
                    if getattr(self, "_mapped", None):     # just left: final stitch, then line it up with past routes
                        place, self._mapped = self._mapped, None
                        self.compose(place)
                        try:
                            import align
                            if self.meta(place).get("unit") == "world" and                                     (self.meta(place).get("align") or {}).get("source") != "log":
                                align.main(place)          # no log anchors: fit past routes to the map shape
                            self.places.pop(place, None)  # reload meta (align.py wrote it)
                        except Exception as e:
                            self.status = f"{place}: map saved; lining up with routes failed ({e})"
                for place in [p for p, v in list(self.dirty.items()) if v and time.time() - v > 8]:
                    self.compose(place)
                # (the world map is redrawn by its own thread - _compose_worker - a full redraw takes tens of
                # seconds and must never pause the position readings: a 30 s gap mid-flight lost the path)
            except Exception as e:
                self.status = f"error: {e}"
                import traceback
                try:
                    with open(os.path.join(HERE, "minimap_errors.log"), "a", encoding="utf-8") as f:
                        f.write(time.strftime("%H:%M:%S ") + traceback.format_exc() + "\n")
                except OSError:
                    pass
            time.sleep(max(0.02, (0.25 if (self.mapping_on and self.mapping_fast) else self.every) - (time.time() - t0)))

    # ------------------------------------------------------------- capture + track
    def _near_dungeon(self):
        """Does the add-on say you are in, or at, a dungeon: its instance flag, or a zone / minimap name that is a
        dungeon's or one of its rooms'?"""
        try:
            import beacon
            d = beacon.latest()
        except Exception:
            return False
        if not d:
            return False
        if d.get("instance"):
            return True
        tx = d.get("texts") or {}
        return any(norm_place(tx.get(k)) in self.dungeon_names for k in ("zone", "label") if tx.get(k))

    def _keep_frame(self):
        """One minimap picture into the buffer the kill shots are taken from (and the spool): nothing else."""
        g = self.geom()
        cx, cy, r = g["cx"], g["cy"], g["r"]
        img = self.grab((cx - r, cy - r, cx + r, cy + r))
        if img is None:
            return
        img = img.convert("RGB").resize((2 * r, 2 * r))
        import time as _tm
        bad_t, ok_t = getattr(self, "screen_bad_t", 0), getattr(self, "screen_ok_t", None)
        if np.asarray(img).mean() < 6 or _tm.time() - bad_t < 1.5 or (ok_t is not None and _tm.time() - ok_t > 3):
            return                                # a loading screen, or not the game
        img.putalpha(disc_mask(r))
        bio = io.BytesIO()
        img.save(bio, "PNG")
        fr = {"t": time.time(), "png": bio.getvalue(), "seg": "kept", "lx": 0.0, "ly": 0.0, "r": r}
        with self.lock:
            self.buf.append(fr)
        self._spool_save(fr)
        self.dbg = getattr(self, "dbg", {})
        self.dbg["frames_kept"] = self.dbg.get("frames_kept", 0) + 1

    def k_shot(self):
        """Minimap px per yard of an indoor picture taken right now, in the layout's terms. The main character's
        is the layout's measured one (its indoor zoom is one step in). A character with its own layout whose
        minimap is zoomed all the way out indoors shows 150 yd to the rim: the frame's radius / 150."""
        try:
            if self._spot():
                import beacon
                d = beacon.latest()
                if d and d.get("zoom") == 0:
                    return self.frame_r() / 150.0
        except Exception:
            pass
        return k_indoor()

    def _capture(self):
        place = self.place_now                # (the companion can switch mapping off at any moment)
        if not place:
            return
        g = self.geom()
        cx, cy, r = g["cx"], g["cy"], g["r"]
        img = self.grab((cx - r, cy - r, cx + r, cy + r))
        if img is None:
            return
        img = img.convert("RGB").resize((2 * r, 2 * r))
        import time as _tm                    # (the real clock: the screen reader's times are real)
        bad_t, ok_t = getattr(self, "screen_bad_t", 0), getattr(self, "screen_ok_t", None)
        not_game = _tm.time() - bad_t < 1.5 or (ok_t is not None and _tm.time() - ok_t > 3)
        if np.asarray(img).mean() < 6 or not_game:
            # loading screen (black, or one of Forever's artwork ones - the screen reader no longer sees your
            # player frame): the track breaks, nothing from it is kept as a map piece
            self.seg = None
            self.loading_t = time.time()      # ...and the live spot can't be trusted until the log fixes it
            return
        mask = disc_mask(r)
        rgba = img.copy()
        rgba.putalpha(mask)
        bio = io.BytesIO()
        rgba.save(bio, "PNG")
        s = int(r * 1.3)
        gray = np.asarray(img.convert("L"), dtype=float)[r - s // 2:r + s // 2, r - s // 2:r + s // 2].copy()
        gray[s // 2 - 14:s // 2 + 14, s // 2 - 14:s // 2 + 14] = gray.mean()
        sg = self.seg
        if sg and sg["place"] == place and sg["gray"].shape == gray.shape:
            dx, dy, peak = phase_shift(sg["gray"], gray)
            # no faster than ~12 yd/s on foot: a bigger slide is a view/floor change or a false match
            kk = (self.meta(place).get("align") or {}).get("k") or (k_indoor() if self.unit_now == "world" else 99)
            dt = max(0.5, time.time() - sg.get("t", time.time() - 1))
            if peak > 10 and math.hypot(dx, dy) < min(s * 0.35, kk * 12 * dt):
                lx, ly = sg["lx"] - dx, sg["ly"] - dy          # the picture slides opposite to you
                # measured against a KEYFRAME, not only step by step: small steps are rounded to whole pixels
                # (biased toward 0), so chained steps drift short - worse at the zoomed-out 1.24 px/yd
                kf = sg.get("kf")
                if kf is not None and kf.shape == gray.shape:
                    kx, ky, kpk = phase_shift(kf, gray)
                    kl = (sg["kf_l"][0] - kx, sg["kf_l"][1] - ky)
                    if kpk > 10 and math.hypot(kx, ky) < s * 0.4 and math.hypot(kl[0] - lx, kl[1] - ly) <= 3 + 0.35 * math.hypot(kx, ky):
                        lx, ly = kl
                        if math.hypot(kx, ky) > 0.2 * s:
                            sg["kf"], sg["kf_l"] = gray, (lx, ly)
                    elif peak < 25:
                        # a weak step match the keyframe doesn't back up: the picture changed (stairs to another
                        # floor, a portal) - not a move. (Real steps match at 46+; repeating tiles can fool the
                        # longer keyframe match, so a STRONG step match is trusted on its own.)
                        sg = None
                    else:
                        sg["kf"], sg["kf_l"] = gray, (lx, ly)
                else:
                    sg["kf"], sg["kf_l"] = gray, (lx, ly)
                if sg is not None:
                    sg["lx"], sg["ly"] = lx, ly
                    sg["gray"] = gray
                    sg["t"] = time.time()
            else:
                sg = None
        else:
            sg = None
        if sg is None:
            self.nseg += 1
            self.track_ids = getattr(self, "track_ids", 0) + 1
            m = self.meta(place)
            off = (0.0, 0.0) if not m["cells"] else None      # a brand-new place: this track is the canvas
            sg = self.seg = {"t": time.time(), "id": f"{int(time.time())}-{self.nseg}", "place": place, "lx": 0.0, "ly": 0.0, "gray": gray, "off": off,
                             "kf": gray, "kf_l": (0.0, 0.0)}
        if sg["off"] is None and MODE != "shots":    # lost: look for this view on the map built so far
            full = np.asarray(img.convert("L"), dtype=float)
            hit = self._relocate(place, full)
            if hit:
                sg["off"] = (hit[0] - sg["lx"], hit[1] - sg["ly"])
        fr = {"t": time.time(), "png": bio.getvalue(), "seg": sg["id"], "lx": sg["lx"], "ly": sg["ly"], "r": r}
        self.track.append((fr["t"], sg["id"], sg["lx"], sg["ly"], place))
        lv = self.live.get(place)
        if lv:
            if lv["seg"] == sg["id"]:
                k = (self.meta(place).get("align") or {}).get("k") or k_indoor()
                lv["y"] -= (sg["lx"] - lv["lx"]) / k          # picture px -> yards (u = -y, v = -x)
                lv["x"] -= (sg["ly"] - lv["ly"]) / k
            lv["seg"], lv["lx"], lv["ly"] = sg["id"], sg["lx"], sg["ly"]   # a hiccup: carry on from here
        with self.lock:
            self.buf.append(fr)
        self._spool_save(fr)
        if MODE == "shots":
            if self.unit_now == "world":
                self._maybe_fill(place, rgba, bio.getvalue())
            return                            # (tracking also checks that kill shots are real minimap views)
        if sg["off"] is not None:
            self.here[place] = (sg["lx"] + sg["off"][0], sg["ly"] + sg["off"][1])
            self._commit(place, fr, sg["off"])
        else:
            self.status = f"{place}: lost my place on the map, looking for it"

    def _relocate(self, place, full):
        """Find a minimap view on the stitched map (near where you were last). -> canvas (gx, gy) or None."""
        if self.dirty.get(place) or place not in self.cgray:
            self.compose(place)
        cg = self.cgray.get(place)
        if cg is None:
            return None
        canvas, x0, y0 = cg
        n = full.shape[0]
        S = n * 3
        hx, hy = self.here.get(place, (x0 + canvas.shape[1] / 2, y0 + canvas.shape[0] / 2))
        cx0, cy0 = int(hx - x0 - S / 2), int(hy - y0 - S / 2)
        crop = np.full((S, S), np.nan)
        sx0, sy0 = max(0, cx0), max(0, cy0)
        sx1, sy1 = min(canvas.shape[1], cx0 + S), min(canvas.shape[0], cy0 + S)
        if sx1 <= sx0 or sy1 <= sy0:
            return None
        crop[sy0 - cy0:sy1 - cy0, sx0 - cx0:sx1 - cx0] = canvas[sy0:sy1, sx0:sx1]
        if np.isnan(crop).mean() > 0.9:
            return None
        crop = np.where(np.isnan(crop), np.nanmean(crop), crop)
        pad = np.full((S, S), full.mean())
        yy, xx = np.mgrid[:n, :n]
        inside = (xx - n / 2) ** 2 + (yy - n / 2) ** 2 < (n / 2 - 4) ** 2
        blk = np.where(inside, full, full.mean())
        pad[S // 2 - n // 2:S // 2 - n // 2 + n, S // 2 - n // 2:S // 2 - n // 2 + n] = blk
        dx, dy, peak = phase_shift(pad, crop)
        if peak < 10:
            return None
        return (cx0 + x0 + S / 2 + dx, cy0 + y0 + S / 2 + dy)

    # ------------------------------------------------------------- pinning to the world
    # v4: every pinned snapshot is placed from a logged position (px = k * world), with picture
    # tracking only bridging the few seconds between that log entry and the snapshot. Errors can't
    # pile up. k = median of (picture shift / distance walked) over many short clean steps.
    @staticmethod
    def _uv(unit, P):
        return (P[0], P[1]) if unit == "coords" else (-P[1], -P[0])     # world: x north, y west

    def _track_at(self, t):
        """Your tracked (seg, lx, ly) at time t, interpolated between the snapshots either side."""
        with self.lock:
            fs = list(self.buf)
        before = [f for f in fs if f["t"] <= t]
        after = [f for f in fs if f["t"] > t]
        a = before[-1] if before else None
        b = after[0] if after else None
        if a and b and a["seg"] == b["seg"] and b["t"] - a["t"] < 2.5:
            w = (t - a["t"]) / (b["t"] - a["t"])
            return a["seg"], a["lx"] + w * (b["lx"] - a["lx"]), a["ly"] + w * (b["ly"] - a["ly"])
        near = min((f for f in (a, b) if f), key=lambda f: abs(f["t"] - t), default=None)
        if near and abs(near["t"] - t) < 0.8:
            return near["seg"], near["lx"], near["ly"]
        return None

    @guarded
    def position(self, place, unit, t, P):
        """A logged position of yours (world yards) at log time t -> an anchor for the map solve:
        'at t you were at world (u, v), which is (dx, dy) pixels from piece N of the same track'."""
        dbg = self.dbg = getattr(self, "dbg", {})
        dbg["calls"] = dbg.get("calls", 0) + 1
        dbg["lag"] = round(time.time() - t, 1)
        if unit == "world" and place == self.place_now:
            self.fix_live(place, t, float(P[0]), float(P[1]))
        m = self.meta(place)
        if m["unit"] is None:
            m["unit"] = unit
        if m["unit"] != unit:
            return
        # fresh position (the log just flushed, usually right after a kill): snap the minimap NOW and pin
        # that shot to this exact world spot
        if time.time() - t <= 4 and place == self.place_now and self.seg and self.seg["off"] is not None:
            self._snap_anchor(place, m, t, P, unit, kpx=self.k_shot() if unit == "world" else None)
        if MODE == "shots":
            return
        recs = [r for r in list(self.track) if r[4] == place]
        before = [r for r in recs if r[0] <= t]
        after = [r for r in recs if r[0] > t]
        a, b = (before[-1] if before else None), (after[0] if after else None)
        if a and b and a[1] == b[1] and b[0] - a[0] < 2.5:
            w = (t - a[0]) / (b[0] - a[0])
            seg, lx, ly = a[1], a[2] + w * (b[2] - a[2]), a[3] + w * (b[3] - a[3])
        elif a and t - a[0] < 1.0:
            seg, lx, ly = a[1], a[2], a[3]
        else:
            dbg["no_track"] = dbg.get("no_track", 0) + 1
            return
        mine = [(k, c) for k, c in m["cells"].items() if c.get("seg") == seg and "lx" in c]
        if not mine:
            dbg["no_piece"] = dbg.get("no_piece", 0) + 1
            return
        key, c = min(mine, key=lambda kc: math.hypot(kc[1]["lx"] - lx, kc[1]["ly"] - ly))
        u, v = self._uv(unit, (float(P[0]), float(P[1])))
        anc = m.setdefault("anchors", [])
        if anc and abs(anc[-1]["t"] - t) < 4 and math.hypot(anc[-1]["u"] - u, anc[-1]["v"] - v) < 3:
            return                                   # same spot, a moment later: nothing new
        anc.append({"t": round(t, 1), "u": round(u, 2), "v": round(v, 2), "piece": key,
                    "dx": round(lx - c["lx"], 1), "dy": round(ly - c["ly"], 1)})
        m["anchors"] = anc[-3000:]
        dbg["anchors"] = len(m["anchors"])
        self._save_meta(place)
        self.dirty[place] = self.dirty.get(place) or time.time()

    def coords_shot(self, place):
        """Outdoors: the minimap and the coordinates under it, read from ONE screen grab (so they're from
        the same moment), done twice a split second apart; both coordinate reads must agree."""
        from screenread import region
        from location import read_coords
        g = self.geom()
        cx, cy, r = g["cx"], g["cy"], g["r"]
        cb = region("coords")
        box = (min(cx - r, cb[0]), min(cy - r, cb[1]), max(cx + r, cb[2]), max(cy + r, cb[3]))

        def read():
            img = self.grab(box)
            if img is None:
                return None
            img = img.convert("RGB")
            if img.size != (box[2] - box[0], box[3] - box[1]):
                img = img.resize((box[2] - box[0], box[3] - box[1]))
            mm = img.crop((cx - r - box[0], cy - r - box[1], cx + r - box[0], cy + r - box[1]))
            ct = img.crop((cb[0] - box[0], cb[1] - box[1], cb[2] - box[0], cb[3] - box[1]))
            xy = read_coords(ct)
            return (mm, xy[0], xy[1]) if xy else None
        dbg = self.dbg = getattr(self, "dbg", {})
        a = read()
        time.sleep(0.25)
        b = read()
        tol = 1.2 if (getattr(self, "_fast", False) or self.mapping_on) else 0.4   # (a gryphon moves ~1 % between the two reads)
        if not a or not b or abs(a[1] - b[1]) > tol or abs(a[2] - b[2]) > tol:
            dbg["coords_disagree"] = dbg.get("coords_disagree", 0) + 1
            return
        mm, x, y = b
        # misreads: the very edge of the map, or a jump faster than you can run (~4 %/s even in a small city)
        if not (0.5 < x < 99.5 and 0.5 < y < 99.5):
            dbg["coords_edge"] = dbg.get("coords_edge", 0) + 1
            return
        last = getattr(self, "_last_xy", None)
        now = time.time()
        if self.mapping_on:
            return self._mapping_shot(mm, x, y, now)
        if getattr(self, "_jumped_t", 0) > (getattr(self, "zone_decided_t", 0) or 0):
            self._last_xy = None                   # jumped, zone not decided again yet: wait
            return
        if last and last[0] != place:
            # mapping switched zone: a real zone change ALWAYS comes with a coords jump. Coords that just
            # carried on from the last read are still on the old zone's map: refuse, and make the companion
            # decide the zone again.
            dt_ = now - last[1]
            if dt_ < 30 and math.hypot(x - last[2], y - last[3]) <= 4 * dt_ + 1:
                dbg["zone_conflict"] = dbg.get("zone_conflict", 0) + 1
                self.place_conflict_t = now
                return
            self._last_xy = (place, now, x, y)
            self._settle_until = now + 2
            self.world.break_run(place)
            return
        if last and (now - last[1] < 30 and math.hypot(x - last[2], y - last[3]) > 4 * (now - last[1]) + 1):
            # a jump: zone change (the coords switch before the zone name does), hearth, flight...
            dbg["coords_jump"] = dbg.get("coords_jump", 0) + 1
            # the new coords are on a map we don't know yet: they're no evidence for or against the next
            # zone switch, and nothing is shot until the companion has decided the zone again
            self._last_xy = None
            self._jumped_t = now
            self._settle_until = now + 6
            self.await_map_after = now             # the companion waits for the log (or the label) to confirm the map
            self.last_jump_t = now
            self.world.break_run(place)        # and the run is broken: nothing carries across a jump
            return
        if last and now - last[1] < 10:
            self._fast = math.hypot(x - last[2], y - last[3]) / max(0.5, now - last[1]) > 0.35   # %/s
            try:                                   # speed in yards/s (zone sizes differ a lot in %)
                import quadmap
                b = quadmap.zone_bounds().get(place)
                yd = math.hypot((x - last[2]) / 100 * (b[2] - b[3]), (y - last[3]) / 100 * (b[0] - b[1])) if b else 0
                if yd / max(0.5, now - last[1]) > 20 and b:
                    # faster than any ground mount (gryphon, boat) outside Mapping mode: no shots - zones change
                    # too fast to decide. (Turn Mapping on to map from the air.)
                    self.flying_until = now + 10
                    self.last_jump_t = now
            except Exception:
                pass
        if now < getattr(self, "flying_until", 0):
            self._last_xy = None                   # coords read in the air (zone unsure) are no evidence later
            self.world.break_run(place)
            dbg["flying"] = dbg.get("flying", 0) + 1
            return
        self._last_xy = (place, now, x, y)
        if now < getattr(self, "_settle_until", 0):
            return
        lr = self.raw_last.get(place)
        if not lr or math.hypot(x - lr[0], y - lr[1]) >= 0.25:
            bio = io.BytesIO()
            mm.save(bio, "PNG")
            self.archive(place, "coords", now, (x, y), bio.getvalue(), kind="coords", label=self.label_now)
            self.raw_last[place] = (x, y)
        rgba = mm.convert("RGB").resize((2 * r, 2 * r))
        if np.asarray(rgba).mean() < 6:
            return                                  # loading screen
        rgba = rgba.convert("RGBA")
        rgba.putalpha(disc_mask(r))
        # (outside Mapping mode nothing goes on the map - the user dropped it: only Mapping places shots)

    def _mapping_tick(self):
        """One position reading: the minimap + the coords from ONE grab, a single (voted) coords read."""
        from screenread import region
        from location import read_coords
        g = self.geom()
        cx, cy, r = g["cx"], g["cy"], g["r"]
        cb = region("coords")
        box = (min(cx - r, cb[0]), min(cy - r, cb[1]), max(cx + r, cb[2]), max(cy + r, cb[3]))
        img = self.grab(box)
        track = getattr(self, "_track", None)           # (another layout: the minimap at its own size, see _grab_at)
        if img is None:
            return
        img = img.convert("RGB")
        if img.size != (box[2] - box[0], box[3] - box[1]):
            img = img.resize((box[2] - box[0], box[3] - box[1]))
        mm = img.crop((cx - r - box[0], cy - r - box[1], cx + r - box[0], cy + r - box[1]))
        ex = self._beacon()
        if ex:                                          # the position add-on: exact, no coords to read
            self.rate["readings"] += 1
            return self._mapping_shot(mm, ex["px"], ex["py"], time.time(), ex=ex, track=track)
        xy = read_coords(img.crop((cb[0] - box[0], cb[1] - box[1], cb[2] - box[0], cb[3] - box[1])))
        if not xy or not (0.5 < xy[0] < 99.5 and 0.5 < xy[1] < 99.5):
            return
        self.rate["readings"] += 1
        self._mapping_shot(mm, xy[0], xy[1], time.time())

    def _compose_worker(self):
        """Redraws the world map's changed tiles in the background, so tracking never waits for it."""
        import time as _tm                             # the real clock (tests swap the mapper's)
        while True:
            try:
                for place in [p for p, v in list(self.world.dirty.items()) if v and time.time() - v > 5]:
                    self.world.compose(place)
            except Exception as e:
                self.dbg["compose_error"] = str(e)
            _tm.sleep(1)

    def _shot_worker(self):
        """Saves Mapping shots (raw clip + the map) in the background, so position tracking never waits."""
        while True:
            job = self.shot_q.get()
            try:
                gid, rgba, mm, Gp, now, label, fid, x, y, view, k_hint = job
                bio = io.BytesIO()
                mm.save(bio, "PNG")
                mm_png = bio.getvalue()
                self.archive(gid, "grid", now, (Gp[0], Gp[1]), mm_png, kind="mapping", label=label,
                             frame=fid, zx=x, zy=y, mapping=True, view=view)
                what = self.world.add(gid, rgba, Gp[0], Gp[1], now, label, mapping=True, k_hint=k_hint, frame=fid)
                self.dbg["mapping_" + what] = self.dbg.get("mapping_" + what, 0) + 1
                self.rate["shots"] += 1
            except Exception as e:
                self.dbg["shot_worker_error"] = str(e)

    def _beacon(self):
        """The position add-on's strip, if it is on screen and you are out in the world -> what Mapping needs
        (continent, exact world spot, zone and coords), else None (then the coords + the path are used)."""
        if not self.use_beacon:
            return None
        try:
            import beacon
            import quadmap
            from worldmap import continent
            b = beacon.read()
            self.beacon = b
            if not b or not b["pos"] or not b["continent"] or b.get("fits") is False:
                return None
            zone = beacon.zone_name(b)
            if zone and continent(zone) != b["continent"]:
                zone = None
            if zone and b["rect"] and not quadmap.zone_bounds().get(zone):
                quadmap.note_bounds(zone, [round(v, 2) for v in b["rect"]])    # its rectangle, before the log gives it
            mp = b["map_pos"] or (0.0, 0.0)
            return {"cont": b["continent"], "wa": b["pos"][0], "wb": b["pos"][1], "zone": zone if b["map_pos"] else None,
                    "px": round(mp[0] * 100, 2), "py": round(mp[1] * 100, 2), "zoom": b["zoom"],
                    "indoors": b.get("indoors")}
        except Exception as e:
            self.dbg["beacon_error"] = str(e)
            return None

    def _mapping_shot(self, mm, x, y, now, ex=None, track=None):
        """Mapping mode: find yourself (label + coords) once, then every reading continues the path.
        ex = the position add-on's reading (see _beacon): then the spot is known, nothing is found or followed."""
        G = self.grids
        dbg = self.dbg
        if ex is None and self.use_beacon and self.mapping_fast is False:
            ex = self._beacon()
            if ex:
                x, y = ex["px"], ex["py"]
        if ex:
            self.mapping_lost = None
        if G.cur is None and not ex:
            if G.lost and getattr(G, "last_loss", None) and not getattr(self, "_loss_logged", None) == G.last_loss["t"]:
                self._loss_logged = G.last_loss["t"]
                try:
                    with open(os.path.join(HERE, "zone_decisions.log"), "a", encoding="utf-8") as f:
                        f.write(time.strftime("%H:%M:%S ") + "Mapping lost: " + json.dumps(G.last_loss) + "\n")
                except OSError:
                    pass
            if G.lost and not self.mapping_new:
                # paused: pick the path up again from the last good spot - by the path, never the label
                if G.recover(x, y, now):
                    self.mapping_lost = None
                else:
                    lg = getattr(G, "last_good", None)
                    if lg and now - lg[5] <= 300:
                        self.status = G.status
                        return
                    # too long ago to pick the path up (a dungeon run, a hearth): find the spot afresh below.
                    # (It used to stay paused until Mapping was switched off and on again.)
            if G.need == "choose" and not self.mapping_new and now - getattr(self, "_locate_t", 0) < 5:
                return
            self._locate_t = now
            hint = self.place_now or getattr(self, "log_map", None)   # (label garbled: what the companion / log say)
            gid = G.start(self.label_now, x, y, now, new=self.mapping_new, zone_hint=hint)
            self.mapping_new = False
            self.status = G.status
            if not gid:
                return
        r = self.geom()["r"]
        rgba = mm.convert("RGB").resize((2 * r, 2 * r))
        if np.asarray(rgba).mean() < 6:
            return                                     # loading screen
        rgba = rgba.convert("RGBA")
        rgba.putalpha(disc_mask(r))
        prevG, prevT = (list(G.cur["G"]), G.cur["t"]) if G.cur else (None, None)
        # offer this reading as a shot when you've moved on from the last one offered (standing still: none)
        shot = None
        Gn = G.T(G.grids[G.cur["gid"]], G.cur["fid"], x, y) if G.cur else None
        if ex:
            Gn = G.exact_G(ex["cont"], ex["wa"], ex["wb"])[1]
        lo = getattr(self, "_last_offer", None)
        if Gn is None or not lo or math.hypot(Gn[0] - lo[0], Gn[1] - lo[1]) >= 15 or \
                (now - lo[2] >= 3 and math.hypot(Gn[0] - lo[0], Gn[1] - lo[1]) >= 4):
            shot = {"rgba": rgba, "mm": mm, "t": now, "label": self.label_now, "x": x, "y": y}
            if Gn is not None:
                self._last_offer = (Gn[0], Gn[1], now)
        if ex and ex["zoom"]:
            shot = None                                # the minimap is zoomed in: the path is kept, no pictures
        if ex:
            res = G.step_exact(rgba, ex["cont"], ex["wa"], ex["wb"], ex["zone"], x, y, self.label_now, now, shot=shot,
                               track=track, zoom=ex["zoom"], indoors=ex.get("indoors"))
        else:
            res = G.step(rgba, x, y, self.label_now, now, shot=shot)
        if G.cur and prevG:                            # how fast along the path: a shot every second when fast
            self._fast = math.hypot(G.cur["G"][0] - prevG[0], G.cur["G"][1] - prevG[1]) / max(0.5, now - prevT) > 12
        if shot is not None and G.cur:
            shot["fid"] = G.cur["fid"]
        if res[0] == "lost":
            self.mapping_lost = (res[1], now)
            self.status = G.status
            return
        if ex and ex["zoom"]:
            self.status = "Mapping: exact position - zoom the minimap all the way out to take pictures"
            return
        if res[0] != "place":
            self.status = res[1] if res[0] == "wait" and "zoom" in res[1] else G.status
            if ex and not (res[0] == "wait" and "zoom" in res[1]):
                self.status = f"Mapping '{G.grids[G.cur['gid']]['name']}' · exact position"
                if G.cur.get("still", 0) >= 40:        # you move, the picture doesn't: we are not looking at the minimap
                    self.status = ("Mapping: no pictures - the minimap picture doesn't move when you do. Is the minimap "
                                   "where the companion looks? (/reload for the add-on to say where it is)")
            return
        gid = res[1]
        # shots whose minimap view is confirmed (they come ~20 yd behind you): to the worker
        for s, Gp, view, kv in res[2]:
            if self.shot_q.full():
                try:
                    self.shot_q.get_nowait()           # behind: drop the oldest, keep up with you
                except Exception:
                    pass
            self.shot_q.put((gid, s["rgba"], s["mm"], list(Gp), s["t"], s["label"], s.get("fid"), s["x"], s["y"], view, kv))
        el = max(1.0, now - self.rate["t0"])
        self.status = f"Mapping '{G.grids[gid]['name']}' · {self.rate['readings'] / el:.1f} readings/s · {self.rate['shots']} shots"
        if ex:
            self.status += " · exact position"
        if el > 60:
            self.rate = {"readings": 0, "shots": 0, "t0": now}

    def _flight_shot(self, mm, x, y, now):
        """In the air: shots every second, ~30-40 yd apart. Each reading must land within reach (in the world)
        of the previous one; while the coords carry on smoothly it's the same zone, and when they jump to
        another zone's map, only one zone puts you where you just were. Lose the thread (a gap, two zones
        fitting, an unknown map) -> stop mapping until the companion decides again on the ground."""
        import quadmap
        fl = self.flight
        dbg = self.dbg
        bb = quadmap.zone_bounds()

        def world(z, px, py):
            b = bb.get(z)
            return None if not b else (b[0] - py / 100 * (b[0] - b[1]), b[2] - px / 100 * (b[2] - b[3]))
        dt = now - fl["t"]
        w0 = world(fl["zone"], fl["x"], fl["y"])
        mapping = fl.get("mode") == "mapping"
        if dt > (120 if mapping else 5) or not w0:      # (Mapping mode: no warps, so a pause in readings is fine)
            return self._flight_lost("no readings for %.0f s" % dt if w0 else "unknown map", now)
        d = sorted((math.hypot(w[0] - w0[0], w[1] - w0[1]), z) for z in bb for w in [world(z, x, y)] if w)
        reach = 40 + 50 * dt
        if not d or d[0][0] > reach or (len(d) > 1 and (d[1][0] < 3 * d[0][0] or d[1][0] - d[0][0] < 300)):
            return self._flight_lost(f"no clear zone ({d[:2]})", now)
        zone, step = d[0][1], d[0][0]
        if zone != fl["zone"]:                 # (on the world map a zone line is just more ground)
            dbg["flight_zone_changes"] = dbg.get("flight_zone_changes", 0) + 1
        speed = step / max(0.3, dt)
        fl.update(zone=zone, x=x, y=y, t=now)
        self.place_now = None if not self.place_now else zone
        # landed: slow for 5 s -> hand the zone back to the companion (it's known exactly)
        if mapping:
            self._fast = speed > 12
        elif speed < 12:
            fl["slow_since"] = fl["slow_since"] or now
            if now - fl["slow_since"] >= 5:
                self.flight = None
                self.flight_landed = (zone, x, y, now)
                self._last_xy = (zone, now, x, y)
                self.zone_decided_t = now
                self._jumped_t = 0
                dbg["landed"] = zone
        else:
            fl["slow_since"] = None
        bio = io.BytesIO()
        mm.save(bio, "PNG")
        lr = self.raw_last.get(zone)
        if not lr or math.hypot(x - lr[0], y - lr[1]) >= 0.25:
            self.archive(zone, "coords", now, (x, y), bio.getvalue(), kind="coords", label=self.label_now,
                         flight=fl.get("mode") != "mapping", mapping=fl.get("mode") == "mapping")
            self.raw_last[zone] = (x, y)
        r = self.geom()["r"]
        rgba = mm.convert("RGB").resize((2 * r, 2 * r))
        if np.asarray(rgba).mean() < 6:
            return
        rgba = rgba.convert("RGBA")
        rgba.putalpha(disc_mask(r))
        what = self.world.add_zone(zone, rgba, x, y, now, self.label_now, mapping=fl.get("mode") == "mapping")
        dbg["flight_" + what] = dbg.get("flight_" + what, 0) + 1

    def _flight_lost(self, why, now):
        if self.flight and self.flight.get("mode") == "mapping":
            # Mapping mode promised no warps, yet this reading continues nothing: stop and say so. Mapping
            # starts again once the zone is known for sure (picture / crossing / label).
            self.mapping_lost = (why, now)
            self.status = f"Mapping paused: that move didn't continue your path ({why})"
        self.flight = None
        self._last_xy = None
        self._jumped_t = now
        self.last_jump_t = now
        self.flying_until = now + 10          # (no zone decisions by label while still moving fast)
        self.dbg["flight_lost"] = why

    def looks_like(self, place, x, y):
        """Live minimap vs the zone's existing map at (x, y): best agreement, or None (nothing mapped)."""
        g = self.geom()
        cx, cy, r = g["cx"], g["cy"], g["r"]
        img = self.grab((cx - r, cy - r, cx + r, cy + r))
        if img is None:
            return None
        img = img.convert("RGB").resize((2 * r, 2 * r)).convert("RGBA")
        img.putalpha(disc_mask(r))
        if is_blank(img):
            return None
        gid, Gp = self.grids.place_zone(place, x, y)
        return self.world.matches(gid, img, Gp[0], Gp[1]) if gid else None

    def archive(self, place, unit, t, P, png, kind, **extra):
        """Keep the raw clip and its position, whatever the stitching later decides."""
        try:
            sess = self.raw_session.get(place)
            if not sess:
                import time as _tm                  # the real module (tests swap the mapper's clock)
                sess = os.path.join(RAW_ROOT, safe(place), _tm.strftime("%Y-%m-%d_%H%M", _tm.localtime(t)))
                os.makedirs(sess, exist_ok=True)
                self.raw_session[place] = sess
            with self.raw_lock:                    # (two threads save clips: never the same number twice)
                n = len([f for f in os.listdir(sess) if f.endswith(".png")]) + 1
                fn = f"clip{n:04d}.png"
                if kind == "kill":                     # several kills from one log write share one moment
                    seen = getattr(self, "_raw_seen", {})
                    if seen.get((sess, round(t, 1))):
                        return
                    seen[(sess, round(t, 1))] = fn
                    self._raw_seen = seen
                with open(os.path.join(sess, fn), "wb") as f:
                    f.write(png)
                self.dbg["raw_clips"] = self.dbg.get("raw_clips", 0) + 1
                g = self.geom()
                rec = {"file": fn, "t": round(t, 2), "saved": round(time.time(), 2), "unit": unit, "kind": kind,
                       "x": round(float(P[0]), 2), "y": round(float(P[1]), 2), "minimap": g}
                if unit == "world":
                    rec["kpx"] = round(self.k_shot(), 4)   # the scale this picture was taken at
                rec.update({k: (round(v, 2) if isinstance(v, float) else v) for k, v in extra.items()})
                with open(os.path.join(sess, "clips.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
        except Exception as e:
            self.dbg["raw_error"] = str(e)

    # ------------------------------------------------------------- frames kept across a restart
    # The log arrives minutes late, so a kill is matched to the minimap picture kept from its moment. Those
    # pictures lived only in memory: restart the companion and every kill of the minutes before it had no
    # picture ("kill_no_frame"). They're also written to maps/minimap_spool (10 min of them) and read back.
    @staticmethod
    def _spool_dir():
        return ROOT + "_spool"

    def _spool_save(self, fr):
        try:
            import time as _tm
            d = self._spool_dir()
            if not getattr(self, "_spool_ok", False):
                os.makedirs(d, exist_ok=True)
                self._spool_ok = True
            # as JPEG (~25 KB a frame, 15 MB for the 10 minutes; as PNG it was 180 MB rewritten non-stop)
            Image.open(io.BytesIO(fr["png"])).convert("RGB").save(
                os.path.join(d, f"{int(fr['t'] * 10)}.jpg"), "JPEG", quality=92)
            if _tm.time() - getattr(self, "_spool_prune_t", 0) > 30:
                self._spool_prune_t = _tm.time()
                for fn in os.listdir(d):
                    try:
                        if fn.endswith((".jpg", ".png")) and fr["t"] - int(fn[:-4]) / 10 > 960:
                            os.remove(os.path.join(d, fn))
                    except (OSError, ValueError):
                        pass
        except (OSError, ValueError):
            self._spool_ok = False

    def _spool_load(self):
        try:
            d = self._spool_dir()
            now = time.time()
            got = []
            for fn in os.listdir(d) if os.path.isdir(d) else []:
                try:
                    t = int(fn[:-4]) / 10
                except ValueError:
                    continue
                if not fn.endswith((".jpg", ".png")) or not 0 <= now - t <= 600:
                    continue
                if fn.endswith(".png"):
                    with open(os.path.join(d, fn), "rb") as f:
                        png = f.read()
                else:                                # back to what a kept frame is: the round minimap, as PNG
                    im = Image.open(os.path.join(d, fn)).convert("RGB")
                    im.putalpha(disc_mask(im.width // 2).resize(im.size))
                    bio = io.BytesIO()
                    im.save(bio, "PNG")
                    png = bio.getvalue()
                got.append({"t": t, "png": png, "seg": "before-restart", "lx": 0.0, "ly": 0.0, "r": 0})
            with self.lock:
                self.buf.extend(sorted(got, key=lambda f: f["t"]))
            if got:
                self.dbg["frames_from_before_restart"] = len(got)
        except (OSError, ValueError):
            pass

    @guarded
    def kill_snap(self, place, t_kill, my_last):
        """The log just flushed on a kill: you're standing about where your last logged position says."""
        tp, x, y = my_last
        dbg = self.dbg = getattr(self, "dbg", {})
        dbg["kills"] = dbg.get("kills", 0) + 1
        if t_kill - tp > 15:
            return
        # (the log says which map this kill was on, and the picture kept from that moment is of that map - even
        #  when the companion's idea of where you are NOW hasn't caught up: the first minutes behind a portal
        #  arrive in one late chunk, and every kill in it used to be dropped here)
        # the log can arrive minutes late: use the minimap AS IT WAS at that moment (frames are kept ~4 min)
        with self.lock:
            fr = min(self.buf, key=lambda f: abs(f["t"] - tp), default=None)
        recs = [r for r in list(self.track) if r[4] == place]
        tr = min(recs, key=lambda r: abs(r[0] - tp), default=None)
        self.fix_live(place, tp, x, y)
        if fr and abs(fr["t"] - tp) < 1.5:
            self.archive(place, "world", tp, (x, y), fr["png"], kind="kill", t_kill=t_kill)
            img = Image.open(io.BytesIO(fr["png"]))
            # (the scale it was taken at: a character with its own layout is not at the map's scale. Without it
            #  the shot was compared with the map as if it were - nothing matched, and 54 of a run's kill shots
            #  were held back as "another floor?" on 2026-11-21)
            self._snap_anchor(place, self.meta(place), tp, (x, y), "world", img=img, checked=True, kpx=self.k_shot())
        else:                                       # no minimap kept from that moment (e.g. just restarted):
            dbg["kill_no_frame"] = dbg.get("kill_no_frame", 0) + 1     # never guess with a newer picture

    def _maybe_fill(self, place, rgba, png):
        """Bottom layer: a minimap picture from walking, placed at your live (tracked, log-corrected) spot.
        It's drawn UNDER the kill shots, only shows where they left black/empty, and never takes part in
        the solve, so a badly placed one can't move or bend the map."""
        m = self.meta(place)
        al = m.get("align") or {}
        lv = self.live.get(place)
        if not al.get("k") or not lv or time.time() - lv.get("tfix", 0) > 300:
            return                                   # no log fix in the last 5 min: the live spot may have drifted
        if lv.get("tfix", 0) < max(getattr(self, "loading_t", 0), self.dead_t.get(place, 0)):
            return                                   # loading screen / death since the last fix: wait for a new one
        u, v = -lv["y"], -lv["x"]
        px, py = al["k"] * u + al.get("cx", 0.0), (al.get("ky") or al["k"]) * v + al.get("cy", 0.0)
        r = self.geom()["r"]
        layer = self.cur_layer.get(place, 0)
        if any(math.hypot(c["px"] - px, c["py"] - py) < 0.6 * r for c in m["cells"].values()
               if c.get("layer", 0) == layer):
            return                                   # something (shot or fill) already covers here
        if is_blank(rgba):
            return
        try:
            import restitch
            if restitch.has_menu(rgba):
                return
        except Exception:
            pass
        n = sum(1 for c in m["cells"].values() if c.get("fill")) + 1
        key = f"w{n}"
        while key in m["cells"]:
            key += "x"
        dd = os.path.join(ROOT, safe(place))
        os.makedirs(dd, exist_ok=True)
        open(os.path.join(dd, key + ".png"), "wb").write(png)
        now = time.time()
        m["cells"][key] = {"file": key + ".png", "px": round(px, 1), "py": round(py, 1), "off": 1000.0, "t": round(now, 1),
                           "u": round(u, 2), "v": round(v, 2), "layer": layer, "fill": True,
                           "fix_t": round(lv.get("tfix", 0), 1), "kpx": round(self.k_shot(), 4)}
        self.archive(place, "world", now, (lv["x"], lv["y"]), png, kind="walk", estimated=True)
        self.dbg["fills"] = self.dbg.get("fills", 0) + 1
        self._save_meta(place)
        self.dirty[place] = self.dirty.get(place) or now

    @guarded
    def on_death(self, place, t_dead):
        """You died (log time). Walking pieces placed after that from a position fixed before it are the
        ghost run / graveyard: remove them. The dot goes away until a new logged position."""
        self.dead_t[place] = max(self.dead_t.get(place, 0), t_dead)
        lv = self.live.get(place)
        if lv and lv.get("tfix", 0) < t_dead:
            self.live.pop(place, None)
        m = self.meta(place)
        bad = [k for k, c in m["cells"].items()
               if c.get("fill") and c.get("t", 0) >= t_dead - 2 and c.get("fix_t", 0) < t_dead]
        if bad:
            d = os.path.join(ROOT, safe(place))
            for k in bad:
                c = m["cells"].pop(k)
                try:
                    os.replace(os.path.join(d, c["file"]), os.path.join(d, "removed_" + c["file"]))
                except OSError:
                    pass
            self._save_meta(place)
            self.dirty[place] = self.dirty.get(place) or time.time()
        self.dbg["deaths"] = self.dbg.get("deaths", 0) + 1

    def fix_live(self, place, tp, x, y):
        """A logged position of yours (from time tp) corrects the live spot: that spot, plus how far the
        minimap has slid since tp (when tracking was unbroken since then)."""
        if time.time() - tp > 600:
            return                                   # replaying an old log: nothing live about it
        lv = self.live.get(place)
        if lv and tp < lv.get("tfix", 0):
            return                                   # an older position than the one we already used
        sg = self.seg
        recs = [r for r in list(self.track) if r[4] == place]
        rec = min(recs, key=lambda r: abs(r[0] - tp), default=None)
        if not rec or abs(rec[0] - tp) >= 1.5:
            return                                   # no tracking from that moment: can't bring it up to now
        # walked since tp: add up every tracked stretch after it (a break loses only that one second)
        k = (self.meta(place).get("align") or {}).get("k") or k_indoor()
        dl = [0.0, 0.0]
        after = [r for r in recs if r[0] >= rec[0]]
        for a, b in zip(after, after[1:]):
            if a[1] == b[1]:
                dl[0] += b[2] - a[2]
                dl[1] += b[3] - a[3]
        if sg and after and after[-1][1] == sg["id"]:
            dl[0] += sg["lx"] - after[-1][2]
            dl[1] += sg["ly"] - after[-1][3]
        nx, ny = x - dl[1] / k, y - dl[0] / k
        self.live[place] = {"x": nx, "y": ny, "seg": sg["id"] if sg else None, "lx": sg["lx"] if sg else 0.0,
                            "ly": sg["ly"] if sg else 0.0, "tfix": tp}
        self.dbg["fixes"] = self.dbg.get("fixes", 0) + 1

    def live_pos(self, place):
        """Your live spot in a dungeon (world x north, y west): moved every second by the minimap,
        corrected by every logged position of yours. None until the log has given one."""
        lv = self.live.get(place)
        return [round(lv["x"], 1), round(lv["y"], 1)] if lv else None

    def _snap_anchor(self, place, m, t, P, unit, img=None, checked=False, kpx=None):
        """A minimap shot pinned to a known spot (outdoors: the coords; dungeons: your spot at a kill).
        Floors: places can stack levels on the same x/y (Ironforge, many dungeons) and nothing gives
        height, so the picture decides: a shot goes on the floor whose shots it matches; if it matches
        none of the shots already at that spot, two such shots in a row start a new floor."""
        u, v = self._uv(unit, (float(P[0]), float(P[1])))
        shots = [c for c in m["cells"].values() if "u" in c and not c.get("fill")]
        if m["unit"] is None:
            m["unit"] = unit
        if m["unit"] != unit:
            return
        self.dbg = getattr(self, "dbg", {})
        if unit == "world" and len(shots) >= 3:
            # a spot thousands of yards from everything on this map is on another map (Forever's Deadmines: the
            # open-world mine and the instance behind its portal are 11,000 yd apart in the log's numbers). One
            # such shot made the picture 16,000 px tall, which nothing could draw.
            mu, mv = float(np.median([c["u"] for c in shots])), float(np.median([c["v"] for c in shots]))
            if math.hypot(u - mu, v - mv) > 3000:
                self.dbg["other_map"] = self.dbg.get("other_map", 0) + 1
                return
        g = self.geom()
        cx, cy, r = g["cx"], g["cy"], g["r"]
        if img is None:
            img = self.grab((cx - r, cy - r, cx + r, cy + r))
        if img is None:
            return
        k_now = k_indoor() if unit == "world" else None
        if unit == "world" and img.mode == "RGBA" and has_rim_button(img):
            img = cut_rim_button(img)                # (the default layout's tracking button, on the disc's rim)
        if kpx and k_now and abs(kpx / k_now - 1) >= 0.01:
            # a clip from another minimap size/zoom (rebuilding from the archive): bring it to today's scale,
            # keeping its own round edge
            had_alpha = img.mode == "RGBA"
            img = img.convert("RGBA")
            if not had_alpha:
                img.putalpha(disc_mask(img.width // 2).resize(img.size))
            img = to_scale(img, kpx, k_now)
            if np.asarray(img.convert("RGB")).mean() < 6:
                return
        else:
            img = img.convert("RGB").resize((2 * r, 2 * r))
            if np.asarray(img).mean() < 6:
                return
            img.putalpha(disc_mask(r))
        if not checked:                              # (a kept frame was checked when it was taken)
            import time as _tm
            if _tm.time() - getattr(self, "screen_bad_t", 0) < 1.5:
                return                               # a loading screen (Forever's are artwork, not black)
        if is_blank(img):
            self.dbg["blank"] = self.dbg.get("blank", 0) + 1
            return                                   # the minimap shows nothing here: no map piece, no floor
        try:
            import restitch
            if restitch.has_menu(img):
                return
        except Exception:
            pass
        sg = self.seg
        # must look like the minimap a second ago (not a loading screen / something drawn over it)
        if sg and sg["place"] == place and not checked:     # (a kept frame was already a live minimap view)
            s_ = sg["gray"].shape[0]
            r_ = img.width // 2
            now_g = np.asarray(img.convert("L"), dtype=float)[r_ - s_ // 2:r_ - s_ // 2 + s_, r_ - s_ // 2:r_ - s_ // 2 + s_].copy()
            now_g[s_ // 2 - 14:s_ // 2 + 14, s_ // 2 - 14:s_ // 2 + 14] = now_g.mean()
            if now_g.shape != sg["gray"].shape or phase_shift(sg["gray"], now_g)[2] < 10:
                self.dbg["shot_rejected"] = self.dbg.get("shot_rejected", 0) + 1
                return
        al = m.get("align") or {}
        k = al.get("k") or (K_GUESS_COORDS if unit == "coords" else k_indoor())   # first guess; the solve refines it
        ky = al.get("ky") or k
        px, py = k * u + al.get("cx", 0.0), ky * v + al.get("cy", 0.0)

        # ---- which floor?  No searching (repeating tiles fool a search): compare this shot with the
        # map at exactly the spot its position says (a few px of give). Same floor = strong overlap.
        layer = self.cur_layer.get(place, 0)
        solved = al.get("source") == "log" and len(shots) >= 6 and             ((1.0 < (al.get("k") or 0) < 5) if unit == "world" else (al.get("k") or 0) > 1)
        if solved:
            near = [c for c in shots if math.hypot(c["px"] - px, c["py"] - py) < 0.9 * r]
            if near:
                sims = {}
                for c in near:
                    try:
                        other = Image.open(os.path.join(ROOT, safe(place), c["file"])).convert("RGBA")
                    except OSError:
                        continue
                    if k_now:                        # (a shot kept from the old UI: compare at today's scale)
                        other = to_scale(other, shot_k(c, other.width, k_now), k_now)
                    sm = overlap_similarity(other, img, px - c["px"], py - c["py"])
                    if sm is not None:
                        L = c.get("layer", 0)
                        sims[L] = max(sims.get(L, -1), sm)
                best = max(sims, key=lambda L: sims[L], default=None)
                if best is not None and sims[best] >= 0.35:
                    layer = best
                    self.pending_floor.pop(place, None)
                elif sims:
                    # looks like nothing mapped here. Only a second, separate sighting (20 s and 10+ yd/units
                    # away, also unlike everything) makes it a new floor.
                    pend = self.pending_floor.get(place)
                    far = pend and time.time() - pend["t"] >= 20 and math.hypot(u - pend["u"], v - pend["v"]) >=                         (10 if unit == "world" else 0.5)
                    if not (pend and far and time.time() - pend["t"] < 300):
                        if not pend or time.time() - pend["t"] >= 300:
                            self.pending_floor[place] = {"img": img, "u": u, "v": v, "px": px, "py": py,
                                                         "t": time.time(), "tl": t}
                        self.dbg["floor_pending"] = self.dbg.get("floor_pending", 0) + 1
                        return
                    layer = max([c.get("layer", 0) for c in shots] + [0]) + 1
                    self.pending_floor.pop(place, None)
                    self._commit_shot(place, m, pend["img"], pend["u"], pend["v"], pend["px"], pend["py"], pend["tl"], layer, unit)
                    shots = [c for c in m["cells"].values() if "u" in c]      # the confirming shot counts for spacing
                    self.dbg["floors"] = layer + 1
        self.cur_layer[place] = layer

        # ---- spacing (on this floor)
        if unit == "coords":                         # outdoors: a new shot every ~half a minimap
            gap = 0.45 * r / k
        else:                                        # dungeons: every kill spot 12+ yd apart (anchors)
            gap = 12
        if any(math.hypot(c["u"] - u, c["v"] - v) < gap for c in shots if c.get("layer", 0) == layer):
            return                                   # already have a pinned shot near here on this floor
        self._commit_shot(place, m, img, u, v, px, py, t, layer, unit)

    def _commit_shot(self, place, m, img, u, v, px, py, t, layer, unit):
        shots = [c for c in m["cells"].values() if "u" in c]
        key = f"a{len(shots) + 1}"
        while key in m["cells"]:
            key += "x"
        dd = os.path.join(ROOT, safe(place))
        os.makedirs(dd, exist_ok=True)
        img.save(os.path.join(dd, key + ".png"))
        now = time.time()
        m["cells"][key] = {"file": key + ".png", "px": round(px, 1), "py": round(py, 1), "off": 0.0, "t": round(now, 1),
                           "u": round(u, 2), "v": round(v, 2), "lag": round(now - t, 1), "layer": layer}
        if unit == "world":
            m["cells"][key]["kpx"] = round(k_indoor(), 4)     # saved at today's scale
        # an anchor on itself: this shot's centre IS that world spot
        m.setdefault("anchors", []).append({"t": round(t, 1), "u": round(u, 2), "v": round(v, 2), "piece": key,
                                            "dx": 0.0, "dy": 0.0, "shot": True})
        self.dbg["shots"] = self.dbg.get("shots", 0) + 1
        floors = len({c.get("layer", 0) for c in m["cells"].values() if "u" in c})
        self.status = f"{place}: {len(m['cells'])} {'shots' if unit == 'coords' else 'kill shots'}, {self.dbg.get('raw_clips', 0)} raw clips saved" + (
            f", {floors} floors (you: floor {layer + 1})" if floors > 1 else "") + (
            (" (lined up with the coords)" if unit == "coords" else " (lined up with the log)")
            if (m.get("align") or {}).get("source") == "log" else "")
        self._save_meta(place)
        self.dirty[place] = self.dirty.get(place) or now

    # ------------------------------------------------------------- keeping snapshots
    def _commit(self, place, fr, off, key="cells"):
        m = self.meta(place)
        r = fr["r"]
        m["r"] = r
        cells = m.setdefault(key, {})
        px, py = fr["lx"] + off[0], fr["ly"] + off[1]
        # pieces are never replaced (that reshuffles the whole map): add one only where none is near
        gap = r * 0.6
        if any(math.hypot(px - c["px"], py - c["py"]) < gap for c in cells.values()):
            return
        dist = 0.0
        ck = f"p{len(cells) + 1}"
        while ck in cells:
            ck += "x"
        try:
            import restitch
            if restitch.has_menu(Image.open(io.BytesIO(fr["png"])).convert("RGBA")):
                return
        except Exception:
            pass
        d = os.path.join(ROOT, safe(place))
        os.makedirs(d, exist_ok=True)
        fn = ("w" if key == "wcells" else "") + ck + ".png"
        open(os.path.join(d, fn), "wb").write(fr["png"])
        cells[ck] = {"file": fn, "px": round(px, 1), "py": round(py, 1), "off": round(dist, 1), "t": round(fr["t"], 1),
                     "tx": round(px, 1), "ty": round(py, 1), "seg": fr["seg"],
                     "lx": round(fr["lx"], 1), "ly": round(fr["ly"], 1)}
        self._save_meta(place)
        self.dirty[place] = self.dirty.get(place) or time.time()
        na = len(m.get("anchors") or [])
        self.status = f"{place}: {len(m['cells'])} pieces, {na} log anchors" + (
            " (lined up with the log)" if (m.get("align") or {}).get("source") == "log" else "")

    # ------------------------------------------------------------- stitching
    def compose(self, place):
        self.dirty[place] = None
        m = self.meta(place)
        for key, name in (("cells", safe(place) + ".png"), ("wcells", safe(place) + "_w.png")):
            cells = list((m.get(key) or {}).values())
            if not cells:
                continue
            r = m["r"] or DEFAULT["r"]
            x0 = min(c["px"] for c in cells) - r
            y0 = min(c["py"] for c in cells) - r
            W = int(max(c["px"] for c in cells) + r - x0) + 1
            H = int(max(c["py"] for c in cells) + r - y0) + 1
            if W * H > 6000 * 6000:
                continue
            if key == "cells" and len(cells) >= 2:
                try:
                    import restitch
                    res = restitch.solve_place(place, m)
                    sg = self.seg
                    if res and sg and sg["off"] is not None and sg["place"] == place:
                        mine = [c for c in m["cells"].values() if c.get("seg") == sg["id"] and "tx" in c]
                        if mine:
                            last = max(mine, key=lambda c: c["t"])
                            dx, dy = last["px"] - last["tx"], last["py"] - last["ty"]
                            if dx or dy:
                                sg["off"] = (sg["off"][0] + dx, sg["off"][1] + dy)
                                for c in mine:              # tracked positions now include the correction
                                    c["tx"], c["ty"] = round(c["tx"] + dx, 1), round(c["ty"] + dy, 1)
                    if res:
                        x0, y0, W, H = res
                        self.cgray.pop(place, None)
                        im = Image.open(os.path.join(ROOT, safe(place), name))
                        a = np.asarray(im.convert("RGBA"), dtype=float)
                        self.cgray[place] = (np.where(a[..., 3] > 128, a[..., :3].mean(axis=2), np.nan), x0, y0)
                        m["origin"], m["size"] = [round(x0, 1), round(y0, 1)], [W, H]
                        continue
                except Exception as e:
                    self.status = f"restitch failed: {e}"
            canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = os.path.join(ROOT, safe(place))
            for c in sorted(cells, key=lambda c: -c["off"]):
                try:
                    im = Image.open(os.path.join(d, c["file"])).convert("RGBA")
                except OSError:
                    continue
                canvas.alpha_composite(im, (int(c["px"] - x0 - im.width / 2), int(c["py"] - y0 - im.height / 2)))
            canvas.save(os.path.join(d, name))
            if key == "cells":
                a = np.asarray(canvas, dtype=float)
                g = a[..., :3].mean(axis=2)
                self.cgray[place] = (np.where(a[..., 3] > 128, g, np.nan), x0, y0)
                m["origin"], m["size"] = [round(x0, 1), round(y0, 1)], [W, H]
            else:
                m["worigin"], m["wsize"] = [round(x0, 1), round(y0, 1)], [W, H]
        self._save_meta(place)

    def places_list(self):
        """Every place with a built map: name, unit, shots, lined up or not, last updated."""
        out = self.world.places_list()
        have = {p["place"] for p in out}
        try:
            dirs = [d for d in os.listdir(ROOT) if os.path.isfile(os.path.join(ROOT, d, "meta.json"))]
        except OSError:
            dirs = []
        for d in dirs:
            try:
                m = json.load(open(os.path.join(ROOT, d, "meta.json"), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            place = m.get("place") or d.replace("_", " ")
            if place in have or m.get("unit") == "coords":
                continue                             # outdoor maps are quadrant maps now
            cells = m.get("cells") or {}
            out.append({"place": place, "unit": m.get("unit"), "shots": len(cells),
                        "pinned": bool(m.get("align")), "size": m.get("size"),
                        "updated": max([c.get("t", 0) for c in cells.values()] or [0])})
        return sorted(out, key=lambda x: -x["updated"])

    def info(self, place):
        """Picture + placement for the pages. Pinned map: image pixel = k * (u, v) - worigin, which is
        the 'k/origin' form the pages use. Before that: the minimap-only map with your dot."""
        G = self.grids
        # (inside a dungeon place_now is the dungeon while G.cur is still the outdoor grid you walked in on:
        #  only an outdoor place may answer with that grid, or the Deadmines got Eastern Kingdoms as its picture)
        if self.mapping_on and G.cur and (place in (G.cur["gid"], None)
                                          or (place == self.place_now and self.unit_now == "coords")):
            q = G.info(G.cur["gid"])
            if q:
                return q
        if place in G.grids:
            q = G.info(place)
            if q:
                return q
        gid, _ = G.place_zone(place, 50, 50) if place else (None, None)
        if gid:
            q = G.info(gid, zone=place)
            if q:
                return q
        m = self.meta(place)
        if m.get("unit") == "coords":
            return None                              # old-style outdoor map: replaced by the quadrant map
        d = os.path.join(ROOT, safe(place))
        p = os.path.join(d, safe(place) + ".png")
        layers = sorted({c.get("layer", 0) for c in m["cells"].values()} or {0})
        cur = self.cur_layer.get(place, 0)

        def lurl(L):
            f = os.path.join(d, safe(place) + (f"_L{L}" if L else "") + ".png")
            return f"/minimap/{safe(place)}{f'_L{L}' if L else ''}.png?v={int(os.path.getmtime(f))}" if os.path.exists(f) else None
        floors = [{"id": L, "url": lurl(L)} for L in layers if lurl(L)]
        if cur not in layers:
            cur = 0
        if len(floors) > 1 and lurl(cur):
            p = os.path.join(d, safe(place) + (f"_L{cur}" if cur else "") + ".png")
        al = m.get("align")
        if al and m.get("origin") and os.path.exists(p):
            # fitted from past routes (align.py): image pixel = k * (u, v) + (cx, cy) - canvas origin
            return {"url": lurl(cur) or f"/minimap/{safe(place)}.png?v={int(os.path.getmtime(p))}", "unit": m.get("unit") or "world",
                    "floors": floors, "floor": cur,
                    "pinned": True, "k": al["k"], "kx": al["k"], "ky": al.get("ky") or al["k"],
                    "origin": [m["origin"][0] - al["cx"], m["origin"][1] - al["cy"]],
                    "size": m["size"], "snaps": len(m["cells"]), "fit": al.get("score")}
        if m.get("origin") and os.path.exists(p) and any("u" in c for c in m["cells"].values()):
            # shots are placed by your logged spots at a guessed scale until the solve has enough of them
            kg = K_GUESS_COORDS if m.get("unit") == "coords" else K_GUESS
            return {"url": lurl(cur) or f"/minimap/{safe(place)}.png?v={int(os.path.getmtime(p))}", "unit": m.get("unit") or "world",
                    "floors": floors, "floor": cur,
                    "pinned": True, "k": kg, "kx": kg, "ky": kg, "origin": m["origin"], "size": m["size"],
                    "snaps": len(m["cells"]), "provisional": True}
        pw = os.path.join(d, safe(place) + "_w.png")
        if m.get("k") and m.get("worigin") and os.path.exists(pw):
            return {"url": f"/minimap/{safe(place)}_w.png?v={int(os.path.getmtime(pw))}", "unit": m["unit"],
                    "pinned": True, "k": m["k"], "origin": m["worigin"], "size": m["wsize"],
                    "snaps": len(m["wcells"])}
        p = os.path.join(d, safe(place) + ".png")
        if not os.path.exists(p) or not m.get("origin"):
            return None
        h = self.here.get(place)
        return {"url": f"/minimap/{safe(place)}.png?v={int(os.path.getmtime(p))}", "unit": m["unit"],
                "pinned": False, "size": m["size"], "snaps": len(m["cells"]),
                "me": [h[0] - m["origin"][0], h[1] - m["origin"][1]] if h else None}
