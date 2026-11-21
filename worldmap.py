"""One map per continent in world yards (the user's "world grid", 2026-10-09).

Every zone map's coords convert to the game's own world position: the log's MAP_CHANGE line gives each
zone's rectangle in world yards (north X, south X, west Y, east Y). Shots of every zone go onto ONE
continent canvas at that position, so the map is seamless by construction: a zone line is just more
ground, zones are names on top. The same rules as the zone maps (quadmap.QuadMap) apply - runs, zoom
measured per run, levels by picture agreement, best-centred shot per square - only the unit is yards
(x = east, y = south; square, so ASPECT 1) and the zoom is in px per yard (the same everywhere).

The continent is drawn as T x T px tiles (a continent is far too big for one picture); a tile is only
redrawn when the shots that cover it changed. Files: maps/world/<continent>/meta.json, clips/,
tiles/L<layer>_<tx>_<ty>.png
"""
import hashlib
import math
import os
import time

import numpy as np

from PIL import Image, ImageFilter

import quadmap
from quadmap import QuadMap, G, zone_bounds
from minimapper import HERE, safe

WROOT = os.path.join(HERE, "maps", "world")
INDOOR_K = 1.0               # px/yd: shots above this are WoW's indoor minimap view (1.24 vs outdoor 0.81)
FLOOR_NEAR = 40              # yd: the nearest drawn shot this close says which level you're on
T = 1000                     # tile size in px (a multiple of the square size G)

EASTERN_KINGDOMS = {
    "Alterac Mountains", "Arathi Highlands", "Badlands", "Blasted Lands", "Burning Steppes", "Deadwind Pass",
    "Dun Morogh", "Duskwood", "Eastern Plaguelands", "Elwynn Forest", "Hillsbrad Foothills", "Ironforge",
    "Loch Modan", "Redridge Mountains", "Searing Gorge", "Silverpine Forest", "Stormwind City",
    "Stranglethorn Vale", "Swamp of Sorrows", "The Hinterlands", "Tirisfal Glades", "Undercity",
    "Western Plaguelands", "Westfall", "Wetlands", "Eversong Woods", "Ghostlands", "Silvermoon City",
}
KALIMDOR = {
    "Ashenvale", "Azshara", "Darkshore", "Darnassus", "Desolace", "Durotar", "Dustwallow Marsh", "Felwood",
    "Feralas", "Moonglade", "Mulgore", "Orgrimmar", "Silithus", "Stonetalon Mountains", "Tanaris", "Teldrassil",
    "The Barrens", "Thousand Needles", "Thunder Bluff", "Un'Goro Crater", "Winterspring",
    "Azuremyst Isle", "Bloodmyst Isle", "The Exodar",
}


def continent(zone):
    if zone in EASTERN_KINGDOMS:
        return "Eastern Kingdoms"
    if zone in KALIMDOR:
        return "Kalimdor"
    return None


def to_world(zone, px, py):
    """Zone map % -> world (east yards, south yards), or None without the zone's rectangle."""
    b = zone_bounds().get(zone)
    if not b:
        return None
    N, S, W, E = b
    return (-W + px / 100 * (W - E), -N + py / 100 * (N - S))


def _grow_in(a, holes, rounds=48):
    """Fills the see-through pixels marked in `holes` (an RGBA array's small gaps) from the drawn pixels around
    them, a ring at a time: each takes the average of its drawn neighbours. A gap wider than 2 x rounds px keeps
    a see-through middle rather than a guess."""
    known = a[..., 3] > 0
    rgb = a[..., :3].astype(np.float32)
    todo = holes & ~known
    for _ in range(rounds):
        if not todo.any():
            break
        tot = np.zeros_like(rgb)
        cnt = np.zeros(known.shape, dtype=np.float32)
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            k = np.roll(known, (dy, dx), axis=(0, 1))
            v = np.roll(rgb, (dy, dx), axis=(0, 1))
            if dy == 1: k[0, :] = False
            if dy == -1: k[-1, :] = False
            if dx == 1: k[:, 0] = False
            if dx == -1: k[:, -1] = False
            tot += v * k[..., None]
            cnt += k
        ring = todo & (cnt > 0)
        if not ring.any():
            break
        rgb[ring] = tot[ring] / cnt[ring][:, None]
        known = known | ring
        todo &= ~ring
    out = a.copy()
    filled = holes & known & (a[..., 3] == 0)
    out[..., :3][filled] = np.clip(rgb[filled], 0, 255).astype(np.uint8)
    out[..., 3][filled] = 255
    return out


class WorldMap(QuadMap):
    ASPECT = 1.0
    MOVE_MIN = 10.0          # yd
    QUANT = 5.0              # yd: one decimal of % is ~2-5 yd
    K_DEFAULT = 1.17         # px per yd (outdoor minimap, 2026-10-09 layout)
    K_MIN = 0.2
    FAR_MIN, FAR_SURE = 25.0, 70.0
    SPOT = 20.0

    def __init__(self, root=WROOT):
        super().__init__(root)

    # ------------------------------------------------------------------ zone % in, world out
    def add_zone(self, zone, img, px, py, t=None, label=None, mapping=False):
        cont, w = continent(zone), to_world(zone, px, py)
        if not cont or not w:
            return "no-world-position"
        return self.add(cont, img, w[0], w[1], t, label, mapping=mapping)

    def break_run(self, zone):
        cont = continent(zone)
        if cont:
            self.last.pop(cont, None)

    def matches_zone(self, zone, img, px, py):
        cont, w = continent(zone), to_world(zone, px, py)
        return self.matches(cont, img, w[0], w[1]) if cont and w else None

    def prior_ks(self, place):
        return []                # one zoom for the whole continent: nothing to borrow

    # ------------------------------------------------------------------ drawing (tiles)
    def compose(self, place):
        with self.lock:
            self.dirty[place] = None
            m = self.meta(place)
            for rid, R in list(m["runs"].items()):
                if R.get("n", 0) >= 2 and (not R.get("k") or R.get("layer") is None):
                    self._resolve_run(place, m, rid)
            items = [(c, R) for c, R in self.drawable(m) if self.img(place, c) is not None]
            # drawn at the sharpest zoom present (the indoor view's), so city detail is never shrunk away
            ref = max([R["k"] for c, R in items if R.get("k")] or [m.get("ref_k") or 0]) or None
            if not ref or not items:
                return None
            if m.get("draw_k") and abs(m["draw_k"] - ref) > 0.02 * ref:
                m["tiles"] = {}                       # the zoom everything is drawn at changed: redraw all
            m["draw_k"] = ref
            d = os.path.join(self._dir(place), "tiles")
            os.makedirs(d, exist_ok=True)
            old = m.get("tiles") or {}
            new = {}
            drawn = 0
            for L in sorted({R["layer"] for c, R in items}):
                shots = []
                for c, R in items:
                    if R["layer"] != L:
                        continue
                    shots.append((ref * c["x"], ref * c["y"], c, R))
                # squares (absolute canvas px) -> nearest shots that fully cover them
                sq = {}
                radius, near = {}, {}                 # each shot's drawn radius; shots by 250 px cell (hole filling)
                for i, (px, py, c, R) in enumerate(shots):
                    f = ref / R["k"]
                    rr = (self.img(place, c).width / 2 - 6) * f
                    radius[i] = rr
                    near.setdefault((int(px // 250), int(py // 250)), []).append(i)
                    reach = rr - G * 0.72
                    if reach <= 0:
                        continue
                    for qx in range(int((px - reach) // G), int((px + reach) // G) + 1):
                        for qy in range(int((py - reach) // G), int((py + reach) // G) + 1):
                            dd = math.hypot((qx + 0.5) * G - px, (qy + 0.5) * G - py)
                            if dd <= reach:
                                sq.setdefault((qx, qy), []).append((dd, i))
                tiles = {}
                for (qx, qy), cand in sq.items():
                    # anything taken in Mapping mode overrides every other shot of this spot
                    if any(shots[i][2].get("mapping") for dd, i in cand):
                        cand = [(dd, i) for dd, i in cand if shots[i][2].get("mapping")]
                    cand.sort()
                    tiles.setdefault((qx * G // T, qy * G // T), []).append(((qx, qy), [i for dd, i in cand[:8]]))
                lt = {}
                for (tx, ty), squares in tiles.items():
                    key = f"{tx},{ty}"
                    sig = hashlib.md5(repr(sorted(
                        (s, [(shots[i][2]["id"], round(shots[i][3]["k"], 3), bool(shots[i][2].get("mapping"))) for i in ids])
                        for s, ids in squares)
                    ).encode() + repr(round(ref, 3)).encode() + b"v2").hexdigest()
                    lt[key] = sig
                    name = f"L{L}_{tx}_{ty}.png"
                    if (old.get(str(L)) or {}).get(key) == sig and os.path.exists(os.path.join(d, name)):
                        continue                      # nothing changed under this tile
                    canvas = Image.new("RGBA", (T, T), (0, 0, 0, 0))
                    cache = {}

                    def piece(i, qx, qy):             # shot i's part of square (qx, qy), at the map's zoom
                        px, py, c, R = shots[i]
                        im = cache.get(i)
                        if im is None:
                            im = self.img(place, c)
                            if (R.get("k") or 0) >= INDOOR_K:   # indoor view: black around the buildings
                                a = np.asarray(im).copy()       # is not ground - see-through
                                a[..., 3] = np.where(a[..., :3].max(axis=2) < 12, 0, a[..., 3])
                                im = Image.fromarray(a)
                            f = ref / R["k"]
                            if abs(f - 1) > 0.01:
                                w = max(8, int(round(im.width * f)))
                                im = im.resize((w, w), Image.BILINEAR)
                                # enlarging blends the picture into the black behind its cut-outs: a dark line
                                # round every hole and rim. Those blended pixels (and 2 more) are cut away too.
                                al = im.getchannel("A").point(lambda v: 255 if v >= 250 else 0).filter(ImageFilter.MinFilter(5))
                                im.putalpha(al)
                            cache[i] = im
                        bx0, by0 = qx * G, qy * G
                        ix, iy = int(round(bx0 - (px - im.width / 2))), int(round(by0 - (py - im.height / 2)))
                        return im.crop((ix, iy, ix + G, iy + G))

                    holes = np.zeros((T, T), dtype=bool)    # see-through pixels left inside drawn squares
                    for (qx, qy), ids in squares:
                        at = (qx * G - tx * T, qy * G - ty * T)
                        for i in reversed(ids):       # nearest last = on top; the others fill its holes
                            canvas.alpha_composite(piece(i, qx, qy), at)
                        if L != 0:
                            continue                  # (a city level's see-through black is meant: not a hole)
                        # The middle of every shot is see-through (your arrow is cut out). Where the shots chosen
                        # for this square all have their hole on it - a road walked once - it stayed black. Any
                        # other shot that reaches the square goes UNDER what is drawn, nearest first.
                        cur = canvas.crop((at[0], at[1], at[0] + G, at[1] + G))
                        if np.asarray(cur)[..., 3].min() > 0:
                            continue
                        cx, cy = (qx + 0.5) * G, (qy + 0.5) * G
                        more = []
                        for ux in range(int(cx // 250) - 2, int(cx // 250) + 3):
                            for uy in range(int(cy // 250) - 2, int(cy // 250) + 3):
                                for i in near.get((ux, uy), ()):
                                    dd = math.hypot(shots[i][0] - cx, shots[i][1] - cy)
                                    if i not in ids and dd < radius[i] + G * 0.72:
                                        more.append((dd, i))
                        for dd, i in sorted(more)[:10]:
                            under = piece(i, qx, qy)
                            under.alpha_composite(cur)
                            cur = under
                            if np.asarray(cur)[..., 3].min() > 0:
                                break
                        canvas.paste(cur, at)
                        left = np.asarray(cur)[..., 3] == 0
                        if left.any():
                            holes[at[1]:at[1] + G, at[0]:at[0] + G] = left
                    if holes.any():                   # what no shot covers at all: grown in from its edges
                        canvas = Image.fromarray(_grow_in(np.asarray(canvas).copy(), holes))
                    canvas.save(os.path.join(d, name + ".tmp"), "PNG")
                    os.replace(os.path.join(d, name + ".tmp"), os.path.join(d, name))
                    drawn += 1
                new[str(L)] = lt
            for L, lt in old.items():                 # tiles that no longer have anything on them
                for key in lt:
                    if key not in (new.get(L) or {}):
                        tx, ty = key.split(",")
                        try:
                            os.remove(os.path.join(d, f"L{L}_{tx}_{ty}.png"))
                        except OSError:
                            pass
            m["tiles"] = new
            us = [ref * c["x"] for c, R in items]
            vs = [ref * c["y"] for c, R in items]
            m["bbox"] = [int(min(us)) - 200, int(min(vs)) - 200, int(max(us)) + 200, int(max(vs)) + 200]
            m["drawn"] = {L: True for L in new}
            self.save(place)
            nl = len(new)
            self.status = (f"{place}: {len(items)} shots, {sum(len(v) for v in new.values())} tiles"
                           f" ({drawn} redrawn), {ref:.2f} px/yd" + (f", {nl} levels" if nl > 1 else ""))
            return new

    # ------------------------------------------------------------------ for the pages
    def info(self, place, frame=None):
        """The continent map as seen from a zone's coords (frame): image px = k * zone% - origin, exactly
        like a zone map - the pages draw the tiles in that space. place: a zone (its continent, in its frame)
        or a continent (frame = the given zone, else any mapped zone of it)."""
        cont = continent(place) or place
        if cont == place and not frame:
            frame = next((z for z in zone_bounds() if continent(z) == cont), None)
        frame = frame or place
        m = self.meta(cont)
        b = zone_bounds().get(frame)
        ref = m.get("draw_k")
        if not ref or not m.get("tiles") or not b:
            return None
        N, S, W, E = b
        d = os.path.join(self._dir(cont), "tiles")

        def tiles(L):
            out = []
            for key in (m["tiles"].get(str(L)) or {}):
                tx, ty = (int(v) for v in key.split(","))
                p = os.path.join(d, f"L{L}_{tx}_{ty}.png")
                if os.path.exists(p):
                    out.append([tx * T, ty * T, f"/world/{safe(cont)}/L{L}_{tx}_{ty}.png?v={int(os.path.getmtime(p))}"])
            return out
        layers = sorted(int(L) for L in m["tiles"])
        cur = self.cur_layer.get(cont, layers[0] if layers else 0)
        if cur not in layers:
            cur = layers[0]
        kx, ky = ref * (W - E) / 100, ref * (N - S) / 100
        bb = m.get("bbox") or [0, 0, 0, 0]
        return {"world": True, "continent": cont, "frame": frame, "url": None, "unit": "coords", "pinned": True,
                "k": kx, "kx": kx, "ky": ky, "origin": [ref * W, ref * N], "tile": T,
                "tiles": tiles(cur), "floors": [{"id": L, "tiles": tiles(L)} for L in layers], "floor": cur,
                "bbox": bb, "size": [bb[2] - bb[0], bb[3] - bb[1]],
                "snaps": len(self.drawable(m)), "px_per_yd": ref}

    def floor_for_view(self, gid, G, view, cur=None, frame=None):
        """Which level the minimap is showing: its outdoor view is the outdoor map (level 0); its indoor view
        (cities, caves, buildings) is one of the indoor levels - the one the shots around you say, else the
        level the last indoor stretch went to, else the first. None: that view has nothing drawn yet."""
        m = self.meta(gid)
        drawn = sorted(int(L) for L in (m.get("tiles") or {}))
        if view == "out":
            return 0 if 0 in drawn else None
        if view != "in":
            return None
        inside = [L for L in drawn if L >= 1]
        if not inside:
            return None
        L = self.floor_at(gid, G, cur if cur in inside else None, frame, only=inside) if G is not None else None
        if L is None:
            L = self.cur_layer.get(gid)
        return L if L in inside else (cur if cur in inside else inside[0])

    def floor_at(self, gid, G, cur=None, frame=None, only=None):
        """Which level you're on, by where you stand: every drawn shot is an activation spot for its level
        (city shots, taken in WoW's zoomed-in indoor view, for the city level; the rest for the outdoor map).
        The nearest one within FLOOR_NEAR yd decides; nothing that close -> None (keep the last)."""
        m = self.meta(gid)
        key = (len(m["clips"]), len(m["runs"]))
        sp = getattr(self, "_spots", {}).get(gid)
        if not sp or sp[0] != key:
            pts = [(c["x"], c["y"], R["layer"], c.get("frame")) for c, R in self.drawable(m) if R.get("layer") is not None]
            self._spots = getattr(self, "_spots", {})
            self._spots[gid] = sp = (key, pts)
        # every spot within FLOOR_NEAR votes for its level, nearer ones more; the level shown only changes when
        # another clearly wins (1.5x) - one odd shot, or standing between two levels, can't make it flicker
        # only shots taken in the zone you're in vote: Ironforge lies UNDER Dun Morogh's mountains, so on the flat
        # map the outdoor shots overhead are as near as the city's own
        pts = allpts = sp[1]
        if only is not None:                     # (only these levels may answer: the minimap's view is known)
            pts = allpts = [p for p in pts if p[2] in only]
        if frame and any(f == frame for x, y, L, f in pts):
            pts = [p for p in pts if p[3] == frame]
        votes = {}
        for x, y, L, f in pts:
            d2 = (x - G[0]) ** 2 + (y - G[1]) ** 2
            if d2 <= FLOOR_NEAR ** 2:
                votes[L] = votes.get(L, 0) + 1 / (math.sqrt(d2) + 5)
        if not votes:
            # nothing close: a zone that is (nearly) all one level - Ironforge is all city - shows that level
            if frame and pts and pts is not allpts:
                n = {}
                for p in pts:
                    n[p[2]] = n.get(p[2], 0) + 1
                top = max(n, key=n.get)
                if n[top] >= 0.9 * len(pts):
                    return top
            return None
        best = max(votes, key=votes.get)
        if cur is not None and cur in votes and votes[best] < 1.5 * votes[cur]:
            return cur
        return best

    def info_grid(self, gid, G=None, frame=None, view=None):
        """A Mapping grid's tiles (grid units; the caller adds the zone frame's k/origin). The level shown is
        the one the minimap is showing (view: 'in' / 'out', measured while Mapping - see floor_for_view); when
        that isn't known it follows where you stand (G, see floor_at)."""
        m = self.meta(gid)
        ref = m.get("draw_k")
        if not ref or not m.get("tiles"):
            return None
        d = os.path.join(self._dir(gid), "tiles")

        def tiles(L):
            out = []
            for key in (m["tiles"].get(str(L)) or {}):
                tx, ty = (int(v) for v in key.split(","))
                p = os.path.join(d, f"L{L}_{tx}_{ty}.png")
                if os.path.exists(p):
                    out.append([tx * T, ty * T, f"/grid/{safe(gid)}/L{L}_{tx}_{ty}.png?v={int(os.path.getmtime(p))}"])
            return out
        layers = sorted(int(L) for L in m["tiles"])
        here = getattr(self, "_floor_here", {})
        self._floor_here = here
        L = self.floor_for_view(gid, G, view, here.get(gid), frame) if view else None
        if L is None and G is not None:
            L = self.floor_at(gid, G, here.get(gid), frame)
        if L is not None:
            here[gid] = L
        cur = here.get(gid, 0)
        if cur not in layers:
            cur = layers[0] if layers else 0
        bb = m.get("bbox") or [0, 0, 0, 0]
        return {"world": True, "continent": m.get("place"), "url": None, "pinned": True, "unit": "coords",
                "k": ref, "kx": ref, "ky": ref, "origin": [0, 0], "tile": T, "px_per_unit": ref,
                "tiles": tiles(cur), "floors": [{"id": L, "tiles": tiles(L)} for L in layers], "floor": cur,
                "bbox": bb, "size": [bb[2] - bb[0], bb[3] - bb[1]], "snaps": len(self.drawable(m)), "px_per_yd": ref}

    def places_list(self):
        out = super().places_list()
        for p in out:
            p["world"] = True
        return out


def rebuild(raw_root=None, root=WROOT, zones=None):
    """Rebuild the continent maps from every zone's raw clips, in time order."""
    import glob
    import json
    import shutil
    import numpy as np
    from minimapper import RAW_ROOT, disc_mask
    raw_root = raw_root or RAW_ROOT
    if os.path.isdir(root):
        shutil.rmtree(root)
    w = WorldMap(root)
    recs = []
    for zd in glob.glob(os.path.join(raw_root, "*")):
        zone = os.path.basename(zd).replace("_", " ")
        if zone.startswith("_") or not continent(zone) or (zones and zone not in zones):
            continue
        for sess in glob.glob(os.path.join(zd, "*")):
            try:
                for ln in open(os.path.join(sess, "clips.jsonl"), encoding="utf-8"):
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue                         # a line cut short by a kill mid-write
                    if r.get("unit") == "coords":
                        r["path"], r["zone"] = os.path.join(sess, r["file"]), zone
                        recs.append(r)
            except OSError:
                continue
    recs.sort(key=lambda r: r["t"])
    res = {}
    prev_zone = None
    for r in recs:
        try:
            im = Image.open(r["path"]).convert("RGBA")
        except OSError:
            continue
        n = im.width // 2
        a = np.minimum(np.asarray(im)[..., 3], np.asarray(disc_mask(n)))
        im.putalpha(Image.fromarray(a))
        if prev_zone and prev_zone != r["zone"]:
            w.break_run(r["zone"])                    # (a zone change in the raw record: don't chain across)
        prev_zone = r["zone"]
        what = w.add_zone(r["zone"], im, r["x"], r["y"], r["t"], r.get("label"), mapping=bool(r.get("mapping")))
        res[what] = res.get(what, 0) + 1
    for cont in {continent(r["zone"]) for r in recs}:
        if cont:
            w.compose(cont)
    return w, res
