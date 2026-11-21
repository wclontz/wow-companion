"""Simulated city walk for Mapping (grids.py + worldmap). Must say ALL PASS before trusting city changes.

Outdoors in Elwynn (outdoor minimap view, 0.81 px/yd) -> in through Stormwind's gates (coords switch to the
city's map AND the minimap switches to WoW's indoor view, 1.24 px/yd, different art) -> around the city ->
down a ramp to an under-level (same coords, different picture) -> back up -> the minimap zoomed in for a
while (must NOT be drawn) -> zoomed back out -> back out through the gates.
Checks: the view of every reading, where shots land, which level they go on, nothing drawn while zoomed in,
and the drawn city level against the true city picture.
"""
import math
import os
import random
import shutil
import sys
import tempfile

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quadmap
import grids as G
from minimapper import disc_mask
from test_quad import texture

K_OUT, K_IN = 0.81, 1.24
R = 166


def run(seed, seamless=False):
    rng = random.Random(seed)
    bb = quadmap.zone_bounds()
    El, SW = "Elwynn Forest", "Stormwind City"

    def world(z, px, py):
        N, S, W, E = bb[z]
        return (-W + px / 100 * (W - E), -N + py / 100 * (N - S))

    def pct(z, ex, sy):
        N, S, W, E = bb[z]
        return ((ex + W) / (W - E) * 100, (sy + N) / (N - S) * 100)
    c0 = world(SW, 50, 55)                                  # the city's middle
    CITY = 330                                              # yd: inside this, Stormwind's map + indoor view
    UNDER = (c0[0] + 120, c0[1] + 60, 70)                   # an under-level patch (x, y, radius) inside the city
    ex0, sy0 = c0[0] - 1100, c0[1] - 1100
    span = 2200
    out_tex = texture(seed, int(span * K_OUT), int(span * K_OUT))
    in_tex = texture(seed + 50, int(span * K_IN), int(span * K_IN), sharp=1.5)
    if seamless:            # Stormwind: the SAME ground art in both views (canals outdoor, districts indoor) -
        in_tex = out_tex.resize((int(span * K_IN), int(span * K_IN)), Image.BILINEAR)   # the picture never breaks
    under_tex = texture(seed + 90, int(span * K_IN), int(span * K_IN), sharp=1.5)

    def shot(ex, sy, view, zoom=1.0, under=False):
        tex, k = (out_tex, K_OUT) if view == "out" else ((under_tex if under else in_tex), K_IN)
        k *= zoom
        cx, cy = (ex - ex0) * (K_OUT if view == "out" else K_IN), (sy - sy0) * (K_OUT if view == "out" else K_IN)
        half = R * (K_OUT if view == "out" else K_IN) / k
        im = tex.crop((int(cx - half), int(cy - half), int(cx + half), int(cy + half))).resize((2 * R, 2 * R), Image.BILINEAR)
        d = ImageDraw.Draw(im)
        d.polygon([(R - 6, R + 4), (R + 7, R), (R - 6, R - 4)], fill=(240, 240, 240))
        im = im.convert("RGBA")
        im.putalpha(disc_mask(R))
        return im
    # the walk: outside -> gates -> city loop -> under-level -> up -> zoomed-in stretch -> out
    pts = []

    def walk_to(tx, ty, step=9, tag=None):
        if seamless:
            step = 3                                        # live: ~3 readings/s, a few yd apart
        x, y = pts[-1][0], pts[-1][1]
        n = max(1, int(math.hypot(tx - x, ty - y) / step))
        for i in range(1, n + 1):
            pts.append((x + (tx - x) * i / n + rng.uniform(-1, 1), y + (ty - y) * i / n + rng.uniform(-1, 1), tag))
    pts.append((c0[0] - 700, c0[1] + 150, None))
    walk_to(c0[0] - 150, c0[1] + 60)                         # in through the gates
    walk_to(c0[0], c0[1] - 120)
    walk_to(c0[0] + 120, c0[1] - 20)
    walk_to(UNDER[0], UNDER[1], tag=None)                   # to the ramp...
    walk_to(UNDER[0] + 40, UNDER[1] + 40, step=6, tag="under")
    walk_to(UNDER[0] - 40, UNDER[1] + 20, step=6, tag="under")
    walk_to(UNDER[0], UNDER[1], step=6, tag="under")
    walk_to(c0[0] - 60, c0[1] - 60)
    walk_to(c0[0] - 60, c0[1] + 120, tag="zoomed")          # the minimap zoomed in here
    walk_to(c0[0] - 120, c0[1] + 100)
    walk_to(c0[0] - 750, c0[1] + 200)                       # back out
    root = tempfile.mkdtemp(prefix="citytest_")
    g = G.Grids(root, resolve_label=lambda lab: lab if lab in (El, SW) else None)
    g.k_now = lambda view="out": K_IN if view == "in" else K_OUT
    t = 0.0
    x0, y0 = pct(El, pts[0][0], pts[0][1])
    gid = g.start(El, round(x0, 1), round(y0, 1), t)
    o = world(El, round(x0, 1), round(y0, 1))
    stats = {"view_wrong": 0, "placed_in": 0, "placed_out": 0, "placed_while_zoomed": 0, "lost": 0, "err": []}
    truth_view = {}
    prev = (pts[0][0], pts[0][1], "out")
    real_ps = G.phase_shift
    for ex, sy, tag in pts[1:]:
        inside = math.hypot(ex - c0[0], sy - c0[1]) < CITY
        z = SW if inside else El
        canal = seamless and inside and abs(sy - (c0[1] - 40)) < 22 and tag != "under"
        view = "in" if inside and not canal else "out"   # (a canal strip across the city: outdoor view)
        px, py = pct(z, ex, sy)
        im = shot(ex, sy, view, zoom=2.0 if tag == "zoomed" else 1.0, under=(tag == "under"))
        t += 1.0 if not seamless else 0.4
        if g.cur is None:
            g.recover(round(px, 1), round(py, 1), t)
            if g.cur is None:
                stats["lost"] += 1
                continue
        if seamless and view != prev[2]:
            # the worst case seen in Stormwind: across the switch the picture still "matches", confidently,
            # moving exactly as the OLD zoom would - nothing in the picture says the zoom changed
            kp = K_IN if prev[2] == "in" else K_OUT
            G.phase_shift = lambda a, b, _s=(-(ex - prev[0]) * kp, -(sy - prev[1]) * kp): (round(_s[0]), round(_s[1]), 50.0)
        try:
            res = g.step(im, round(px, 1), round(py, 1), z, t, shot=(im, ex, sy, tag, view, t, z))
        finally:
            G.phase_shift = real_ps
        prev = (ex, sy, view)
        if res[0] == "lost":
            stats["lost"] += 1
        if res[0] != "place":
            continue
        gid_ = res[1]
        for (im_, ex_, sy_, tag_, view_, t_, z_), Gp, v, kv in res[2]:
            if tag_ == "zoomed":
                stats["placed_while_zoomed"] += 1
            if v != view_:
                stats["view_wrong"] += 1
            stats["placed_" + v] += 1
            stats["err"].append(math.hypot(Gp[0] - (ex_ - o[0]), Gp[1] - (sy_ - o[1])))
            g.world.add(gid_, im_, Gp[0], Gp[1], t_, z_, mapping=True, k_hint=kv)
            m = g.world.meta(gid_)
            if m["clips"] and m["clips"][-1]["t"] == round(t_, 1):
                truth_view[m["clips"][-1]["id"]] = "under" if tag_ == "under" else view_
    g.world.compose(gid)
    m = g.world.meta(gid)
    by = {}
    for cl, Rr in g.world.drawable(m):
        by.setdefault(truth_view.get(cl["id"], "?"), set()).add(Rr["layer"])
    res = {k: v for k, v in stats.items() if k != "err"}
    res["err_max_yd"] = round(max(stats["err"]), 1) if stats["err"] else None
    res["levels"] = {k: sorted(v) for k, v in by.items()}
    ok = bool(stats["view_wrong"] <= 2 and stats["placed_while_zoomed"] == 0 and stats["placed_in"] > 20
          and stats["placed_out"] > 20 and stats["lost"] == 0 and (res["err_max_yd"] or 99) < 12
          and by.get("out") == {0} and by.get("in") == {1} and by.get("under") and 1 not in by["under"]
          and 0 not in by["under"])
    shutil.rmtree(root, ignore_errors=True)
    return ok, res


if __name__ == "__main__":
    allok = True
    for seed in (1, 2, 3):
        for seamless in (False, True):
            ok, res = run(seed, seamless)
            if seamless:        # view switches with no break in the picture: not one shot at the wrong size
                ok = ok and res["view_wrong"] == 0
            allok &= ok
            print("PASS" if ok else "FAIL", "seed", seed, "seamless" if seamless else "", res)
    print("ALL PASS" if allok else "SOME FAILED")
