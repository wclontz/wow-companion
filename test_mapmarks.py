"""The game's own marks on minimap shots (mapmarks.py): a quest ! and a quest-area ring are found by comparing
shots of one spot, the plain shot is drawn, water that every shot agrees on is left alone, and a plainer shot of
a full square is kept.

    python test_mapmarks.py
"""
import os
import sys
import tempfile

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mapmarks as MK  # noqa: E402
import minimapper as MM  # noqa: E402
from worldmap import WorldMap  # noqa: E402

N = 320
rnd = np.random.default_rng(5)
ground = np.zeros((1200, 1200, 3), np.uint8)
ground[...] = (120, 85, 30)                                        # warm fields
ground += (rnd.random((1200, 1200, 1)) * 25).astype(np.uint8)
ground[470:530, 470:570] = (40, 90, 160)                           # a pond: blue on every shot


def shot(cx, cy, ring=False, icon=False):
    """A minimap picture centred on ground px (cx, cy); the marks sit at fixed places on the ground."""
    g = Image.fromarray(ground.copy())
    d = ImageDraw.Draw(g)
    if ring:
        d.ellipse((560, 560, 700, 680), outline=(170, 200, 255), width=5)
    if icon:
        d.rectangle((640, 500, 645, 516), fill=(250, 220, 0), outline=(20, 15, 0))
    im = g.crop((cx - N // 2, cy - N // 2, cx + N // 2, cy + N // 2)).convert("RGBA")
    im.putalpha(MM.disc_mask(N // 2))
    return im


# the coarse grids, and one square compared across shots
plain, ringed, iconed = (MK.coarse(shot(600, 600, **k)) for k in ({}, {"ring": True}, {"icon": True}))
at = (560 - 440, 600 - 440, 40)                                    # the ring's left edge, in the shot's px
assert MK.square(ringed, *at)[0] > MK.square(plain, *at)[0] + MK.BLUE_TOL, (MK.square(ringed, *at), MK.square(plain, *at))
assert MK.order([MK.square(ringed, *at), MK.square(plain, *at)]) == [1, 0], "the plain shot goes first"
assert MK.order([MK.square(ringed, *at), MK.square(ringed, *at)]) == [0, 1], "all ringed: as they came"
ic = (640 - 440, 500 - 440, 40)
assert MK.square(iconed, *ic)[1] > 20 and MK.square(plain, *ic)[1] == 0
assert MK.order([MK.square(iconed, *ic), MK.square(plain, *ic)]) == [1, 0]
pond = (470 - 440 + 30, 470 - 440 + 10, 40)
a, b = MK.coarse(shot(600, 600)), MK.coarse(shot(590, 606))
assert MK.order([MK.square(a, *pond), MK.square(b, pond[0] + 10, pond[1] - 6, 40)]) == [0, 1], "water: nothing changes"
assert MK.plainer(MK.middle(plain), [MK.middle(ringed)] * 4) and not MK.plainer(MK.middle(ringed), [MK.middle(plain)] * 4)
assert not MK.plainer(MK.middle(plain), [MK.middle(plain)] * 4)

# marks.bin: written, read back, a cut-off record dropped
tmp = tempfile.mkdtemp()
m = MK.Marks(tmp)
c1, c2 = {"id": 1, "t": 10.0}, {"id": 2, "t": 20.0}
m.get(c1, shot(600, 600, ring=True))
m.get(c2, shot(600, 600))
m.flush()
size = os.path.getsize(m.path)
m2 = MK.Marks(tmp)
assert m2.get(c1)[0] == N and (m2.get(c1)[1][0] == ringed[0]).all() and m2.get({"id": 3, "t": 1.0}) is None
assert m2.get({"id": 1, "t": 99.0}) is None, "another clip under an old id is not believed"
open(m.path, "r+b").truncate(size - 100)
m3 = MK.Marks(tmp)
assert m3.get(c1) and m3.get(c2) is None
m3.get(c2, shot(600, 600))
m3.flush()
assert os.path.getsize(m.path) == size and MK.Marks(tmp).get(c2)

# the map: ringed + iconed shots first, then one plain shot of the same ground -> the map shows plain ground
root = tempfile.mkdtemp()
w = WorldMap(root)
K = 0.8                                                            # px a yard (an outdoor view): yards = ground px / K
t = 1000.0
for k in range(12):                                                # a walk east past the ring, marks on
    w.add("Test", shot(520 + 15 * k, 600, ring=True, icon=True), (520 + 15 * k) / K, 600 / K, t, "x", mapping=True, k_hint=K)
    t += 1


def blue_and_yellow():
    w.dirty["Test"] = None
    tiles = w.compose("Test")
    assert tiles, w.status
    d = os.path.join(root, "Test", "tiles")
    blue = yel = 0
    for f in os.listdir(d):
        a = np.asarray(Image.open(os.path.join(d, f)).convert("RGBA")).astype(int)
        tx, ty = (int(v) for v in f[:-4].split("_")[1:])
        ys, xs = np.mgrid[0:a.shape[0], 0:a.shape[1]]
        near = (abs(xs + tx * 1000 - 630) < 90) & (abs(ys + ty * 1000 - 620) < 80) & (a[..., 3] > 0)   # round the ring
        blue += int(((a[..., 2] > a[..., 0] + 40) & near).sum())
        yel += int(((a[..., 0] > 215) & (a[..., 1] > 160) & (a[..., 2] < 50) & near).sum())
    return blue, yel


b0, y0 = blue_and_yellow()
assert b0 > 800, ("the ring is on every shot, so it is drawn", b0)
assert y0 == 0, ("the icon is painted over even when every shot has it", y0)
t += 100
for k in range(12):                                                # the same walk with the quest handed in
    what = w.add("Test", shot(521 + 15 * k, 601), (521 + 15 * k) / K, 601 / K, t, "x", mapping=True, k_hint=K)
    t += 1
b1, y1 = blue_and_yellow()
assert b1 < 0.05 * b0 and y1 == 0, ("the plain shots are drawn where the ring was", b0, b1, y1)
pond_px = 0
for f in os.listdir(os.path.join(root, "Test", "tiles")):
    a = np.asarray(Image.open(os.path.join(root, "Test", "tiles", f)).convert("RGBA")).astype(int)
    pond_px += int(((a[..., 2] > a[..., 0] + 80) & (a[..., 3] > 0)).sum())
assert pond_px > 4000, ("the pond is still there", pond_px)

# a full square (4 ringed shots kept) takes a plainer shot although it is no better centred
root2 = tempfile.mkdtemp()
w2 = WorldMap(root2)
for k in range(6):
    w2.add("T2", shot(620, 620, ring=True), 620 / K, 620 / K, 2000.0 + k, "x", mapping=True, k_hint=K)
n = len(w2.meta("T2")["clips"])
assert n == 4, n
assert w2.add("T2", shot(620, 620, ring=True), 620 / K, 620 / K, 2100.0, "x", mapping=True, k_hint=K) == "skipped"
assert w2.add("T2", shot(620, 620), 620 / K, 620 / K, 2101.0, "x", mapping=True, k_hint=K) == "stored"
assert w2.add("T2", shot(620, 620), 620 / K, 620 / K, 2102.0, "x", mapping=True, k_hint=K) == "skipped", "one plain shot is enough"
print("ALL PASS")
