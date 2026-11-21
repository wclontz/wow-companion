"""Simulated Mapping session for grids.py. Must say ALL PASS before trusting Mapping changes.

A made-up landscape in world yards. Start a map in Elwynn (its rectangle known), walk east over the
Elwynn/Redridge line with Redridge's rectangle HIDDEN (so it must be learned from the crossing: picture
continuity pins it, walking measures its scale), hearth (must pause), then switch Mapping on again at a
remembered spot (must find you). Checks the learned Redridge placement against the truth.
"""
import math
import os
import random
import shutil
import sys
import tempfile

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quadmap
import grids as G
from minimapper import disc_mask
from test_quad import texture

K = 1.17          # minimap px per yard
R = 111


def run(seed):
    rng = random.Random(seed)
    bb = dict(quadmap.zone_bounds())
    real_rr = bb["Redridge Mountains"]
    hidden = dict(bb)
    hidden.pop("Redridge Mountains")
    quadmap._BOUNDS = hidden                    # Redridge unknown to the mapper

    def world(z, px, py, b=None):
        N, S, W, E = (b or bb)[z]
        return (-W + px / 100 * (W - E), -N + py / 100 * (N - S))

    def pct(z, ex, sy):
        N, S, W, E = bb[z]
        return ((ex + W) / (W - E) * 100, (sy + N) / (N - S) * 100)
    a = world("Elwynn Forest", 86.0, 60.0)
    b = world("Redridge Mountains", 14.0, 52.0)
    ex0, sy0 = min(a[0], b[0]) - 600, min(a[1], b[1]) - 600
    W = int((abs(b[0] - a[0]) + 1200) * K)
    H = int((abs(b[1] - a[1]) + 1200) * K)
    tex = texture(seed, W, H, sharp=1.5)

    def shot(ex, sy):
        cx, cy = (ex - ex0) * K, (sy - sy0) * K
        im = tex.crop((int(cx - R), int(cy - R), int(cx + R), int(cy + R))).convert("RGBA")
        im.putalpha(disc_mask(R))
        return im
    root = tempfile.mkdtemp(prefix="gridtest_")
    labels = {"Elwynn Forest": "Elwynn Forest", "Redridge Mountains": "Lakeridge Highway"}
    g = G.Grids(root, resolve_label=lambda lab: "Elwynn Forest" if lab == "Elwynn Forest" else None)
    g.k_now = lambda view="out": K if view != "in" else K * 1.53   # the made-up world's zooms (not the live layout's)
    t = 0.0
    # start (first ever click: new map here)
    p0 = (86.0, 60.0)
    gid = g.start("Elwynn Forest", *p0, t)
    res = {"gid": gid}
    placed = []
    n = 60
    for i in range(1, n + 1):
        f = i / n
        ex, sy = a[0] + (b[0] - a[0]) * f + rng.uniform(-3, 3), a[1] + (b[1] - a[1]) * f + rng.uniform(-3, 3)
        z = "Elwynn Forest" if pct("Elwynn Forest", ex, sy)[0] < 94.2 else "Redridge Mountains"
        px, py = pct(z, ex, sy)
        t += 2.0
        r = g.step(shot(ex, sy), round(px, 1), round(py, 1), labels[z], t, shot=(ex, sy))
        if r[0] == "lost":
            res["lost_on_walk"] = r[1]
            break
        if r[0] == "place":
            # grid = world - world(origin), so the truth is (ex, sy) - origin
            o = world("Elwynn Forest", *p0)
            for (ex_, sy_), Gp, _v, _k in r[2]:
                placed.append(math.hypot(Gp[0] - (ex_ - o[0]), Gp[1] - (sy_ - o[1])))
    gr = g.grids[gid]
    learned = [f for fid, f in gr["frames"].items() if fid.startswith("F")]
    res["frames"] = sorted(gr["frames"])
    res["learned_pending"] = [f.get("pending") for f in learned]
    if learned and not learned[0].get("pending"):
        f = learned[0]
        o = world("Elwynn Forest", *p0)
        # truth for Redridge %: grid = world(Redridge %) - o
        N, S, Wr, E = real_rr
        res["scale_err_%"] = round(abs(f["sx"] - (Wr - E) / 100) / ((Wr - E) / 100) * 100, 1)
        tx = (-Wr - o[0])
        res["offset_err_yd"] = round(abs(f["ox"] - tx), 1)
    res["place_err_yd_max"] = round(max(placed), 1) if placed else None
    # hearth: coords jump somewhere else entirely
    t += 12
    g.loading()                                 # the hearth's loading screen
    res["hearth"] = "lost" if g.lost and g.cur is None else "kept going"
    # Mapping on again at a remembered spot (back where we walked)
    t += 30
    c = next(iter(gr["cells"].values()))
    hit = g.locate(next(iter(c["labels"])).title(), c["x"], c["y"])
    res["relocate"] = hit
    quadmap._BOUNDS = None
    shutil.rmtree(root, ignore_errors=True)
    ok = (not res.get("lost_on_walk") and res["learned_pending"] == [False] and res.get("scale_err_%", 99) < 4
          and res.get("offset_err_yd", 99) < 25 and (res["place_err_yd_max"] or 99) < 40 and res["hearth"] == "lost"
          and res["relocate"] is not None)
    return ok, res


if __name__ == "__main__":
    allok = True
    for seed in (1, 2, 3):
        ok, res = run(seed)
        allok &= ok
        print("PASS" if ok else "FAIL", "seed", seed, res)
    print("ALL PASS" if allok else "SOME FAILED")
