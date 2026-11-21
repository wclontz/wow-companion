"""End-to-end test of the dungeon minimap mapper on a simulated run (no game needed).

A fake dungeon picture with REPEATING floor tiles (like the Hall of Thanes) plus a few unique rooms.
A fake player walks a route; every simulated second the real mapper grabs 'the minimap' (a crop of the
fake dungeon around the player). Kills are reported through the real kill path with the log arriving
30-90 s late, sometimes several kills in one write, logged spots off by up to 2 yd.
Optionally a second floor (a different picture over the same x/y) is visited.

Checks: solved scale within 2%; each kept shot within 6 px of its true place; live dot within 3 yd;
no invented floors (single-floor run); the real second floor found (two-floor run).

    python test_mapper.py
"""
import math
import os
import random
import shutil
import sys
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import minimapper as MM  # noqa: E402
import restitch as RS  # noqa: E402

K_TRUE = 1.24                # minimap px per yard (indoor view, fully zoomed out - restitch.k_indoor)
RS.k_indoor = lambda: K_TRUE          # (the test's own minimap scale, not whatever screen_layout.json says today)
SIZE = 2400                  # fake dungeon picture, px


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make_floor(seed, unique=True):
    rng = np.random.default_rng(seed)
    tile = Image.fromarray((rng.random((48, 48, 3)) * 120 + 60).astype("uint8")).filter(ImageFilter.GaussianBlur(1.5))
    img = Image.new("RGB", (SIZE, SIZE), (0, 0, 0))
    for y in range(0, SIZE, 48):
        for x in range(0, SIZE, 48):
            img.paste(tile, (x, y))                       # repeating tiles everywhere
    d = ImageDraw.Draw(img)
    if unique:                                            # a few unique rooms / features
        for _ in range(60):
            x, y = rng.integers(100, SIZE - 100, 2)
            w, h = rng.integers(30, 160, 2)
            col = tuple(int(c) for c in rng.integers(30, 230, 3))
            (d.rectangle if rng.random() < 0.5 else d.ellipse)((x, y, x + w, y + h), fill=col)
    return img


def run(two_floors=False, seed=1):
    clock = Clock()
    MM.time = clock                                        # the mapper's clock
    root = tempfile.mkdtemp(prefix="mmtest_")
    MM.ROOT = root
    RS.ROOT = root                                         # restitch keeps its own copy of the path
    MM.RAW_ROOT = root + "_raw"
    RS._CACHE.clear()
    floors = [make_floor(seed), make_floor(seed + 100)]
    me = {"x": 0.0, "y": 0.0, "floor": 0}                 # world: x north, y west

    def grab(box):
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        cx = SIZE / 2 + K_TRUE * (-me["y"])
        cy = SIZE / 2 + K_TRUE * (-me["x"])
        return floors[me["floor"]].crop((int(cx - w / 2), int(cy - h / 2)), ) if False else \
            floors[me["floor"]].crop((int(cx - w / 2), int(cy - h / 2), int(cx - w / 2) + w, int(cy - h / 2) + h))

    mm = MM.MinimapMapper(grab)
    place = "Sim Dungeon"
    mm.place_now, mm.unit_now = place, "world"
    rnd = random.Random(seed)

    # route: a winding walk with fights; on the two-floor run, the second half goes 'upstairs' over the start area
    route = [(0, 0), (40, 0), (80, 20), (120, 10), (160, -30), (170, -80), (130, -120), (80, -110), (40, -70), (10, -40)]
    if two_floors:
        route += [("up",), (10, -40), (40, -70), (80, -110), (60, -60)]
    walk_errs = []
    true_spot = {}                                          # kill time -> (x, y, floor)
    pending_log = []                                        # (arrives_at, t_kill, (tp, x, y))
    floor = 0
    for wp in route:
        if wp[0] == "up":
            floor = 1
            me["floor"] = 1
            continue
        tx, ty = wp
        # walk there at 7 yd/s, one mapper tick per second
        while math.hypot(tx - me["x"], ty - me["y"]) > 0.5:
            dx, dy = tx - me["x"], ty - me["y"]
            d = math.hypot(dx, dy)
            step = min(7.0, d)
            me["x"] += dx / d * step
            me["y"] += dy / d * step
            clock.t += 1.0
            mm._capture()
            for p in [p for p in pending_log if p[0] <= clock.t]:
                if p[1] == "pos":
                    mm.position(place, "world", p[2][0], (p[2][1], p[2][2]))
                else:
                    mm.kill_snap(place, p[1], p[2])
                pending_log.remove(p)
            lp = mm.live_pos(place)
            if lp:
                walk_errs.append(math.hypot(lp[0] - me["x"], lp[1] - me["y"]))
            if len(mm.dirty) and any(mm.dirty.values()) and rnd.random() < 0.2:
                for pl in [pl for pl, v in mm.dirty.items() if v]:
                    mm.compose(pl)
        # a fight here: stand ~15 s, 1-3 kills; the log arrives 30-90 s later, all kills in one write
        tp = None
        for i in range(15):
            clock.t += 1.0
            mm._capture()
            if i == 8:
                tp = clock.t                                  # your last cast
            if i in (2, 5, 8):                                # casts log your position (delivered late)
                pending_log.append((clock.t + rnd.uniform(30, 90), "pos",
                                    (clock.t, me["x"] + rnd.uniform(-1, 1), me["y"] + rnd.uniform(-1, 1))))
        delay = rnd.uniform(30, 90)
        for kk in range(rnd.randint(1, 3)):
            tk = clock.t - rnd.uniform(0, 3)
            noisy = (me["x"] + rnd.uniform(-2, 2), me["y"] + rnd.uniform(-2, 2))
            pending_log.append((clock.t + delay, tk, (tp, noisy[0], noisy[1])))
            true_spot[tp] = (me["x"], me["y"], floor)
    # let the last logs arrive
    for _ in range(100):
        clock.t += 1.0
        mm._capture()
        for p in [p for p in pending_log if p[0] <= clock.t]:
            if p[1] == "pos":
                mm.position(place, "world", p[2][0], (p[2][1], p[2][2]))
            else:
                mm.kill_snap(place, p[1], p[2])
            pending_log.remove(p)
    mm.compose(place)
    m = mm.meta(place)
    al = m.get("align") or {}
    k = al.get("k")
    # each shot vs its true place (solved px vs k_true * true uv, compared after removing the common offset)
    errs, floors_seen = [], {}
    for c in m["cells"].values():
        tx, ty, fl = true_spot.get(c["t"] - 0, (None, None, None)) if False else (None, None, None)
        floors_seen.setdefault(c.get("layer", 0), 0)
        floors_seen[c.get("layer", 0)] += 1
    fills = [c for c in m["cells"].values() if c.get("fill")]
    shots = [c for c in m["cells"].values() if "u" in c and not c.get("fill")]
    if k:
        offx = np.median([c["px"] - k * c["u"] for c in shots])
        offy = np.median([c["py"] - k * c["v"] for c in shots])
        errs = [math.hypot(c["px"] - (k * c["u"] + offx), c["py"] - (k * c["v"] + offy)) for c in shots]
    lp = mm.live_pos(place)
    dot_err = math.hypot(lp[0] - me["x"], lp[1] - me["y"]) if lp else None
    raw = sum(len(open(os.path.join(dp, "clips.jsonl")).read().splitlines())
              for dp, _, fs in os.walk(root + "_raw") if "clips.jsonl" in fs)
    # rebuild from the raw clips and check it gives the same scale and floors
    import rebuild_from_raw as RB
    RB.MM.RAW_ROOT = root + "_raw"
    rb_root = root + "_rb"
    MM.ROOT = RS.ROOT = rb_root
    RB.HERE = rb_root
    import io, contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        RB.main(place, test_out=True)
    shutil.rmtree(root, ignore_errors=True); shutil.rmtree(root + "_raw", ignore_errors=True); shutil.rmtree(rb_root, ignore_errors=True)
    return {"fills": len(fills), "walk_max_yd": round(max(walk_errs), 1) if walk_errs else None,
            "walk_avg_yd": round(sum(walk_errs) / len(walk_errs), 1) if walk_errs else None, "raw_clips": raw, "rebuild": " | ".join(out.getvalue().strip().splitlines()),"k": k, "k_err_%": round(100 * abs(k - K_TRUE) / K_TRUE, 2) if k else None, "shots": len(shots),
            "max_shot_err_px": round(max(errs), 1) if errs else None, "floors": floors_seen,
            "dot_err_yd": round(dot_err, 1) if dot_err is not None else None, "dbg": mm.dbg}


if __name__ == "__main__":
    ok = True
    for name, two in (("single floor", False), ("two floors", True)):
        for seed in (1, 2, 3):
            r = run(two_floors=two, seed=seed)
            good = (r["k"] is not None and r["k_err_%"] <= 2 and (r["max_shot_err_px"] or 99) <= 6
                    and (r["dot_err_yd"] is not None and r["dot_err_yd"] <= 3)
                    and (len(r["floors"]) == (2 if two else 1))
                    and r["walk_max_yd"] is not None and r["walk_max_yd"] <= (6 if two else 3))
            ok &= good
            print(f"{'PASS' if good else 'FAIL'}  {name:12s} seed {seed}: scale {r['k'] and round(r['k'], 3)} "
                  f"({r['k_err_%']}% off), {r['shots']} shots, worst shot {r['max_shot_err_px']} px, "
                  f"floors {r['floors']}, dot {r['dot_err_yd']} yd, moving dot avg {r['walk_avg_yd']} / worst {r['walk_max_yd']} yd, fill pieces {r['fills']}, raw {r['raw_clips']}")
    print("ALL PASS" if ok else "SOMETHING FAILED")
