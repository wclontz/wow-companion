"""Rebuild a Mapping grid from its raw clips (maps/minimap_raw/<grid>/), replaying the path with the
current rules. Use when the map got something wrong: the raw clips keep every Mapping reading's zone
coords, label, time and picture. Writes into maps/grids_rebuild/ (swap it in by hand / via the companion).

    python replay_grid.py EasternKingdoms
"""
import glob
import json
import os
import shutil
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import grids as G
from minimapper import disc_mask
from location import ZONES


def resolver():
    try:
        sz = json.load(open(os.path.join(HERE, "subzones.json"), encoding="utf-8"))
    except (OSError, ValueError):
        sz = {}
    names = {z.lower(): z for z in ZONES}

    def resolve(lab):
        k = (lab or "").strip(" .").lower()
        return names.get(k) or (sz.get(k) or None)
    return resolve


LIVE_CHECKED = 1791630700    # 2026-10-10 07:11: clips from here on were only archived once their view was confirmed


def clip_views(recs, k_out, k_in, tol=0.12):
    """Each raw clip's minimap view (outdoor / indoor zoom), from the clips around it: the picture's slide vs
    how far you went (zone coords), against neighbours up to 12 s before AND after. A clip gets a view only
    when at least one earlier and one later neighbour both measure the same view - a pair across a view
    switch measures neither (or disagrees), so clips at a switch get none and aren't drawn."""
    import math
    from minimapper import phase_shift
    from quadmap import gray_core
    cache = {}

    def gc(r):
        if r["path"] not in cache:
            try:
                cache[r["path"]] = gray_core(Image.open(r["path"]).convert("RGB"))
            except OSError:
                cache[r["path"]] = None
        return cache[r["path"]]

    def pair(a, b):
        if a.get("frame") != b.get("frame") or a.get("x") is None or b.get("x") is None:
            return None
        dG = math.hypot(b["x"] - a["x"], b["y"] - a["y"])
        if dG < 12:
            return None
        ga, gb = gc(a), gc(b)
        if ga is None or gb is None or ga.shape != gb.shape:
            return None
        dx, dy, pk = phase_shift(ga, gb)
        s = math.hypot(dx, dy)
        if pk < 10 or s < 4 or s > 0.4 * ga.shape[0]:
            return None
        if -(dx * (b["x"] - a["x"]) + dy * (b["y"] - a["y"])) / (s * dG) < 0.9:
            return None                                # the slide must run against your movement
        kp = s / dG
        for v, k0 in (("out", k_out), ("in", k_in)):
            if abs(math.log(kp / k0)) <= tol:
                return v
        return "odd"
    views = {}
    bad = set()                                        # clips whose live view is known wrong (bad_views.json)
    for f in {os.path.dirname(os.path.dirname(r["path"])) for r in recs}:
        try:
            bad |= {os.path.normpath(os.path.join(f, p)) for p in json.load(open(os.path.join(f, "bad_views.json")))["clips"]}
        except (OSError, ValueError, KeyError):
            pass
    for i, r in enumerate(recs):
        if os.path.normpath(r["path"]) in bad:
            continue
        if r.get("view") in ("out", "in") and r.get("t", 0) >= LIVE_CHECKED:
            views[r["path"]] = r["view"]               # confirmed live (track before + after it): kept
            continue
        before = [pair(recs[j], r) for j in range(max(0, i - 4), i) if r["t"] - recs[j]["t"] <= 12]
        after = [pair(r, recs[j]) for j in range(i + 1, min(len(recs), i + 5)) if recs[j]["t"] - r["t"] <= 12]
        # (a pair that matches neither zoom is a bad picture match or straddles a switch: no vote)
        vb = {v for v in before if v and v != "odd"}
        va = {v for v in after if v and v != "odd"}
        if len(vb) == 1 and vb == va:                  # every usable measurement, both sides, one view
            views[r["path"]] = vb.pop()
    # The open world: a clip that couldn't be measured itself (sparse clips, a picture that didn't match) is
    # outdoor when the measured clips around it in time all are (one within 20 s each side, nothing indoor
    # within 60 s) and no indoor view was ever measured within 300 yd of it - the minimap only switches to
    # its indoor zoom in cities, caves and buildings.
    ins = [(r["x"], r["y"]) for r in recs if views.get(r["path"]) == "in"]
    meas = [(r["t"], views[r["path"]]) for r in recs if r["path"] in views]
    import bisect
    ts = [t for t, v in meas]
    for r in recs:
        if r["path"] in views or r.get("x") is None:
            continue
        i = bisect.bisect_left(ts, r["t"])
        near = [v for t, v in meas[max(0, i - 40):i + 40] if abs(t - r["t"]) <= 60]
        before = any(0 < r["t"] - t <= 20 for t, v in meas[max(0, i - 10):i])
        after = any(0 < t - r["t"] <= 20 for t, v in meas[i:i + 10])
        if before and after and near and all(v == "out" for v in near) and                 all((r["x"] - x) ** 2 + (r["y"] - y) ** 2 > 300 ** 2 for x, y in ins):
            views[r["path"]] = "out"
    return views


def replay(gid, k=0.81, out=None):
    out = out or os.path.join(HERE, "maps", "grids_rebuild")
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)
    recs = []
    for f in glob.glob(os.path.join(HERE, "maps", "minimap_raw", gid, "*", "clips.jsonl")):
        d = os.path.dirname(f)
        for ln in open(f, encoding="utf-8"):
            r = json.loads(ln)
            if r.get("zx") is not None:
                r["path"] = os.path.join(d, r["file"])
                recs.append(r)
    recs.sort(key=lambda r: r["t"])
    old = json.load(open(os.path.join(HERE, "maps", "grids", gid, "grid.json"), encoding="utf-8"))
    g = G.Grids(out, resolve_label=resolver())
    g.session = old.get("trail_session")
    o = old["origin"]
    new_gid = g.new_grid(o.get("label"), o["x"], o["y"], o["t"], zone_hint=o.get("frame"))
    assert new_gid == gid, (new_gid, gid)
    stats = {"placed": 0, "skipped": 0, "lost": 0, "recovered": 0, "waiting": 0, "no_view": 0}
    views = clip_views(recs, g.k_now("out"), g.k_now("in"))   # (offline: both sides of every clip are known)
    for r in recs:
        try:
            im = Image.open(r["path"]).convert("RGBA")
        except OSError:
            continue
        n = im.width // 2
        im.putalpha(Image.fromarray(np.minimum(np.asarray(im)[..., 3], np.asarray(disc_mask(n)))))
        if g.cur is None:
            if g.recover(r["zx"], r["zy"], r["t"]):
                stats["recovered"] += 1
            elif g.start(r.get("label"), r["zx"], r["zy"], r["t"], zone_hint=r.get("frame")) and g.cur:
                stats["restarted"] = stats.get("restarted", 0) + 1   # (a new play session: as Mapping does)
            else:
                stats["waiting"] += 1
                continue
        res = g.step(im, r["zx"], r["zy"], r.get("label"), r["t"])   # (the path: where this clip is)
        if res[0] in ("place", "wait") and g.cur and not g.grids[gid]["frames"][g.cur["fid"]].get("pending"):
            view = views.get(r["path"])
            if not view:
                stats["no_view"] += 1
                continue
            Gp = g.cur["G"]
            g.world.add(gid, im, Gp[0], Gp[1], r["t"], r.get("label"), mapping=True, k_hint=g.k_now(view),
                        frame=g.cur["fid"])
            stats["placed"] += 1
            stats[view] = stats.get(view, 0) + 1
        elif res[0] == "lost":
            stats["lost"] += 1
        else:
            stats["skipped"] += 1
    g.save(gid)
    g.world.compose(gid)
    return stats, g


if __name__ == "__main__":
    stats, g = replay(sys.argv[1] if len(sys.argv) > 1 else "EasternKingdoms")
    print(stats, g.world.status)
