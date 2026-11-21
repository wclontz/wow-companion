"""Simulated open-world walk for quadmap.py. Must say ALL PASS before trusting quadrant-map changes.

A made-up zone (no repeating patterns) and a cave under part of it (different picture, indoor zoom).
The walk: outdoor loop -> minimap zoomed in mid-walk -> into the cave (under ground already mapped)
-> back out over mapped ground -> a teleport -> new ground. Coords have one decimal like the game;
some frames have a menu over the minimap or are blank. Checks: one zoom per view found, the cave is
its own level, the drawn maps match the truth where drawn, nothing from junk frames got drawn.
"""
import math
import os
import random
import shutil
import sys
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quadmap
from minimapper import disc_mask

K0 = 29.0           # truth texture: px per % east-west (north-south = / 1.5), like Westfall
R = 111


def texture(seed, w, h, sharp=1.0):
    rng = np.random.default_rng(seed)
    acc = np.zeros((h, w, 3))
    for blur, amp in ((40 / sharp, 90), (12 / sharp, 50), (4 / sharp, 25), (1.2 / sharp, 12)):
        n = Image.fromarray((rng.random((h, w, 3)) * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(blur))
        a = np.asarray(n, dtype=float)
        a = (a - a.mean()) / (a.std() + 1e-9)
        acc += a * amp
    acc = acc - acc.min()
    acc = acc / acc.max() * 200 + 25
    return Image.fromarray(acc.astype(np.uint8))


def shot(tex, x, y, kz, junk=None):
    """The minimap at coords (x, y) with zoom kz px/% (2R x 2R RGBA, masked like the live grab)."""
    cx, cy = K0 * x, K0 / 1.5 * y
    half = R * K0 / kz
    im = tex.crop((int(cx - half), int(cy - half), int(cx + half), int(cy + half))).resize((2 * R, 2 * R), Image.BILINEAR)
    d = ImageDraw.Draw(im)
    d.polygon([(R - 8, R + 6), (R + 9, R), (R - 8, R - 6)], fill=(240, 240, 240))       # your arrow
    d.ellipse((R + 80 - 20, R - 116 - 20, R + 80 + 20, R - 116 + 20), fill=(250, 210, 40))  # the sun button
    if junk == "menu":
        d.rectangle((30, 40, 200, 190), fill=(45, 45, 45))
        for yy in range(50, 185, 16):
            d.rectangle((40, yy, 170, yy + 6), fill=(250, 250, 250))
    if junk == "blank":
        im = Image.new("RGB", im.size, (20, 60, 25))
    im = im.convert("RGBA")
    im.putalpha(disc_mask(R))
    return im


def walk(start, heading, steps, step_pct, rng, turn=0.35, lo=12, hi=88):
    x, y = start
    out = []
    for _ in range(steps):
        heading += rng.uniform(-turn, turn)
        nx = x + math.cos(heading) * step_pct
        ny = y + math.sin(heading) * step_pct * 1.5      # same yards north-south = 1.5x the %
        if not (lo < nx < hi and lo < ny < hi):          # stay inside the made-up zone: turn around
            heading += math.pi
            continue
        x, y = nx, ny
        out.append((x, y))
    return out, heading


def run(seed):
    rng = random.Random(seed)
    W, H = int(K0 * 100), int(K0 / 1.5 * 100)
    outdoor = texture(seed, W, H)
    cave = texture(seed + 100, W, H, sharp=2.5)       # indoor minimaps are drawn sharp at their zoom
    root = tempfile.mkdtemp(prefix="quadtest_")
    q = quadmap.QuadMap(root)
    place = "Testfall"
    t = 1000.0
    junk_drawn = 0
    plan = []
    p1, h = walk((30, 35), 0.3, 70, 0.45, rng)
    plan += [(x, y, "out", 29.0) for x, y in p1]
    p2, h = walk(p1[-1], h, 30, 0.45, rng)
    plan += [(x, y, "out", 40.0) for x, y in p2]          # you scrolled the minimap zoom in
    # cave entrance: back to a spot already mapped, the minimap switches to the cave (indoor zoom)
    ce = p1[25]
    plan.append(("jump",))
    p3, h = walk(ce, 1.2, 25, 0.25, rng, turn=0.2)
    plan += [(x, y, "cave", 29.0 * 2.77) for x, y in p3]
    plan.append(("jump",))
    p4, h = walk(p1[40], 2.0, 25, 0.45, rng)
    plan += [(x, y, "out", 29.0) for x, y in p4]           # back outside over mapped ground
    plan.append(("jump",))                                 # hearth / teleport
    p5, h = walk((70, 70), -0.5, 30, 0.45, rng)
    plan += [(x, y, "out", 29.0) for x, y in p5]
    truth_layer = {}
    n = 0
    for st in plan:
        if st[0] == "jump":
            q.last.pop(place, None)                        # the mapper breaks the run on a coords jump
            t += 20
            continue
        x, y, where, kz = st
        n += 1
        junk = "menu" if n % 17 == 0 else ("blank" if n % 23 == 0 else None)
        im = shot(cave if where == "cave" else outdoor, x, y, kz, junk)
        qx, qy = round(x, 1), round(y, 1)                 # the game shows one decimal
        before = len(q.meta(place)["clips"])
        q.add(place, im, qx, qy, t, "Testfall Cave" if where == "cave" else "Testfall")
        m = q.meta(place)
        if len(m["clips"]) > before:
            c = m["clips"][-1]
            truth_layer[c["id"]] = where
            if junk:
                c["_junk"] = True
        t += 2.0
    q.compose(place)
    m = q.meta(place)
    res = {}
    # zoom
    ref = m["ref_k"] or 0
    res["ref_k"] = round(ref, 2)
    ok_k = abs(ref - 29.0) / 29.0 < 0.03 or abs(ref - 40.0) / 40.0 < 0.03
    # layers: every drawn outdoor shot on one layer, every drawn cave shot on another
    by = {}
    for c, Rr in q.drawable(m):
        by.setdefault(truth_layer.get(c["id"]), set()).add(Rr["layer"])
        if c.get("_junk"):
            junk_drawn += 1
    res["layers"] = {k: sorted(v) for k, v in by.items()}
    ok_layers = len(by.get("out", ())) == 1 and len(by.get("cave", ())) == 1 and by["out"] != by["cave"]
    # picture check: drawn maps vs the truth (east-west scale ref, north-south ref / 1.5)
    scores = {}
    drawn = m.get("drawn") or {}
    for where, tex in (("out", outdoor), ("cave", cave)):
        Ls = by.get(where)
        if not Ls:
            continue
        L = next(iter(Ls))
        img = Image.open(os.path.join(root, quadmap.safe(place), drawn[str(L)])).convert("RGBA")
        X0, Y0 = m["origin"]
        f = K0 / ref
        sub = tex.resize((int(W / f), int(H / f)), Image.BILINEAR)
        a = np.asarray(img, dtype=float)
        tx = np.zeros_like(a[..., :3])
        crop = sub.crop((X0, Y0, X0 + img.width, Y0 + img.height))
        tx[:crop.height, :crop.width] = np.asarray(crop, dtype=float)[:img.height, :img.width]
        ok = a[..., 3] > 200
        g1, g2 = a[..., :3].mean(axis=2)[ok], tx.mean(axis=2)[ok]
        g1, g2 = g1 - g1.mean(), g2 - g2.mean()
        scores[where] = round(float((g1 * g2).sum() / (np.sqrt((g1 * g1).sum() * (g2 * g2).sum()) + 1e-9)), 3)
        holes = 0
        scores[where + "_px"] = int(ok.sum())
    res["match"] = scores
    res["junk_drawn"] = junk_drawn
    ok_pic = all(scores.get(w, 0) > 0.85 for w in ("out", "cave"))
    shutil.rmtree(root, ignore_errors=True)
    good = ok_k and ok_layers and ok_pic and junk_drawn == 0
    return good, res


if __name__ == "__main__":
    allok = True
    for seed in (1, 2, 3):
        ok, res = run(seed)
        allok &= ok
        print("PASS" if ok else "FAIL", "seed", seed, res)
    print("ALL PASS" if allok else "SOME FAILED")
