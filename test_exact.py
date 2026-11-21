"""Mapping with the position add-on (grids.step_exact), on a made-up landscape. Must say ALL PASS.

An empty maps folder. Walk in Elwynn and over the line into Redridge: the continent gets its map by itself,
every shot lands on its true spot, the zone line costs nothing. Then a street shown in the minimap's indoor
zoom: its shots must come out as "in". Then a boat to Kalimdor (a zone with no name yet): a second map,
nothing "lost". Then a hearth back: the first map carries on, with a gap in the trail.
"""
import math
import os
import random
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quadmap
import grids as G
from minimapper import disc_mask
from test_quad import texture

K, KIN, R = 1.17, 1.17 * 1.53, 111


def run(seed):
    rng = random.Random(seed)
    bb = quadmap.zone_bounds()

    def world(z, px, py):
        N, S, W, E = bb[z]
        return (-W + px / 100 * (W - E), -N + py / 100 * (N - S))

    def pct(z, ex, sy):
        N, S, W, E = bb[z]
        return ((ex + W) / (W - E) * 100, (sy + N) / (N - S) * 100)
    a = world("Elwynn Forest", 90.0, 60.0)
    b = world("Redridge Mountains", 10.0, 52.0)
    ex0, sy0 = min(a[0], b[0]) - 500, min(a[1], b[1]) - 500
    tex = texture(seed, int((abs(b[0] - a[0]) + 1000) * K), int((abs(b[1] - a[1]) + 1000) * K), sharp=1.5)

    def shot(ex, sy, k=K):
        cx, cy = (ex - ex0) * K, (sy - sy0) * K
        r = R * K / k                              # the indoor zoom shows less ground in the same circle
        im = tex.crop((int(cx - r), int(cy - r), int(cx + r), int(cy + r))).resize((2 * R, 2 * R)).convert("RGBA")
        im.putalpha(disc_mask(R))
        return im
    root = tempfile.mkdtemp(prefix="exacttest_")
    g = G.Grids(root)
    g.k_now = lambda view="out": K if view != "in" else KIN
    res, t, errs, views = {}, 0.0, [], {"out": 0, "in": 0}
    o = None

    def feed(ex, sy, zone, k=K, cont="Eastern Kingdoms"):
        nonlocal o
        if o is None:
            o = (ex, sy)
        px, py = pct(zone, ex, sy) if zone else (0, 0)
        r = g.step_exact(shot(ex, sy, k), cont, -sy, -ex, zone, round(px, 2), round(py, 2), "Somewhere", t, shot=(ex, sy, k))
        if r[0] == "lost":
            res["lost"] = r[1]
        if r[0] == "place":
            for (ex_, sy_, k_), Gp, v, _k in r[2]:
                if cont == "Eastern Kingdoms":
                    errs.append(math.hypot(Gp[0] - (ex_ - o[0]), Gp[1] - (sy_ - o[1])))
                    views[v] += 1
                    if (v == "in") != (k_ == KIN):
                        res["wrong_view"] = res.get("wrong_view", 0) + 1
        return r
    n = int(math.hypot(b[0] - a[0], b[1] - a[1]) / 3.5)
    ex = sy = 0
    for i in range(n + 1):                         # the walk, over the zone line
        f = i / n
        ex, sy = a[0] + (b[0] - a[0]) * f + rng.uniform(-.5, .5), a[1] + (b[1] - a[1]) * f + rng.uniform(-.5, .5)
        z = "Elwynn Forest" if pct("Elwynn Forest", ex, sy)[0] < 94.2 else "Redridge Mountains"
        t += 0.5
        feed(ex, sy, z)
    res["frames"] = sorted(g.grids["EasternKingdoms"]["frames"])
    out_before = views["out"]
    for i in range(40):                            # a street in the indoor zoom
        ex, sy = ex + 2.0, sy + rng.uniform(-.3, .3)
        t += 0.5
        feed(ex, sy, "Redridge Mountains", KIN)
    res["views"] = dict(views)
    res["place_err_yd_max"] = round(max(errs), 2) if errs else None
    t += 40                                        # the boat: another continent, a zone with no name yet
    for i in range(20):
        t += 0.5
        r = feed(a[0] + 3.5 * i, a[1], None, cont="Kalimdor")
    res["grids"] = sorted(g.grids)
    res["kalimdor_at"] = [round(v) for v in g.cur["G"]]
    t += 30                                        # the hearth back
    r = feed(a[0] + 40, a[1] + 40, "Elwynn Forest")
    res["back"] = (g.cur["gid"], g.cur["fid"], [round(v) for v in g.cur["G"]])
    tr = g.grids["EasternKingdoms"]["trail"]
    res["trail_gap"] = None in tr
    shutil.rmtree(root, ignore_errors=True)
    ok = (not res.get("lost") and not res.get("wrong_view") and res["place_err_yd_max"] is not None and res["place_err_yd_max"] < 0.2
          and out_before >= n * 0.8 and views["in"] >= 20 and res["grids"] == ["EasternKingdoms", "Kalimdor"]
          and res["kalimdor_at"] == [round(3.5 * 19), 0] and res["back"] == ("EasternKingdoms", "Elwynn Forest", [40, 40])
          and "Elwynn Forest" in res["frames"] and "Redridge Mountains" in res["frames"] and res["trail_gap"])
    return ok, res


if __name__ == "__main__":
    allok = True
    for seed in (1, 2, 3):
        ok, res = run(seed)
        allok = allok and ok
        print("PASS" if ok else "FAIL", "seed", seed, res)
    print("ALL PASS" if allok else "FAILED")
