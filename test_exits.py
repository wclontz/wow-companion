"""Zone exits: a border crossed on the ground is kept; a flight, a teleport or an instance is not."""
import json
import exits
from worldmap import continent

w = exits.Watch()
K = "Kalimdor"
assert w.feed("Stonetalon Mountains", K, (100.0, 100.0), 0) is None
assert w.feed("Stonetalon Mountains", K, (107.0, 100.0), 1) is None
hit = w.feed("Ashenvale", K, (114.0, 100.0), 2)
assert hit == ("Ashenvale|Stonetalon Mountains", K, 114.0, 100.0), hit
assert w.feed("Ashenvale", K, (120.0, 100.0), 3) is None
# on a flight: nothing, and nothing at the landing either
assert w.feed("Darkshore", K, (150.0, 100.0), 4, taxi=True) is None
assert w.feed("Darkshore", K, (180.0, 100.0), 5) is None
# a teleport (far in a second), a long gap, another continent, an unknown zone
assert w.feed("The Barrens", K, (5000.0, 100.0), 6) is None
assert w.feed("Durotar", K, (5005.0, 100.0), 60) is None
assert w.feed("Elwynn Forest", "Eastern Kingdoms", (5010.0, 100.0), 61) is None
assert w.feed(None, "Eastern Kingdoms", (5015.0, 100.0), 62) is None
assert w.feed("Westfall", "Eastern Kingdoms", (5020.0, 100.0), 63) is None
assert w.feed("Elwynn Forest", "Eastern Kingdoms", (5027.0, 100.0), 64, instance=True) is None
# on a mount (14 yd / s), readings 3 s apart: kept
w = exits.Watch()
w.feed("Westfall", "Eastern Kingdoms", (0.0, 0.0), 0)
assert w.feed("Elwynn Forest", "Eastern Kingdoms", (42.0, 0.0), 3)

bb = json.load(open("zone_bounds.json", encoding="utf-8"))
out = exits.on_map("Stonetalon Mountains", bb, continent)
by = {e["label"]: e for e in out}
t = by["to Ashenvale (Talondeep Path)"]
assert abs(t["fx"] - 78.2) < 0.01 and abs(t["fy"] - 42.8) < 0.01 and t["used"] is False and t["kind"] == "exit"
assert "to The Barrens" in by and "to Desolace" in by and "The Barrens / Mulgore" in by, sorted(by)
assert not any("Elwynn" in k for k in by), "another continent's exits"
# walked: the learned spot replaces the seed near it; one far from it (another pass) is added beside it
n, s, wst, e = bb["Stonetalon Mountains"]
at = (n - 0.44 * (n - s), wst - 0.79 * (wst - e))
out = exits.on_map("Stonetalon Mountains", bb, continent, [("Ashenvale|Stonetalon Mountains", at[0], at[1])])
ash = [x for x in out if x["label"].startswith("to Ashenvale")]
assert len(ash) == 1 and ash[0]["used"] and abs(ash[0]["fx"] - 79) < 0.01 and ash[0]["label"] == "to Ashenvale", ash
out = exits.on_map("Stonetalon Mountains", bb, continent, [("Ashenvale|Stonetalon Mountains", at[0] + 2000, at[1])])
assert len([x for x in out if x["label"].startswith("to Ashenvale")]) == 2
assert exits.on_map("Nowhere", bb, continent) == []
for z, x, y, o, _ in exits.SEED:
    assert continent(z) and continent(z) == continent(o) and 0 <= x <= 100 and 0 <= y <= 100, (z, o)

import os, tempfile, places
p = places.Places(os.path.join(tempfile.mkdtemp(), "p.json"))
assert p.add("exit", "Ashenvale|Stonetalon Mountains", K, 10.0, 10.0) == "new"
assert p.add("exit", "Ashenvale|Stonetalon Mountains", K, 90.0, 10.0) == "seen"          # the same pass, a few steps over
assert p.add("exit", "Ashenvale|Stonetalon Mountains", K, 900.0, 10.0) == "new"
print("ALL PASS")
