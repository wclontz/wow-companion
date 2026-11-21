"""Rebuild a minimap map from the raw clip archive (maps/minimap_raw/<place>/<session>/).

The raw archive keeps every minimap picture taken and the position it was taken at, untouched by
stitching. If a map comes out wrong, fix the stitching code and rebuild it from the clips:

    python rebuild_from_raw.py "The Hall of Thanes"                 # all sessions, into maps/minimap/<place>
    python rebuild_from_raw.py "The Hall of Thanes" 2026-10-05_2300 # only sessions starting with that
    python rebuild_from_raw.py "The Hall of Thanes" --out test      # into maps/minimap_rebuilt/<place> instead

The current map (if any) is moved aside to maps/minimap_old/<place>_<time> first (never deleted).
Stop the companion before rebuilding into maps/minimap (it holds the map in memory).
"""
import glob
import io
import json
import math
import os
import shutil
import sys
import time

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import minimapper as MM  # noqa: E402
import restitch as RS  # noqa: E402


class Clock:
    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def strftime(self, *a):
        return time.strftime(*a)

    def localtime(self, *a):
        return time.localtime(*a)


def main(place, session=None, test_out=False):
    src = os.path.join(MM.RAW_ROOT, MM.safe(place))
    sessions = sorted(d for d in glob.glob(os.path.join(src, "*")) if os.path.isdir(d)
                      and (not session or os.path.basename(d).startswith(session)))
    clips = []
    for s in sessions:
        try:
            with open(os.path.join(s, "clips.jsonl"), encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue                         # a line cut short by a kill mid-write
                    rec["_dir"] = s
                    clips.append(rec)
        except OSError:
            continue
    if not clips:
        print("no raw clips for", place)
        return
    walks = [r for r in clips if r.get("kind") == "walk"]
    clips = [r for r in clips if r.get("kind") != "walk"]     # walking clips have ESTIMATED spots: never top layer
    clips.sort(key=lambda r: r["t"])
    world = [r for r in clips if r.get("unit") == "world"]
    if len(world) >= 3:
        # clips thousands of yards from the rest are another map filed under this name (the Deadmines' open-world
        # mine and its instance, before they were told apart): leave them out
        xs, ys = sorted(r["x"] for r in world), sorted(r["y"] for r in world)
        mx, my = xs[len(xs) // 2], ys[len(ys) // 2]
        far = [r for r in world if math.hypot(r["x"] - mx, r["y"] - my) > 3000]
        if far:
            print(f"left out {len(far)} clips taken on another map (thousands of yards away)")
            clips = [r for r in clips if r not in far]
            walks = [r for r in walks if math.hypot(r["x"] - mx, r["y"] - my) <= 3000]
    if not clips:                                        # (checked BEFORE the current map is moved aside)
        print("only walking clips for", place, "- nothing to rebuild from")
        return
    root = os.path.join(HERE, "maps", "minimap_rebuilt") if test_out else MM.ROOT
    dest = os.path.join(root, MM.safe(place))
    if os.path.isdir(dest):
        old = os.path.join(HERE, "maps", "minimap_old", f"{MM.safe(place)}_{time.strftime('%Y%m%d_%H%M%S')}")
        os.makedirs(os.path.dirname(old), exist_ok=True)
        shutil.move(dest, old)
        print("moved the old map to", old)
    MM.ROOT = RS.ROOT = root
    RS._CACHE.clear()
    clock = Clock(clips[0]["t"])
    MM.time = clock
    mm = MM.MinimapMapper(lambda box: None)
    m = mm.meta(place)
    k_now = MM.k_indoor()
    last_n = 0
    for rec in clips:
        clock.t = rec["t"]
        try:
            img = Image.open(os.path.join(rec["_dir"], rec["file"]))
            img.load()
        except OSError:
            continue                                     # a missing or cut-short picture
        # the scale the clip was taken at: written in it (clips since 2026-11-16), or told by its size (old UI)
        kpx = rec.get("kpx") or MM.OLD_SHOT_K.get(img.width) or k_now
        mm._snap_anchor(place, m, rec["t"], (rec["x"], rec["y"]), rec["unit"], img=img, checked=True,
                        kpx=kpx if rec["unit"] == "world" else None)
        if len(m["cells"]) >= last_n + 10:
            last_n = len(m["cells"])
            mm.compose(place)
    mm.compose(place)
    # bottom layer: walking clips at their estimated spots, drawn only under the log layer
    m = mm.meta(place)
    al = m.get("align") or {}
    if al.get("k"):
        r = mm.geom()["r"]
        for rec in sorted(walks, key=lambda r: r["t"]):
            u, v = -rec["y"], -rec["x"]
            px, py = al["k"] * u + al.get("cx", 0.0), (al.get("ky") or al["k"]) * v + al.get("cy", 0.0)
            if any(math.hypot(c["px"] - px, c["py"] - py) < 0.6 * r for c in m["cells"].values()):
                continue
            key = f"w{sum(1 for c in m['cells'].values() if c.get('fill')) + 1}"
            os.makedirs(os.path.join(MM.ROOT, MM.safe(place)), exist_ok=True)
            shutil.copy(os.path.join(rec["_dir"], rec["file"]), os.path.join(MM.ROOT, MM.safe(place), key + ".png"))
            try:
                wk = rec.get("kpx") or MM.OLD_SHOT_K.get(Image.open(os.path.join(rec["_dir"], rec["file"])).width) or k_now
            except OSError:
                continue
            m["cells"][key] = {"file": key + ".png", "px": px, "py": py, "off": 1000.0, "t": rec["t"], "u": u, "v": v,
                               "layer": 0, "fill": True, "kpx": wk}
        mm.compose(place)
    m = mm.meta(place)
    al = m.get("align") or {}
    floors = sorted({c.get("layer", 0) for c in m["cells"].values()})
    print(f"rebuilt {place}: {len(clips)} clips -> {len(m['cells'])} shots, scale {al.get('k')}, floors {[f + 1 for f in floors]}")
    print("map:", os.path.join(dest, MM.safe(place) + ".png"))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--out" and a != "test"]
    main(args[0] if args else "The Hall of Thanes", args[1] if len(args) > 1 else None,
         test_out="--out" in sys.argv)
