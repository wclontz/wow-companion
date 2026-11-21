"""Simulated death + ghost run through the real mapper: the ghost run must not end up in the dungeon map."""
import math
import os
import shutil
import sys
import tempfile

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import minimapper as MM  # noqa: E402
import restitch as RS  # noqa: E402
import test_mapper as T  # noqa: E402


def run(seed=1, guards=True):
    clock = T.Clock()
    MM.time = clock
    root = tempfile.mkdtemp(prefix="mmdeath_")
    MM.ROOT = RS.ROOT = root
    MM.RAW_ROOT = root + "_raw"
    RS._CACHE.clear()
    dungeon, outside = T.make_floor(seed), T.make_floor(seed + 500)
    me = {"x": 0.0, "y": 0.0, "pic": "d", "black": False}

    def grab(box):
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        if me["black"]:
            return Image.new("RGB", (w, h), (0, 0, 0))
        img = dungeon if me["pic"] == "d" else outside
        cx = T.SIZE / 2 + T.K_TRUE * (-me["y"])
        cy = T.SIZE / 2 + T.K_TRUE * (-me["x"])
        return img.crop((int(cx - w / 2), int(cy - h / 2), int(cx - w / 2) + w, int(cy - h / 2) + h))

    mm = MM.MinimapMapper(grab)
    if not guards:                            # control: what happens without the death/loading guards
        mm.on_death = lambda *a, **k: None
        orig = mm._capture
        def cap():
            orig(); mm.loading_t = 0
        mm._capture = cap
    place = "Sim Dungeon"
    mm.place_now, mm.unit_now = place, "world"
    pending = []

    def tick(n=1):
        for _ in range(n):
            clock.t += 1
            mm._capture()
            for p in [p for p in pending if p[0] <= clock.t]:
                if p[1] == "pos":
                    mm.position(place, "world", p[2], (p[3], p[4]))
                elif p[1] == "kill":
                    mm.kill_snap(place, p[2], p[3])
                elif p[1] == "death":
                    mm.on_death(place, p[2])
                pending.remove(p)

    def walk(tx, ty):
        while math.hypot(tx - me["x"], ty - me["y"]) > 0.5:
            dx, dy = tx - me["x"], ty - me["y"]
            d = math.hypot(dx, dy)
            me["x"] += dx / d * min(7, d)
            me["y"] += dy / d * min(7, d)
            tick()

    def fight():
        for i in range(12):
            tick()
            if i in (3, 8):
                pending.append((clock.t + 40, "pos", clock.t, me["x"], me["y"]))
        pending.append((clock.t + 40, "kill", clock.t, (clock.t - 3, me["x"], me["y"])))

    S = 2.30 / T.K_TRUE          # (laid out for the old 2.30 px/yd zoom: same picture spacing at today's zoom)
    for wp in [(0, 0), (40, 0), (80, 20), (120, 10), (150, -20), (190, -60), (230, -40)]:
        walk(wp[0] * S, wp[1] * S)
        fight()
        tick(45)
        mm.compose(place)
    tick(50)
    mm.compose(place)
    before = {k for k, c in mm.meta(place)["cells"].items() if c.get("fill")}
    # die here; release: loading screen, ghost at a graveyard elsewhere, run back, loading screen, back in
    t_dead = clock.t
    pending.append((clock.t + 60, "death", t_dead))
    me["black"] = True
    tick(4)
    me["black"], me["pic"] = False, "o"
    me["x"], me["y"] = -300.0, 300.0
    walk(-200, 250)
    walk(-120, 200)
    me["black"] = True
    tick(4)
    me["black"], me["pic"] = False, "d"
    me["x"], me["y"] = 0.0, 0.0            # back at the entrance
    walk(30, 0)
    tick(90)                                # the log (with the death) arrives
    mm.compose(place)
    m = mm.meta(place)
    after = {k for k, c in m["cells"].items() if c.get("fill")}
    ghost = after - before
    al = m.get("align") or {}
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(root + "_raw", ignore_errors=True)
    return {"fills_before": len(before), "fills_from_ghost_run": len(ghost), "scale": round(al.get("k", 0), 3),
            "dbg": mm.dbg}


if __name__ == "__main__":
    ok = True
    ctl = run(1, guards=False)
    print("CONTROL (guards off):", ctl["fills_before"], "walking pieces before death,", ctl["fills_from_ghost_run"], "from the ghost run")
    if ctl["fills_from_ghost_run"] == 0:
        print("control made no ghost pieces: the test can't tell anything"); ok = False
    for seed in (1, 2, 3):
        r = run(seed)
        good = r["fills_from_ghost_run"] == 0 and r["fills_before"] > 0 and abs(r["scale"] - T.K_TRUE) / T.K_TRUE < 0.02
        ok &= good
        print(("PASS" if good else "FAIL"), r)
    print("ALL PASS" if ok else "SOMETHING FAILED")
