"""Mapping grids (the user's design, 2026-10-09).

  - The first Mapping click sets a grid's 0,0 where you stand. Everything is built off that by following
    your path: every reading has to continue it.
  - A zone's coords are a "frame" on the grid: grid = offset + scale * zone%. Zones whose rectangle the
    combat log has given (MAP_CHANGE) are placed exactly (the grid is then the game's world yards, shifted
    so your first click is 0,0). Any other zone is LEARNED at the crossing: the minimap pictures just
    before and after the coords jump overlap, so the picture shift says exactly where the new coords sit;
    the scale comes from how far the minimap slides per 1% of the new coords. Until it's measured, that
    zone's shots wait and are placed afterwards.
  - Each grid spot remembers what the game showed there: the label above the minimap and the coords.
    Turning Mapping on looks those up; found -> carry on, not found -> "go to a known spot" or "start a
    new map here" (another continent is started automatically when the label names one with no map).
  - A reading that continues nothing (hearth, portal, relog) = a warp: Mapping stops and says so.
Files: maps/grids/<id>/grid.json (+ the WorldMap files: meta.json, clips/, tiles/).
"""
import json
import math
import os
import time

import numpy as np

from minimapper import HERE, safe, phase_shift, phase_shift_sub
import quadmap
from quadmap import gray_core
from worldmap import WorldMap, continent

GROOT = os.path.join(HERE, "maps", "grids")
K_YD_IN = 1.24               # the same, WoW's indoor view (Ironforge, Stormwind's streets, caves)
K_YD = 0.81                  # minimap px per yard, fully zoomed out (layout of 2026-10-09 evening)
CELL = 0.5                   # % size of a remembered spot
AREA = 12.0                  # yd: the squares named areas are built from


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _swap_in(tmp, dst):
    """os.replace that waits out a page reading the file this instant (Windows refuses the swap: 'Access is
    denied' - eight of those in minimap_errors.log, each one a save that didn't happen)."""
    for i in range(8):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if i == 7:
                raise
            time.sleep(0.05)


class Grids:
    def __init__(self, root=GROOT, resolve_label=None):
        self.root = root
        self.world = WorldMap(root)              # one tiled map per grid id
        self.grids = {}
        self.cur = None                          # the path chain: {gid, fid, x, y, G, t, g}
        self.status = ""
        self.lost = None                         # why Mapping stopped
        self.session = None                      # WoW's start time: the trail is one per play session
        self.need = None                         # "choose": not found - known spots / new map
        self.resolve_label = resolve_label or (lambda lab: None)
        os.makedirs(root, exist_ok=True)
        for d in os.listdir(root):
            p = os.path.join(root, d, "grid.json")
            if os.path.isfile(p):
                try:
                    gr = json.load(open(p, encoding="utf-8"))
                    self.grids[gr["id"]] = gr
                except (OSError, ValueError, KeyError):
                    pass
        try:                                     # the path survives a companion restart (if it was recent)
            c = json.load(open(os.path.join(root, "path.json"), encoding="utf-8"))
            if c.get("gid") in self.grids and time.time() - c["t"] < 90:
                c["g"] = None
                self.cur = c
            elif c.get("gid") in self.grids and time.time() - c["t"] < 300:     # older: a spot to pick the path up from
                self.last_good = (c["gid"], c["fid"], c["x"], c["y"], c["G"], c["t"])
        except (OSError, ValueError, KeyError):
            pass

    def save(self, gid):
        d = self.world._dir(gid)
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "grid.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.grids[gid], f)
        _swap_in(tmp, os.path.join(d, "grid.json"))

    # ------------------------------------------------------------------ frames
    def T(self, gr, fid, px, py):
        if fid == "exact":                           # (the position add-on, zone not known by name: grid units)
            return (px, py)
        f = gr["frames"].get(fid)
        if not f or f.get("pending"):
            return None
        return (f["ox"] + f["sx"] * px, f["oy"] + f["sy"] * py)

    def _bounds_frame(self, gr, zone):
        """A frame from the zone's rectangle (exact), on an aligned grid."""
        b = quadmap.zone_bounds().get(zone)
        # (never a zone of another continent: a page asking for a Kalimdor zone put a "Dustwallow Marsh" frame
        #  on the Eastern Kingdoms grid, on top of Loch Modan and Dun Morogh)
        if not b or not gr.get("aligned") or (gr.get("continent") and continent(zone) and continent(zone) != gr.get("continent")):
            return None
        N, S, W, E = b
        ex0, sy0 = gr["w0"]
        return {"zone": zone, "bounds": True, "ox": -W - ex0, "oy": -N - sy0, "sx": (W - E) / 100, "sy": (N - S) / 100}

    def frame_for_zone(self, gr, zone):
        for fid, f in gr["frames"].items():
            if f.get("zone") == zone and not f.get("pending"):
                return fid
        f = self._bounds_frame(gr, zone) if zone else None
        if f:
            gr["frames"][zone] = f
            return zone
        return None

    # ------------------------------------------------------------------ grids
    def new_grid(self, label, px, py, now, zone_hint=None):
        zone = self.resolve_label(label) or zone_hint     # (a garbled label: the zone the companion knows)
        cont = continent(zone) if zone else None
        if cont and any(g.get("continent") == cont for g in self.grids.values()):
            # (never a second map of one continent: "start a new map here" in Darkshore, while the log still
            #  said Stormwind City, made "EasternKingdoms3" - and two maps of one continent make every spot
            #  on it two answers, so the real map could not find you any more)
            self.need = "choose"
            self.status = (f"Mapping: {cont} already has a map and the game still says {zone} - "
                           "walk a few steps and switch Mapping on again")
            return None
        n = len(self.grids) + 1
        gid = safe(cont or f"Map {n}").replace("_", "")
        while gid in self.grids:
            n += 1
            gid = safe(f"{cont or 'Map'} {n}").replace("_", "")
        b = quadmap.zone_bounds().get(zone) if zone else None
        gr = {"id": gid, "name": cont or f"Map {n}", "continent": cont, "created": now, "frames": {}, "cells": {}}
        if b:                                    # the game's world yards, shifted so this spot is 0,0
            N, S, W, E = b
            gr["aligned"] = True
            gr["w0"] = [-W + px / 100 * (W - E), -N + py / 100 * (N - S)]
            fid = zone
            gr["frames"][fid] = self._bounds_frame(gr, zone)
        else:                                    # nothing known: 0,0 here, the scale is learned by walking
            gr["aligned"] = False
            fid = "F1"
            gr["frames"][fid] = {"zone": zone, "bounds": False, "pending": True, "anchor": [0.0, 0.0, px, py],
                                 "samples": [], "label": label}
        gr["origin"] = {"frame": fid, "x": px, "y": py, "t": now, "label": label}
        self.grids[gid] = gr
        self.save(gid)
        self.cur = {"gid": gid, "fid": fid, "x": px, "y": py, "G": [0.0, 0.0], "t": now, "g": None}
        self.lost = self.need = None
        self.status = f"New map '{gr['name']}' started here (0,0)"
        return gid

    def import_world(self, gid, src_root=None):
        """Today's continent map (world yards) onto an aligned grid: shift by the grid's 0,0."""
        import shutil
        gr = self.grids[gid]
        if not gr.get("aligned") or not gr.get("continent"):
            return 0
        src_root = src_root or os.path.join(HERE, "maps", "world")
        sd = os.path.join(src_root, safe(gr["continent"]))
        try:
            m = json.load(open(os.path.join(sd, "meta.json"), encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        ex0, sy0 = gr["w0"]
        dd = os.path.join(self.root, gid, "clips")
        os.makedirs(dd, exist_ok=True)
        for c in m["clips"]:
            c["x"], c["y"] = round(c["x"] - ex0, 2), round(c["y"] - sy0, 2)
            c.pop("vote", None)
            if "file" in c and os.path.exists(os.path.join(sd, "clips", c["file"])):
                shutil.copy(os.path.join(sd, "clips", c["file"]), os.path.join(dd, c["file"]))
        for R in m["runs"].values():
            R["p"] = [[a, b, round(x - ex0, 2), round(y - sy0, 2)] for a, b, x, y in R.get("p") or []]
        m["place"], m["tiles"], m["drawn"] = gid, {}, {}
        self.world.places[gid] = m
        self.world.save(gid)
        self.world.dirty[gid] = time.time()
        return len(m["clips"])

    # ------------------------------------------------------------------ remembering spots
    def _remember(self, gr, fid, px, py, label):
        if not label:
            return
        key = f"{fid}|{round(px / CELL)}|{round(py / CELL)}"
        c = gr["cells"].setdefault(key, {"x": round(px, 1), "y": round(py, 1), "labels": {}})
        lab = label.strip(" .").lower()
        c["labels"][lab] = c["labels"].get(lab, 0) + 1

    def _area(self, gr, G, label):
        """Named areas: the square you're on counts one more for the name above the minimap."""
        import re
        lab = (label or "").strip(" .")
        # only clean names: a garbled reading ("The ?rge") must not become an area
        if len(lab) < 4 or not re.fullmatch(r"[A-Za-z][A-Za-z']*(?: [A-Za-z][A-Za-z']*)*", lab):
            return
        lab = " ".join(w if w.lower() in ("of", "the", "and") and i else w[:1].upper() + w[1:] for i, w in enumerate(lab.split()))
        a = gr.setdefault("areas", {})
        e = a.setdefault(lab.lower(), {"name": lab, "cells": {}, "n": 0})
        key = f"{int(G[0] // AREA)},{int(G[1] // AREA)}"
        e["cells"][key] = e["cells"].get(key, 0) + 1
        e["n"] = e.get("n", 0) + 1
        if e["n"] == 10:                       # seen plenty: teach the label reader this name (it snaps to it)
            try:
                p = os.path.join(HERE, "subzones.json")
                sz = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
                if lab.lower() not in sz:
                    sz[lab.lower()] = self.resolve_label(lab) or ""
                    with open(p + ".tmp", "w", encoding="utf-8") as f_:
                        json.dump(sz, f_, indent=1, sort_keys=True)
                    _swap_in(p + ".tmp", p)
            except (OSError, ValueError):
                pass

    def areas(self, gid):
        """Each named area as a bubble: the squares where that name was shown most, grown one square out
        (not into squares that belong to another name), its border edges and its centre (grid units)."""
        gr = self.grids.get(gid) or {}
        a = gr.get("areas") or {}
        owner = {}
        for lab, e in a.items():
            for key, n in e["cells"].items():
                if key not in owner or n > owner[key][1]:
                    owner[key] = (lab, n)
        out = []
        for lab, e in a.items():
            own = {tuple(map(int, k.split(","))) for k, (l, n) in owner.items() if l == lab}
            if len(own) < 2 or e.get("n", 0) < 3:
                continue                          # a stray reading, not an area
            grown = set(own)
            for (i, j) in own:                    # a bubble: two squares out from where the name was seen
                for di in (-2, -1, 0, 1, 2):
                    for dj in (-2, -1, 0, 1, 2):
                        if abs(di) + abs(dj) > 3:
                            continue
                        q = (i + di, j + dj)
                        if f"{q[0]},{q[1]}" not in owner:
                            grown.add(q)
            edges = []
            for (i, j) in grown:                  # a border wherever the bubble meets anything else
                if (i, j - 1) not in grown:
                    edges.append([i, j, i + 1, j])
                if (i, j + 1) not in grown:
                    edges.append([i, j + 1, i + 1, j + 1])
                if (i - 1, j) not in grown:
                    edges.append([i, j, i, j + 1])
                if (i + 1, j) not in grown:
                    edges.append([i + 1, j, i + 1, j + 1])
            cx = sum(i + 0.5 for i, j in own) / len(own)
            cy = sum(j + 0.5 for i, j in own) / len(own)
            # the label goes on the area's own square nearest its middle (works for curved areas too)
            near = sorted(own, key=lambda q: (q[0] + 0.5 - cx) ** 2 + (q[1] + 0.5 - cy) ** 2)
            bi, bj = near[0]
            # other places for the name, nearest the middle first (used when names would overlap)
            spots = [[(i + 0.5) * AREA, (j + 0.5) * AREA] for i, j in near[:40:4]]
            # (the border "edges" are not sent: no page draws them, and they were 700 KB of every 930 KB poll)
            out.append({"name": e["name"], "cell": AREA,
                        "center": [(bi + 0.5) * AREA, (bj + 0.5) * AREA], "spots": spots, "squares": len(own)})
        return out

    def clear_trail(self):
        """The 'Clear path' button: every map's trail starts again from here."""
        for gid, gr in self.grids.items():
            if gr.get("trail"):
                gr["trail"] = []
                self.save(gid)

    def rename_area(self, old, new):
        """The user corrected a name: the area (and remembered spots) take the new name; merged if it exists."""
        o, n = old.strip().lower(), new.strip()
        for gid, gr in self.grids.items():
            a = gr.get("areas") or {}
            if o in a:
                e = a.pop(o)
                t = a.setdefault(n.lower(), {"name": n, "cells": {}, "n": 0})
                t["name"] = n
                t["n"] = t.get("n", 0) + e.get("n", 0)
                for c, k in e["cells"].items():
                    t["cells"][c] = t["cells"].get(c, 0) + k
            for c in gr["cells"].values():
                if o in c["labels"]:
                    c["labels"][n.lower()] = c["labels"].get(n.lower(), 0) + c["labels"].pop(o)
            self.save(gid)

    def locate(self, label, px, py, zone_hint=None):
        """Mapping switched on: where is this? The label + coords against every grid's remembered spots,
        and (aligned grids) the zone the label names. -> (gid, fid) if exactly one place fits."""
        from difflib import SequenceMatcher
        lab = (label or "").strip(" .").lower()
        hits = set()
        exact = set()
        for gid, gr in self.grids.items():
            for key, c in gr["cells"].items():
                fid = key.split("|")[0]
                if abs(c["x"] - px) <= 1.0 and abs(c["y"] - py) <= 1.0 and lab and any(
                        SequenceMatcher(None, lab, l).ratio() >= 0.85 for l in c["labels"]):
                    if fid in gr["frames"] and not (gr["frames"].get(fid) or {}).get("pending"):
                        hits.add((gid, fid))
            zone = self.resolve_label(label) or zone_hint
            if zone and gr.get("aligned") and continent(zone) == gr.get("continent") and quadmap.zone_bounds().get(zone):
                exact.add((gid, self.frame_for_zone(gr, zone)))
        # (the same label + coords remembered in a learned copy of the zone as well made two answers = "I don't
        #  know this spot", all over Westfall: the zone's own rectangle is never outvoted by a remembered spot)
        exact = {h for h in exact if h[1]}
        if len(exact) == 1:
            return next(iter(exact))
        hits = {h for h in hits | exact if h[1]}
        if len(hits) == 1:
            return next(iter(hits))
        return None

    def known_spots(self, n=6):
        """The most-seen labels of each grid (for "go to a known spot")."""
        out = []
        for gid, gr in self.grids.items():
            cnt = {}
            for c in gr["cells"].values():
                for l, k in c["labels"].items():
                    cnt[l] = cnt.get(l, 0) + k
            for l, k in sorted(cnt.items(), key=lambda kv: -kv[1])[:n]:
                out.append({"map": gr["name"], "label": l.title()})
        return out

    # ------------------------------------------------------------------ Mapping on
    def _any_grid_on_disk(self):
        return any(os.path.isfile(os.path.join(self.root, d, "grid.json")) for d in os.listdir(self.root))

    def start(self, label, px, py, now, new=False, zone_hint=None):
        """First reading after Mapping was switched on."""
        if not self.grids and self._any_grid_on_disk():
            self.__init__(self.root, self.resolve_label)     # a map file couldn't be read a moment ago: read again
            if not self.grids:
                self.status = "Mapping: couldn't read the saved map - trying again"
                return None
        if new or not self.grids:
            return self.new_grid(label, px, py, now, zone_hint)
        if self.recover(px, py, now):            # recent path: carry on from it (the label lies at borders)
            self.need = None
            return self.cur["gid"]
        hit = self.locate(label, px, py)        # (never the log's zone: it lags - "found" you in Redridge in Westfall)
        lg = getattr(self, "last_good", None)
        if hit and lg and lg[0] == hit[0] and now - lg[5] < 300:
            Gh = self.T(self.grids[hit[0]], hit[1], px, py)
            if Gh is None or _dist(Gh, lg[4]) > 40 + 40 * (now - lg[5]):
                hit = None                       # the label says one thing, your recent path another: don't
        if hit:
            gid, fid = hit
            G = self.T(self.grids[gid], fid, px, py)
            self.cur = {"gid": gid, "fid": fid, "x": px, "y": py, "G": list(G), "t": now, "g": None}
            self.lost = self.need = None
            self.status = f"Mapping: found you on '{self.grids[gid]['name']}'"
            return gid
        zone = self.resolve_label(label)
        cont = continent(zone) if zone else None
        # another continent: its own map - but only when the log agrees you're in that zone (a misread label like
        # "Darkshire" -> "Darkshore" once started a Kalimdor map in Duskwood)
        if cont and zone_hint == zone and not any(g.get("continent") == cont for g in self.grids.values())                 and not self._any_grid_on_disk_for(cont):
            return self.new_grid(label, px, py, now, zone_hint)   # another continent: its own map
        self.need = "choose"
        self.status = "Mapping: I don't know this spot - go to a known spot, or start a new map here"
        return None

    def _any_grid_on_disk_for(self, cont):
        for d in os.listdir(self.root):
            try:
                if json.load(open(os.path.join(self.root, d, "grid.json"), encoding="utf-8")).get("continent") == cont:
                    return True
            except (OSError, ValueError):
                continue
        return False

    def k_now(self, view="out"):
        """Minimap px per yard, fully zoomed out: the outdoor view (px_per_yd) or WoW's indoor view used in
        Ironforge, Stormwind's streets, caves (px_per_yd_in) - from screen_layout "minimap"."""
        try:
            from screenread import region
            r = region("minimap")
            return float((r.get("px_per_yd_in") if view == "in" else r.get("px_per_yd")) or (K_YD_IN if view == "in" else K_YD))
        except Exception:
            return K_YD_IN if view == "in" else K_YD

    def k_views(self, view):
        """Every zoom (px per yard) a view can have, the layout's own first. The indoor view has two: the
        layout's px_per_yd_in (the main character's, one zoom step in: 120 yd to the rim, 1.395) and fully
        zoomed out (150 yd to the rim = the frame's radius / 150: 1.12; a new character's). WoW keeps the
        indoor zoom apart from the outdoor one, per character."""
        ks = [self.k_now(view)]
        if view == "in":
            try:
                from screenread import region
                fr = region("minimap").get("frame") or []
                k0 = float(fr[2]) / 150.0 if len(fr) > 2 else None
                if k0 and abs(math.log(k0 / ks[0])) > 0.05:
                    ks.append(k0)
            except Exception:
                pass
        return ks

    def view_k(self, view, kp):
        """The zoom of this view nearest to a measured one."""
        return min(self.k_views(view), key=lambda k0: abs(math.log(kp / k0))) if kp and kp > 0 else self.k_now(view)

    def _classify(self, kp):
        """Which minimap view a measured zoom (px per yard) belongs to: 'out', 'in' or 'odd' (neither)."""
        for v in ("out", "in"):
            if any(abs(kp - k0) <= 0.18 * k0 for k0 in self.k_views(v)):
                return v
        return "odd"

    @staticmethod
    def default_view(zone):
        return "in" if zone == "Ironforge" else "out"   # (Ironforge is all indoor; elsewhere it's measured)

    def stop(self):
        self.cur = None

    # ------------------------------------------------------------------ following the path
    def step(self, img, px, py, label, now, shot=None):
        """One reading in Mapping mode. shot = anything the caller wants drawn for this reading (its picture),
        or None. -> ("place", gid, [(shot, G, view, k), ...]) for the shots whose minimap view is confirmed
        now (they come out 20+ yd later - see _views), ("wait", reason), ("skip", reason) or ("lost", reason)."""
        c = self.cur
        gr = self.grids[c["gid"]]
        g = gray_core(img)
        dt = max(0.3, now - c["t"])
        if dt > 120:
            return self._lose(f"no readings for {dt:.0f} s")
        reach = 40 + 40 * dt                       # yards: walking, mounted, flying (gryphons ~35 yd/s)
        shift = None
        if c.get("g") is not None and c["g"].shape == g.shape:
            dx, dy, pk = phase_shift(c["g"], g)
            if pk >= 10 and math.hypot(dx, dy) < 0.4 * g.shape[0] and not (dx == 0 and dy == 0 and dt > 3
                                                                           and _dist(self.T(gr, c["fid"], px, py) or [1e9, 1e9], c["G"]) > 2):
                shift = (int(dx), int(dy))          # (standing still: no slide, same coords - still on track)
        view = c.get("view") or self.default_view((gr["frames"].get(c["fid"]) or {}).get("zone"))
        k = self.k_now("in" if view == "in" else "out")  # px per yard: FIXED per view (minimap fully zoomed out)
        G_est = [c["G"][0] - shift[0] / k, c["G"][1] - shift[1] / k] if shift else list(c["G"])
        for f_id in [k for k, f in gr["frames"].items() if k.startswith("F") and f.get("zone")
                     and gr.get("aligned") and quadmap.zone_bounds().get(f["zone"])]:
            real = self.frame_for_zone(gr, gr["frames"][f_id]["zone"])
            if real and real != f_id:
                gr["frames"].pop(f_id)                 # the game gave its rectangle: the learned copy goes
                if c["fid"] == f_id:
                    c["fid"] = real
        fr = gr["frames"].get(c["fid"])
        fid = None
        if fr and c["fid"].startswith("F") and gr.get("aligned"):
            # a learned zone whose real rectangle has turned up in the log since: switch to the exact one
            zone_l0 = self.resolve_label(label)
            if zone_l0 and quadmap.zone_bounds().get(zone_l0):
                zf = self.frame_for_zone(gr, zone_l0)
                Gb = self.T(gr, zf, px, py) if zf else None
                if Gb and _dist(Gb, G_est) <= 80:
                    c["fid"], fr = zf, gr["frames"][zf]
        if fr and not fr.get("pending"):
            Gs = self.T(gr, c["fid"], px, py)
            # (after a gap, being "within reach" of the same zone proves nothing - mid-flight, 30 s of reach
            # swallowed a whole zone change: then every zone competes below, same rules)
            if _dist(Gs, c["G"]) <= reach and dt <= 1.5:
                fid, G = c["fid"], Gs              # carried on in the same zone
                # the minimap's zoom right now (px per yard): its slide vs how far you really went. You can
                # resize / zoom the minimap any time; this follows it.
                # (fitted over the last ~40 readings, not step by step: one step's coords are off by a few yards)
                acc = c.setdefault("acc", [])
                if shift:
                    last = acc[-1] if acc else [0.0, 0.0, c["G"][0], c["G"][1]]
                    acc.append([last[0] - shift[0], last[1] - shift[1], Gs[0], Gs[1]])
                    del acc[:-40]
                    if len(acc) >= 5:
                        a0 = acc[0]
                        num = sum((p[0] - a0[0]) * (p[2] - a0[2]) + (p[1] - a0[1]) * (p[3] - a0[3]) for p in acc[1:])
                        den = sum((p[2] - a0[2]) ** 2 + (p[3] - a0[3]) ** 2 for p in acc[1:])
                        far = max(math.hypot(p[2] - a0[2], p[3] - a0[3]) for p in acc)
                        if far > 60 and den > 0 and num > 0:
                            c["K_seen"] = round(num / den, 3)   # (shown for checking only: the zoom is fixed)
                else:
                    c["acc"] = []                  # the picture lost track: start the fit again
        elif fr and abs(px - c["x"]) + abs(py - c["y"]) <= 4 * dt + 1:
            fid, G = c["fid"], G_est               # same (still unmeasured) zone: follow by the picture
            if shift:
                fr["samples"].append([-shift[0], -shift[1], px - c["x"], py - c["y"]])
                del fr["samples"][:-400]           # (only the last 400 are used; one frame had grown to 825)
                self._try_scale(gr, c["fid"], k)
        if fid is None:
            self._jump_note = (c["fid"], c["x"], c["y"], px, py, label, round(dt, 1), shift)
            # the coords jumped: a zone line. Which known zone puts these coords where you are?
            cand = []
            zone_l = self.resolve_label(label)
            extra = []                             # zones not used on this map yet: every zone of its continent
            if gr.get("aligned"):                  # whose rectangle is known (the path decides which one fits)
                extra = [z for z in quadmap.zone_bounds() if continent(z) == gr.get("continent") and z not in gr["frames"]]
            elif zone_l and zone_l not in gr["frames"]:
                extra = [zone_l]
            for f_id, f in list(gr["frames"].items()) + [(z, None) for z in extra]:
                if f is None:
                    f_id = self.frame_for_zone(gr, f_id)
                    if not f_id:
                        continue
                Gc = self.T(gr, f_id, px, py)
                if Gc is not None:
                    cand.append((_dist(Gc, G_est), f_id, Gc))
            for f_id, f in gr["frames"].items():          # zones still being measured: last coords + map spot
                if f.get("pending") and f.get("last"):
                    lx, ly, lg = f["last"]
                    if abs(px - lx) + abs(py - ly) <= 2 and _dist(lg, G_est) <= 60:
                        cand.append((_dist(lg, G_est), f_id, G_est))
            cand.sort(key=lambda z: z[0])
            lim = min(reach, 60 + 30 * dt) if shift else reach
            if cand and cand[0][0] <= lim and (len(cand) == 1 or cand[1][0] >= max(3 * cand[0][0], cand[0][0] + 300)):
                _, fid, G = cand[0]
            elif shift and shift != (0, 0) and (not cand or cand[0][0] > 400) and not (
                    gr.get("aligned") and zone_l and quadmap.zone_bounds().get(zone_l)):
                # (not standing still: a stuck coords reading (52.0,49.2) with a picture that hadn't moved made
                #  "new zones" F11-F14 on top of Westfall. And not when the label names a zone whose rectangle
                #  is known: then the coords are a warp or a misread, never a new zone.)
                # (the label can't decide this: it lags - at Duskwood's south edge it still said Duskwood on
                # Stranglethorn's coords. Nothing known within 400 yd + the ground continues = a new zone.)
                # The ground continues (pictures overlap) but no known zone fits: a new zone. Its 0 is pinned here
                # by the picture; its scale is measured as you walk on. (A known zone 99 yd off - Ironforge's indoor
                # zoom throwing the picture estimate - is a pause + pick-up again, not a new zone.)
                n = 1 + sum(1 for f in gr["frames"] if f.startswith("F"))
                while f"F{n}" in gr["frames"]:       # (a frame was removed since: never reuse a live name)
                    n += 1
                fid = f"F{n}"
                gr["frames"][fid] = {"zone": zone_l, "bounds": False, "pending": True, "label": label,
                                     "anchor": [G_est[0], G_est[1], px, py], "samples": []}
                G = G_est
                self.status = f"Mapping: new zone ({label or '?'}) - measuring it as you walk"
            elif c.get("miss", 0) < 2:
                c["miss"] = c.get("miss", 0) + 1     # one odd reading (a misread): skip it, keep the path
                return ("skip", "a reading that doesn't continue the path")
            else:
                self.last_loss = {"t": now, "from": [c["fid"], c["x"], c["y"], [round(v) for v in c["G"]]],
                                  "to": [px, py, label], "dt": round(dt, 1), "shift": shift, "K": round(k, 3),
                                  "G_est": [round(v) for v in G_est], "best": [(round(d), f) for d, f, _ in cand[:3]]}
                self.off = {"x": px, "y": py, "t": now}     # where the coords jumped TO: a map this one doesn't know
                return self._lose("the coords jumped and nothing continues your path")
        return self._advance(c, gr, fid, G, g, shift, px, py, label, now, shot, dt, locals().get("cand"))

    def exact_G(self, cont, wa, wb):
        """World yards (north-south, west-east) from the position add-on -> (gid, G) on that continent's map,
        or (None, None) while it has no map yet."""
        for gid, gr in self.grids.items():
            if gr.get("aligned") and gr.get("continent") == cont:
                return gid, [-wb - gr["w0"][0], -wa - gr["w0"][1]]
        return None, None

    def step_exact(self, img, cont, wa, wb, zone, px, py, label, now, shot=None, track=None):
        """One reading with the position add-on: the spot is known exactly, so there is nothing to find, to
        follow or to lose - a boat, a hearth, a flight are just the next spot. A continent without a map
        gets one. zone / px, py = the zone map and your coords on it (for the pages and the remembered
        spots), or None / the grid spot when the zone isn't known by name yet. track = (picture, layout px
        per picture px): the minimap at its own size on a character whose minimap is smaller - the slide is
        then measured on that, to a fraction of a pixel. Returns as step() does."""
        gid, G = self.exact_G(cont, wa, wb)
        if gid is None:
            gid = safe(cont).replace("_", "")
            n = 1
            while gid in self.grids:
                n += 1
                gid = safe(f"{cont} {n}").replace("_", "")
            self.grids[gid] = {"id": gid, "name": cont, "continent": cont, "created": now, "frames": {}, "cells": {},
                               "aligned": True, "w0": [-wb, -wa],
                               "origin": {"frame": zone or "exact", "x": px, "y": py, "t": now, "label": label}}
            self.save(gid)
            G = [0.0, 0.0]
        gr = self.grids[gid]
        fid = (self.frame_for_zone(gr, zone) if zone else None) or "exact"
        if fid == "exact":
            px, py = round(G[0], 1), round(G[1], 1)
        g, gs = (track[0], float(track[1])) if track else (gray_core(img), 1.0)
        c = self.cur
        dt = max(0.3, now - c["t"]) if c else 0.3
        if not c or c.get("gid") != gid or not c.get("exact") or dt > 120 or _dist(G, c["G"]) > 40 + 40 * dt:
            if c and c.get("gid") in self.grids:               # a warp: a gap in the trail, not a line across the map
                tr = self.grids[c["gid"]].setdefault("trail", [])
                if tr and tr[-1] is not None:
                    tr.append(None)
            c = self.cur = {"gid": gid, "fid": fid, "x": px, "y": py, "G": list(G), "t": now, "g": None, "exact": True}
            dt = 0.3
        shift = None
        if c.get("g") is not None and c["g"].shape == g.shape:
            dx, dy, pk = (phase_shift_sub if track else phase_shift)(c["g"], g)
            if pk >= 10 and math.hypot(dx, dy) < 0.4 * g.shape[0] and not (dx == 0 and dy == 0 and _dist(G, c["G"]) > 3):
                shift = (dx * gs, dy * gs) if track else (int(dx), int(dy))
        self.lost = self.need = self.off = None
        return self._advance(c, gr, fid, G, g, shift, px, py, label, now, shot, dt, None, exact=True,
                             gs=gs if track else None)

    def _advance(self, c, gr, fid, G, g, shift, px, py, label, now, shot, dt, cand, exact=False, gs=None):
        """The reading is placed (fid, G): the minimap-view check, the trail, the remembered spots."""
        c["xm"] = exact
        if not exact:
            c.pop("exact", None)
        if fid != c["fid"] and not exact and os.path.normpath(self.root) == os.path.normpath(GROOT):   # (the real map only, not tests)
            try:
                with open(os.path.join(HERE, "zone_decisions.log"), "a", encoding="utf-8") as f:
                    f.write(time.strftime("%H:%M:%S ") + f"Mapping path: {c['fid']} {c['x']},{c['y']} -> {fid} {px},{py}"
                            f" (label {label!r}, {dt:.1f}s, picture shift {shift}, candidates "
                            f"{[(round(d), z) for d, z, _ in (cand or [])[:3]]})" + chr(10))
            except OSError:
                pass
        if (gr["frames"].get(fid) or {}).get("pending"):
            gr["frames"][fid]["last"] = [px, py, [round(G[0], 1), round(G[1], 1)]]
        # which minimap view you're in (outdoor / indoor - different zooms; Stormwind switches between them
        # street by street with NO break in the picture: canals outdoor, districts indoor): measured, never
        # assumed. The picture's slide vs real movement over a stretch, per shot - see _views.
        # (with the position add-on a zone line is just more ground: the stretch carries on across it)
        if (fid != c["fid"] and not exact) or not shift or (gr["frames"].get(fid) or {}).get("pending"):
            why = "zone" if fid != c["fid"] and not exact else ("pending" if (gr["frames"].get(fid) or {}).get("pending") else "picture")
            dr = c.setdefault("drops", {})
            dr["reset_" + why] = dr.get("reset_" + why, 0) + 1
            dr["dropped_at_reset"] = dr.get("dropped_at_reset", 0) + sum(1 for p in c.get("win") or [] if p[4] is not None)
            c["win"] = [[0.0, 0.0, G[0], G[1], shot]]
            c["kf"], c["kf_cum"] = g, [0.0, 0.0]          # the picture everything is measured against  # a zone switch (city gates) / the picture lost track (a
                                                       # door, a zoom change): a new stretch from here
        else:
            win = c.setdefault("win", [])
            last = win[-1] if win else [0.0, 0.0, c["G"][0], c["G"][1], None]
            cum = [last[0] - shift[0], last[1] - shift[1]]
            # The slide is measured against a KEYFRAME, not step by step: a step is only 2-3 px indoors and each is
            # rounded to whole pixels - biased toward 0 by anything static - so chained steps came up ~20-40 %
            # short (Ironforge read 0.6-1.0 px/yd instead of 1.24 and was called "outdoor").
            kf = c.get("kf")
            if kf is not None and kf.shape == g.shape:
                kx, ky, kpk = (phase_shift_sub if gs else phase_shift)(kf, g)
                far = math.hypot(kx, ky)               # (in the picture's own pixels; gs: layout px per picture px)
                kx, ky = kx * (gs or 1.0), ky * (gs or 1.0)
                kc = [c["kf_cum"][0] - kx, c["kf_cum"][1] - ky]
                give = self.KF_GIVE_OWN if gs else self.KF_GIVE
                if kpk >= 10 and far < 0.4 * g.shape[0] and math.hypot(kc[0] - cum[0], kc[1] - cum[1]) <= give[0] + give[1] * math.hypot(kx, ky):
                    cum = kc                           # (agrees with the step-by-step estimate: not a false match)
                    if far > 0.2 * g.shape[0]:
                        c["kf"], c["kf_cum"] = g, list(cum)   # moved on: a new keyframe here
                else:
                    c["kf"], c["kf_cum"] = g, list(cum)
            else:
                c["kf"], c["kf_cum"] = g, list(cum)
            win.append([cum[0], cum[1], G[0], G[1], shot])
            del win[:-300]
        frv = gr["frames"].get(fid) or {}
        c["q"] = 0.1 * max(abs(frv.get("sx") or 0), abs(frv.get("sy") or 0)) or 1.0   # yd per coords step here
        if exact:
            c["q"] = 0.1                                # (the add-on's position: to the inch)
        ready = self._views(c)
        c.update(fid=fid, x=px, y=py, G=list(G), t=now, g=g, miss=0)
        if gr.get("trail_session") != self.session:      # one trail per play session (WoW's start time)
            gr["trail"], gr["trail_session"] = [], self.session
        tr = gr.setdefault("trail", [])
        if not tr or tr[-1] is None or _dist(tr[-1], G) >= 3:
            tr.append([round(G[0], 1), round(G[1], 1)])
            del tr[:-6000]
        if now - gr.get("_saved", 0) > 10:        # remembered spots + named areas go to disk
            gr["_saved"] = now
            self.save(c["gid"])
        if now - c.get("saved", 0) > 3:
            c["saved"] = now
            try:
                json.dump({k: v for k, v in c.items() if k not in ("g", "win", "kf")}, open(os.path.join(self.root, "path.json"), "w"))
            except OSError:
                pass
        if fid != "exact":
            self._remember(gr, fid, px, py, label)
        self._area(gr, G, label)
        if (gr["frames"].get(fid) or {}).get("pending"):
            return ("wait", "measuring this zone")
        if ready:
            return ("place", c["gid"], ready)
        if c.get("view") == "odd":
            return ("wait", f"the minimap's zoom doesn't match (measured {c.get('kp')} px/yd) - zoom it all the way out")
        return ("wait", "checking which minimap view you're in")

    KF_GIVE = (3, 0.35)                  # px + share of the slide a keyframe may differ from the summed steps
    KF_GIVE_OWN = (6, 0.5)               # the same on a small minimap measured at its own size (see step_exact):
                                         # its steps are noisier (2026-11-20 recording: 20 pictures placed, 18 with 3 / 0.35)
    VIEW_BEFORE, VIEW_AFTER = 10, 20     # yd of track a shot's view is checked over, before and after it -
    VIEW_STEPS = 8, 12                   # at least this many of the zone's coords steps (0.1 %: 0.8 yd in
                                         # Ironforge, 3.5 in Westfall, 6.4 in Stranglethorn - shorter is noise)

    def _fit_view(self, pts, tol):
        """Picture slide vs movement over a stretch of track (least squares) -> (view or None, px per yd)."""
        a0 = pts[0]
        num = sum((p[0] - a0[0]) * (p[2] - a0[2]) + (p[1] - a0[1]) * (p[3] - a0[3]) for p in pts[1:])
        den = sum((p[2] - a0[2]) ** 2 + (p[3] - a0[3]) ** 2 for p in pts[1:])
        if den <= 0 or num <= 0:
            return None, None
        kp = num / den
        for v in ("out", "in"):
            if any(abs(math.log(kp / k0)) <= tol for k0 in self.k_views(v)):
                return v, kp
        return None, kp

    def _views(self, c):
        """Shots whose minimap view is now certain -> [(shot, G, view, k)]. A shot is drawn only when the
        stretch of track BEFORE it (>= 10 yd, same picture track) and the stretch AFTER it (>= 20 yd) both
        measure the same view. Where the view switches mid-street, one of the two stretches straddles the
        switch and disagrees, so the shots there are dropped instead of drawn at the wrong size - wrong
        needs two switches within ~30 yd. Shots in the first 10 yd after a door / zone line are dropped too
        (no stretch before them to check). Stretches are at least 8 / 12 of the zone's coords steps. Costs: shots come out 20-80 yd behind you."""
        win = c.get("win") or []
        out = []
        q = c.get("q") or 1.0
        before = max(self.VIEW_BEFORE, self.VIEW_STEPS[0] * q)
        after = max(self.VIEW_AFTER, self.VIEW_STEPS[1] * q)
        tol_a, tol_b = 0.16, 0.12
        if c.get("xm"):
            # the position add-on: the track is exact (not 3.5 yd coords steps), so the only error left is the
            # picture's slide (whole pixels). The two views are 0.58 apart (log scale); shots come 12 yd behind.
            before, after, tol_a, tol_b = 6.0, 12.0, 0.22, 0.22
        for i, p in enumerate(win):
            if p[4] is None:
                continue
            m = next((m for m in range(i + 1, len(win)) if _dist(win[m][2:4], p[2:4]) >= after), None)
            if m is None:
                break                                  # not far enough past it yet
            shot, p[4] = p[4], None                    # decided once, now
            j = next((j for j in range(i - 1, -1, -1) if _dist(win[j][2:4], p[2:4]) >= before), None)
            dr = c.setdefault("drops", {})
            if j is None:
                dr["no_before"] = dr.get("no_before", 0) + 1
                continue
            va, ka = self._fit_view(win[j:i + 1], tol_a)
            vb, kb = self._fit_view(win[i:m + 1], tol_b)
            if va and va == vb:
                out.append((shot, list(p[2:4]), va, self.view_k(va, math.sqrt(ka * kb))))
                dr["placed"] = dr.get("placed", 0) + 1
            else:
                key = f"disagree_{va}_{vb}"
                dr[key] = dr.get(key, 0) + 1
                dr.setdefault("last_k", [])
                dr["last_k"] = (dr["last_k"] + [[round(ka or 0, 2), round(kb or 0, 2)]])[-12:]
        # the view right now, for the status line and picture-based estimates (the last >= 20 yd)
        if len(win) >= 2:
            j = next((j for j in range(len(win) - 2, -1, -1) if _dist(win[j][2:4], win[-1][2:4]) >= after), None)
            if j is not None:
                v, kp = self._fit_view(win[j:], tol_b)
                if kp:
                    c["kp"] = round(kp, 3)
                c["odd"] = 0 if v else c.get("odd", 0) + 1
                # you moved 12+ yd and the picture (almost) didn't slide: that isn't the minimap we are looking at
                c["still"] = c.get("still", 0) + 1 if not kp or kp < 0.3 * self.k_now("out") else 0
                if v:
                    c["view"] = v
                elif c["odd"] >= 8 and kp and not (0.85 * self.k_now("out") < kp < 1.15 * self.k_now("in")):
                    c["view"] = "odd"                  # a zoom well off both views, for a good while
        return out

    def _try_scale(self, gr, fid, k):
        """A learned zone's scale: picture slide (px) vs coords change (%), least squares; then its 0 follows
        from the anchor (where the picture pinned it at the crossing)."""
        f = gr["frames"][fid]
        s = f["samples"][-400:]
        PX = PY = X = Y = 0.0
        num = den = 0.0
        pts = []
        for dPX, dPY, dx, dy in s:
            PX, PY, X, Y = PX + dPX, PY + dPY, X + dx, Y + dy / 1.5
            pts.append((PX, PY, X, Y))
            num += PX * X + PY * Y
            den += X * X + Y * Y
        far = max([math.hypot(X, Y) for _, _, X, Y in pts] or [0])
        if far < 2.0 or den <= 0 or num <= 0:           # (num 0: the picture never slid - a scale of 0 froze the path)
            return
        kp = num / den                             # px per % (x)
        res = [math.hypot(a - kp * X, b - kp * Y) for a, b, X, Y in pts]
        if float(np.sqrt(np.mean(np.square(res)))) > 5 + 0.06 * kp * far:
            return
        sx = kp / k
        ax, ay, apx, apy = f["anchor"]
        f.update(pending=False, sx=sx, sy=sx / 1.5, ox=ax - sx * apx, oy=ay - sx / 1.5 * apy)
        f.pop("samples", None)
        self.status = f"Mapping: learned the new zone ({f.get('label') or fid})"

    def loading(self):
        """A loading screen: hearth, portal, instance, relog - a warp. Mapping promised none: stop."""
        if self.cur:
            self._lose("a loading screen (hearth / portal) - you've moved somewhere the path doesn't reach")

    def recover(self, px, py, now):
        """After a pause: carry on from the last good spot, by the PATH, not the label (at Three Corners the
        label said Duskwood / Elwynn while the coords were Redridge's). Every zone's reading of these coords
        that lands within reach of where you were; exactly one, clearly -> carry on there."""
        lg = getattr(self, "last_good", None)
        if not lg:
            return None
        gid, fid0, x0, y0, G0, t0 = lg
        dt = now - t0
        if dt > 300 or gid not in self.grids:
            return None
        off = getattr(self, "off", None)
        if off:
            gap = now - off["t"]
            if gap <= 30 and abs(px - off["x"]) + abs(py - off["y"]) <= 4 * gap + 1:
                off.update(x=px, y=py, t=now)
                return None
            self.off = None                        # the coords jumped again (back out) or a long gap
        gr = self.grids[gid]
        reach = 40 + 40 * dt
        zones = [z for z in quadmap.zone_bounds() if continent(z) == gr.get("continent")] if gr.get("aligned") else []
        cand = []
        for f_id in set(list(gr["frames"]) + zones):
            if f_id not in gr["frames"]:
                if not self.frame_for_zone(gr, f_id):
                    continue
            Gc = self.T(gr, f_id, px, py)
            if Gc is not None:
                cand.append((_dist(Gc, G0), f_id, Gc))
        cand.sort(key=lambda z: z[0])
        if not cand or cand[0][0] > reach:
            return None
        if len(cand) > 1 and cand[1][0] < max(3 * cand[0][0], cand[0][0] + 300):
            return None                            # two zones fit about as well: don't guess
        _, fid, G = cand[0]
        self.cur = {"gid": gid, "fid": fid, "x": px, "y": py, "G": list(G), "t": now, "g": None}
        self.lost = None
        self.status = f"Mapping: picked your path up again ({fid})"
        return gid

    def _lose(self, why):
        if self.cur:                               # remember the last good spot: the path is picked up from it
            self.last_good = (self.cur["gid"], self.cur["fid"], self.cur["x"], self.cur["y"], list(self.cur["G"]),
                              self.cur["t"])
        if self.cur:                               # a gap in the trail, not a line across the map
            tr = self.grids.get(self.cur["gid"], {}).setdefault("trail", [])
            if tr and tr[-1] is not None:
                tr.append(None)
        self.lost = why
        self.cur = None
        self.status = f"Mapping paused: {why}"
        return ("lost", why)

    # ------------------------------------------------------------------ non-Mapping shots
    def place_zone(self, zone, px, py):
        """Outside Mapping mode: a zone's coords on the grid that knows that zone (lower priority shots)."""
        for gid, gr in self.grids.items():
            fid = self.frame_for_zone(gr, zone)
            if fid:
                return gid, self.T(gr, fid, px, py)
        return None, None

    # ------------------------------------------------------------------ for the pages
    def info(self, gid, zone=None):
        gr = self.grids.get(gid)
        if not gr:
            return None
        fid = (self.cur or {}).get("fid") if self.cur and self.cur["gid"] == gid else None
        if zone:
            fid = self.frame_for_zone(gr, zone) or fid
        fid = fid or gr["origin"]["frame"]
        f = gr["frames"].get(fid)
        here = self.cur["G"] if self.cur and self.cur["gid"] == gid else None
        # the level shown is the one the minimap is showing (its view, measured while Mapping follows you);
        # view unknown (Mapping off, the first yards of a stretch): the level follows where you stand
        view = self.cur.get("view") if here and self.cur.get("view") in ("in", "out") else None
        inf = self.world.info_grid(gid, here, self.cur["fid"] if here else None, view)
        if inf and f and f.get("pending") and self.cur and self.cur["fid"] == fid:
            # being measured: a typical zone's scale (~35 yd per %), anchored at your spot, so the dot stays put
            K = inf["px_per_unit"]
            s_ = 35.0
            ox, oy = self.cur["G"][0] - s_ * self.cur["x"], self.cur["G"][1] - s_ / 1.5 * self.cur["y"]
            inf.update(frame=fid, kx=K * s_, ky=K * s_ / 1.5, k=K * s_, origin=[-K * ox, -K * oy], unit="coords",
                       areas=self.areas(gid), trail=(gr.get("trail") or [])[-3000:], provisional=True)
            return inf
        if not inf or not f or f.get("pending"):
            return inf
        K = inf["px_per_unit"]
        inf.update(frame=fid, kx=K * f["sx"], ky=K * f["sy"], k=K * f["sx"],
                   origin=[-K * f["ox"], -K * f["oy"]], unit="coords", areas=self.areas(gid),
                   trail=(gr.get("trail") or [])[-3000:])
        return inf
