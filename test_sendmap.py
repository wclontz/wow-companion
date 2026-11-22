"""Send my map (mapshare.send / receive / Under): one player's drawn map, sent as a small package, shows under
another player's map of the same continent in the right place - their two maps have different 0,0s and are
drawn at different scales - and the receiver's own ground stays on top. A bad package is refused.

    python test_sendmap.py
"""
import io
import json
import os
import sys
import tempfile
import zipfile

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mapshare  # noqa: E402
import minimapper as MM  # noqa: E402
from worldmap import WorldMap  # noqa: E402

tmp = tempfile.mkdtemp()
mapshare.FRIENDS = os.path.join(tmp, "friends")
N = 320
rnd = np.random.default_rng(9)
world = np.zeros((1500, 1500, 3), np.uint8)                      # the world, 1 px a yard
world[...] = (90, 110, 50)
world += np.kron((rnd.random((75, 75, 1)) * 40).astype(np.uint8), np.ones((20, 20, 1), np.uint8))   # fields, so no shot is "blank"
world[700:760, 380:440] = (200, 40, 40)                          # a red barn at world (380..440, 700..760)
world[700:760, 900:960] = (40, 40, 200)                          # a blue pond at world (900..960, 700..760)


def shot(wx, wy, k, paint=None):
    """The minimap at world (wx, wy), k px a yard."""
    half = N / 2 / k
    src = world if paint is None else paint
    im = Image.fromarray(src).crop((int(wx - half), int(wy - half), int(wx + half), int(wy + half))).resize((N, N), Image.BILINEAR)
    im = im.convert("RGBA")
    im.putalpha(MM.disc_mask(N // 2))
    return im


def make(root, gid, w0, k, xs, paint=None):
    w = WorldMap(root)
    t = 1000.0
    for wx in xs:
        w.add(gid, shot(wx, 730, k, paint), wx - w0[0], 730 - w0[1], t, "x", mapping=True, k_hint=k)
        t += 1
    json.dump({"id": gid, "name": "Test Land", "continent": "Test Land", "aligned": True, "w0": w0},
              open(os.path.join(root, gid, "grid.json"), "w"))
    w.dirty[gid] = None
    assert w.compose(gid), w.status
    return w


def colour_at(w, root, gid, w0, wx, wy):
    m = w.meta(gid)
    X, Y = m["draw_k"] * (wx - w0[0]), m["draw_k"] * (wy - w0[1])
    tx, ty = int(X // 1000), int(Y // 1000)
    try:
        im = Image.open(os.path.join(root, gid, "tiles", f"L0_{tx}_{ty}.png")).convert("RGBA")
    except OSError:
        return None
    return im.getpixel((int(X - tx * 1000), int(Y - ty * 1000)))


# the sender walked past the barn (west); 0,0 at world (100, 200); an outdoor view at 0.8 px a yard
ra = os.path.join(tmp, "a")
mapshare.GRIDS = ra
A0 = [100.0, 200.0]
wa = make(ra, "LandA", A0, 0.8, range(300, 520, 20))
r, g, b, al = colour_at(wa, ra, "LandA", A0, 410, 735)
assert r > 150 and b < 90 and al == 255, "the sender's own map has the barn"
data = mapshare.send("LandA")
assert len(data) < 400_000 and mapshare.kind(data) == mapshare.PICTURE
assert b"LandA" not in data, "the package names the continent, not the sender's map"

# one zone of it: only that zone's rectangle goes (a zone = a frame with known edges; here world x 300..400)
gj = os.path.join(ra, "LandA", "grid.json")
gr = json.load(open(gj))
gr["frames"] = {"West Half": {"zone": "West Half", "bounds": True, "ox": 300 - A0[0], "oy": 500 - A0[1], "sx": 1.0, "sy": 5.0}}
json.dump(gr, open(gj, "w"))
assert mapshare.zones("LandA") == ["West Half"]
part = mapshare.send("LandA", "West Half")
k = wa.meta("LandA")["draw_k"]
with zipfile.ZipFile(io.BytesIO(part)) as z:
    lit = 0
    for nm in z.namelist():
        if nm.endswith(".webp"):
            tx, ty = (int(v) for v in nm[:-5].split("_")[1:])
            al = np.asarray(Image.open(io.BytesIO(z.read(nm))).convert("RGBA"))[..., 3] > 0
            xs = (np.nonzero(al.any(axis=0))[0] + tx * 1000) / k + A0[0]
            assert xs.size == 0 or (xs.min() >= 298 and xs.max() <= 402), ("nothing outside the zone", xs.min(), xs.max())
            lit += int(al.sum())
    assert lit > 1000, lit
try:
    mapshare.send("LandA", "Nowhere")
    raise AssertionError("sent a zone the map does not know")
except ValueError:
    pass

# the receiver walked past the pond (east); another 0,0 and another scale (0.6)
rb = os.path.join(tmp, "b")
mapshare.GRIDS = rb
B0 = [-250.0, 40.0]
wb = make(rb, "LandB", B0, 0.6, range(820, 1040, 20))
assert colour_at(wb, rb, "LandB", B0, 410, 735) in (None, (0, 0, 0, 0)), "the receiver has not been to the barn"
cont, kept, how = mapshare.import_package(data)
assert cont == "Test Land" and kept >= 1 and "friend" in how and mapshare.friends()[0]["continent"] == "Test Land"
wb.dirty["LandB"] = None
assert wb.compose("LandB")
r, g, b, al = colour_at(wb, rb, "LandB", B0, 410, 735)
assert r > 150 and b < 90 and al == 255, ("the friend's barn shows, where it is in the world", (r, g, b, al))
for wx, wy in ((370, 735), (450, 735), (410, 690), (410, 770)):          # just outside the barn: grass
    c = colour_at(wb, rb, "LandB", B0, wx, wy)
    assert c and c[0] < 140, ("the barn is in the right place to within 10 yd", (wx, wy), c)
r, g, b, al = colour_at(wb, rb, "LandB", B0, 930, 735)
assert b > 150 and r < 90, "the receiver's own pond is still drawn"

# the receiver's own shots cover the friend's: paint the barn green on the receiver's side
mine = world.copy()
mine[700:760, 380:440] = (30, 220, 30)
t = 5000.0
for wx in range(300, 520, 20):
    wb.add("LandB", shot(wx, 730, 0.6, mine), wx - B0[0], 730 - B0[1], t, "x", mapping=True, k_hint=0.6)
    t += 1
wb.dirty["LandB"] = None
wb.compose("LandB")
r, g, b, al = colour_at(wb, rb, "LandB", B0, 410, 735)
assert g > 150 and r < 90, ("your own ground is on top of a friend's", (r, g, b))

# forgetting it: the receiver's map is as it was
assert mapshare.forget() == 1 and not mapshare.friends()
wb.dirty["LandB"] = None
wb.compose("LandB")
assert colour_at(wb, rb, "LandB", B0, 930, 735)[2] > 150

# packages that are not what they say
def pack(desc, files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("package.json", json.dumps(desc))
        for name, blob in files.items():
            z.writestr(name, blob)
    return buf.getvalue()


good = {"kind": mapshare.PICTURE, "continent": "Test Land", "k": 0.8, "w0": [0, 0], "tile": 1000}
webp = io.BytesIO()
Image.new("RGBA", (1000, 1000), (1, 2, 3, 255)).save(webp, "WEBP")
png = io.BytesIO()
Image.new("RGBA", (1000, 1000), (1, 2, 3, 255)).save(png, "PNG")
small = io.BytesIO()
Image.new("RGBA", (10, 10)).save(small, "WEBP")
for desc, files, why in (
        (dict(good, k=99), {"tiles/L0_0_0.webp": webp.getvalue()}, "a nonsense scale"),
        (dict(good, continent="../../x"), {"tiles/L0_0_0.webp": b"junk"}, "junk tiles"),
        (good, {"tiles/../../evil.webp": webp.getvalue()}, "a path that climbs out"),
        (good, {"tiles/L0_0_0.webp": png.getvalue()}, "a PNG named .webp"),
        (good, {"tiles/L0_0_0.webp": small.getvalue()}, "a tile of the wrong size"),
        (dict(good, w0="here"), {"tiles/L0_0_0.webp": webp.getvalue()}, "no position")):
    try:
        mapshare.receive(pack(desc, files))
    except ValueError:
        continue
    raise AssertionError("accepted: " + why)
assert not mapshare.friends() and not [f for f in os.listdir(tmp) if "evil" in f]
assert mapshare.receive(pack(good, {"tiles/L0_0_0.webp": webp.getvalue(), "tiles/L0_1_0.webp": b"junk", "x.exe": b"MZ"})) == ("Test Land", 1)
assert sorted(os.listdir(os.path.join(mapshare.FRIENDS, "TestLand"))) == ["package.json", "tiles"]
print("ALL PASS")
