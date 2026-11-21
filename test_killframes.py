"""Kill shots when the dungeon run is only known later (2026-11-21: 25 kills in the open-world Deadmines gave
3 pictures, because frames were only kept while a run was known and the log named the run minutes late).

Near a dungeon - the add-on's zone or minimap name is a dungeon's or a room's, or its instance flag is on -
the mapper keeps a frame a second whatever mode it is in; a kill the log reports later finds its picture.
Also: the indoor scale of a picture on a character with its own layout, zoomed all the way out.

    python test_killframes.py
"""
import io
import os
import sys
import tempfile

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import beacon  # noqa: E402
import minimapper as MM  # noqa: E402

tmp = tempfile.mkdtemp()
MM.ROOT, MM.RAW_ROOT = os.path.join(tmp, "minimap"), os.path.join(tmp, "minimap_raw")


class Clock:
    t = 1_800_000_000.0

    def time(self):
        return self.t


clock = Clock()
MM.time = clock
rnd = np.random.default_rng(3)
world = (rnd.random((900, 900, 3)) * 255).astype(np.uint8)       # a busy picture: never "blank"


def grab(box):
    x0, y0, x1, y1 = (int(v) for v in box)
    return Image.fromarray(world[:max(1, y1 - y0), :max(1, x1 - x0)])


strip = {}
beacon.latest = lambda max_age=1.5: strip.get("d")
m = MM.MinimapMapper(grab)
m.use_beacon = False
m.dungeon_names = {MM.norm_place("The Deadmines"), MM.norm_place("Ironclad Cove")}
m._spool_save = lambda fr: None
m.screen_ok_t = None
saved = []
m.archive = lambda place, unit, t, P, png, kind, **extra: saved.append((place, kind, round(t)))
m._snap_anchor = lambda place, meta, t, P, unit, img=None, checked=False: saved.append((place, "anchor", img.size))
m.fix_live = lambda *a, **k: None

assert not m._near_dungeon(), "no strip: not near a dungeon"
strip["d"] = {"instance": False, "texts": {"zone": "Westfall", "label": "Sentinel Hill"}, "zoom": 0}
assert not m._near_dungeon(), "an ordinary zone"
strip["d"] = {"instance": False, "texts": {"zone": "The Deadmines", "label": "The Deadmines"}, "zoom": 0}
assert m._near_dungeon(), "the zone is a dungeon's name"
strip["d"] = {"instance": False, "texts": {"zone": "Westfall", "label": "Ironclad Cove"}, "zoom": 0}
assert m._near_dungeon(), "the minimap names a room of a dungeon"
strip["d"] = {"instance": True, "inst": 36, "texts": {}, "zoom": 1}
assert m._near_dungeon(), "the add-on's instance flag"

# a minute of frames while no run is known, then the log reports two kills of that minute, late
t0 = clock.t
for s in range(60):
    clock.t = t0 + s
    m._keep_frame()
assert len(m.buf) == 60 and m.dbg.get("frames_kept") == 60, (len(m.buf), m.dbg)
im = Image.open(io.BytesIO(m.buf[0]["png"]))
assert im.mode == "RGBA" and im.size[0] == 2 * m.geom()["r"], (im.mode, im.size)
clock.t = t0 + 400                                              # the log, 6 minutes on
m.kill_snap("The Deadmines (open world)", t0 + 21.0, (t0 + 20.3, -11200.0, 1650.0))
m.kill_snap("The Deadmines (open world)", t0 + 44.0, (t0 + 43.6, -11210.0, 1660.0))
kinds = [k for _, k, _ in saved]
assert kinds == ["kill", "anchor", "kill", "anchor"], saved
assert not m.dbg.get("kill_no_frame"), m.dbg
# a kill from before any frame was kept has no picture, and none is made up
m.kill_snap("The Deadmines (open world)", t0 - 30.0, (t0 - 30.5, -11200.0, 1650.0))
assert m.dbg.get("kill_no_frame") == 1 and len(saved) == 4, (m.dbg, saved)
# a loading screen (black) is not kept
world[:] = 0
n = len(m.buf)
m._keep_frame()
assert len(m.buf) == n, "a black frame was kept"

# the indoor scale of a picture: the layout's measured one on the main character; frame radius / 150 on a
# character with its own layout whose minimap is zoomed all the way out indoors
MM.k_indoor = lambda: 1.395
m._spot = lambda: None
assert abs(m.k_shot() - 1.395) < 1e-9
m._spot = lambda: (1793.5, 156.5, 99.0)
strip["d"] = {"zoom": 0}
assert abs(m.k_shot() - m.frame_r() / 150.0) < 1e-9 and 1.05 < m.k_shot() < 1.2, m.k_shot()
strip["d"] = {"zoom": 1}
assert abs(m.k_shot() - 1.395) < 1e-9, "one step in: the measured scale"
print("ALL PASS")
