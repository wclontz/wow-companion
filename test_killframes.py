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
kpxs = []
m._snap_anchor = lambda place, meta, t, P, unit, img=None, checked=False, kpx=None: (saved.append((place, "anchor", img.size)), kpxs.append(kpx))
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
assert kpxs and all(k and abs(k - m.k_shot()) < 1e-9 for k in kpxs), ("a kill shot goes to the map with the scale it was taken at", kpxs)
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
# the tracking button's arc on the rim of another layout's shot is cut away; the rest of the picture stays
pic = Image.new("RGBA", (320, 320), (200, 100, 50, 255))
cut = np.asarray(MM.cut_rim_button(pic))[..., 3]
assert cut[20, 245] == 0 and cut[160, 160] == 255 and cut[300, 245] == 255 and cut[20, 75] == 255, "only the top-right rim"
assert 0.02 < (cut == 0).mean() < 0.1, (cut == 0).mean()

# a kill shot from a character at another scale lands on a map made at the main character's scale: it is brought
# to the map's scale first, so it matches what is there (same floor) instead of being held back
import math
MM.k_indoor = lambda: 1.4
m2 = MM.MinimapMapper(grab)
m2.use_beacon = False
big = (np.kron(np.random.default_rng(8).random((40, 40, 3)), np.ones((30, 30, 1))) * 255).astype(np.uint8)   # 1200 px of "dungeon"


def view(u, v, k):
    """The minimap at map spot (u, v) - the mapper's u = -(log y), v = -(log x) - k px a yard, as a 320 px disc
    (ground px = 1.4 * yards + 600)."""
    half = 160 / k * 1.4
    cx, cy = 1.4 * u + 600, 1.4 * v + 600
    im = Image.fromarray(big).crop((int(cx - half), int(cy - half), int(cx + half), int(cy + half))).resize((320, 320), Image.BILINEAR)
    im = im.convert("RGBA")
    im.putalpha(MM.disc_mask(160))
    return im


meta = m2.meta("Test Mine")
meta["unit"] = "world"
meta["align"] = {"k": 1.4, "ky": 1.4, "cx": 0.0, "cy": 0.0, "source": "log"}
for i in range(8):                                               # the main character's run: 8 shots, 20 yd apart
    m2._snap_anchor("Test Mine", meta, clock.t, (0.0, -(20.0 * i - 70)), "world", img=view(20.0 * i - 70, 0.0, 1.4), checked=True, kpx=1.4)
n0 = len(meta["cells"])
assert n0 == 8, n0
m2.dbg = {}
for u in (-55.0, 5.0, 45.0):                                     # the other character, between those spots, at 1.12
    m2._snap_anchor("Test Mine", meta, clock.t, (-14.0, -u), "world", img=view(u, 14.0, 1.12), checked=True, kpx=1.12)
assert not m2.dbg.get("floor_pending"), ("held back as another floor", m2.dbg)
assert len(meta["cells"]) == n0 + 3 and {c.get("layer", 0) for c in meta["cells"].values()} == {0}, m2.dbg
print("ALL PASS")
