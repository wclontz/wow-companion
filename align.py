"""Line a minimap-built dungeon map up with the combat log's world positions, without needing
log positions from the mapping run itself.

Every logged footstep of past runs must stand on floor the minimap showed. For each candidate scale
(map pixels per yard), the best offset is found by correlating the footsteps with the map's floor
mask (FFT); the scale+offset that puts the most footsteps on the floor wins, then gets refined.
Result is saved into the map's meta.json as align = {k, bx, by}: image pixel = k * (-y, -x) + (bx, by).

    python align.py "The Hall of Thanes"          # fit, save, and write <place>_check.png
"""
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

from minimapper import ROOT, safe

HERE = os.path.dirname(os.path.abspath(__file__))


def footsteps(place):
    rec = json.load(open(os.path.join(HERE, "instances", safe(place) + ".json"), encoding="utf-8"))
    pts, pulls = [], []
    for r in rec["runs"].values():
        tr = [p[1:] for p in r.get("trail", [])]
        # fill in between logged points (walking is straight-ish over a few yards)
        for a, b in zip(tr, tr[1:]):
            n = max(1, int(np.hypot(b[0] - a[0], b[1] - a[1]) / 3))
            if n > 12:
                continue                                  # a long gap = a jump (death run, etc.)
            for i in range(n):
                pts.append((a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n))
        pulls += [f["pos"] for f in r.get("fights", []) if f.get("pos")]
    return np.array(pts), np.array(pulls)


def fit(mask, uv, ks):
    H, W = mask.shape
    best = None
    for k in ks:
        p = uv * k
        x0, y0 = p.min(axis=0)
        w, h = int(p[:, 0].max() - x0) + 1, int(p[:, 1].max() - y0) + 1
        S = (H + h + 2, W + w + 2)
        F = np.zeros(S); M = np.zeros(S)
        ix = (p[:, 0] - x0).astype(int); iy = (p[:, 1] - y0).astype(int)
        np.add.at(F, (iy, ix), 1)
        M[:H, :W] = mask
        # correlation: for each shift, how many footsteps land on floor
        c = np.fft.ifft2(np.fft.fft2(M) * np.conj(np.fft.fft2(F))).real
        iy0, ix0 = np.unravel_index(np.argmax(c), c.shape)
        score = c[iy0, ix0] / len(p)
        if best is None or score > best[0]:
            best = (score, k, ix0 - x0, iy0 - y0)
    return best


def main(place):
    d = os.path.join(ROOT, safe(place))
    img = Image.open(os.path.join(d, safe(place) + ".png")).convert("RGBA")
    a = np.asarray(img)
    mask = (a[..., 3] > 0).astype(float)
    pts, pulls = footsteps(place)
    if len(pts) < 50:
        print("not enough recorded route to line the map up yet")
        return None
    uv = np.stack([-pts[:, 1], -pts[:, 0]], axis=1)           # world x north, y west -> right/down
    # shrinking the route always "fits" (a small blob fits in any room), so the answer is the
    # LARGEST scale at which nearly every footstep is still on floor
    scores = [(k, fit(mask, uv, [k])) for k in np.arange(0.40, 2.01, 0.01)]
    top = max(s[0] for _, s in scores)
    good = [(k, s) for k, s in scores if s[0] >= top - 0.01]
    k, (score, _, bx, by) = max(good, key=lambda t: t[0])
    coarse = (top, k)
    print(f"{len(pts)} footsteps: {score:.0%} on the map's floor at k={k:.3f} px/yd, offset ({bx:.0f}, {by:.0f})"
          f"  [coarse {coarse[0]:.0%} @ {coarse[1]:.2f}]")
    meta_p = os.path.join(d, "meta.json")
    m = json.load(open(meta_p, encoding="utf-8"))
    # stored in canvas coordinates (image pixel + canvas origin), so it survives the map growing
    ox, oy = m.get("origin") or [0, 0]
    m["align"] = {"k": float(k), "cx": float(bx + ox), "cy": float(by + oy), "score": round(float(score), 3),
                  "n": len(pts)}
    json.dump(m, open(meta_p, "w", encoding="utf-8"), indent=0)
    # picture to eyeball it: footsteps (gold), pulls (green), on the map
    chk = img.copy()
    dr = ImageDraw.Draw(chk)
    for u, v in uv:
        x, y = k * u + bx, k * v + by
        dr.point((x, y), fill=(255, 215, 80, 255))
    for px, py in pulls:
        x, y = k * -py + bx, k * -px + by
        dr.ellipse((x - 3, y - 3, x + 3, y + 3), outline=(60, 230, 90, 255), width=2)
    out = os.path.join(d, safe(place) + "_check.png")
    chk.resize((chk.width * 2, chk.height * 2), Image.NEAREST).save(out)
    print("check picture:", out)
    return m["align"]


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "The Hall of Thanes")
