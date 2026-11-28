"""Instance entrances on the maps (entrances.py): the game's own entrance spots, in the map frame's coords."""
import json
import entrances
from worldmap import continent

looks = [
    {"slug": "shadowfang-keep", "kind": "dungeons", "level": "18", "zone": "Shadowfang Keep",
     "entrance": "Entrance: Eastern Kingdoms, world coordinates -231, 1571.6, in"},
    {"slug": "blackfathom-deeps", "kind": "dungeons", "level": "22",
     "entrance": "Entrance: Kalimdor, world coordinates 4249.1, 748.4."},
    {"slug": "molten-core", "kind": "raids", "level": "60",
     "entrance": "Entrance: Eastern Kingdoms, world coordinates -7510.6, -1036.7, in"},
    {"slug": "city-of-dalaran", "kind": "dungeons", "level": "28",
     "entrance": "Entrance: the map row has no entrance coordinates in build 1.60.1.70205."},
]
assert entrances.spot(looks[0]) == ("Eastern Kingdoms", -231.0, 1571.6)
assert entrances.spot(looks[3]) is None and entrances.spot({}) is None and entrances.spot(None) is None
assert entrances.missing(looks) == ["City Of Dalaran"], entrances.missing(looks)

bb = {"Silverpine Forest": [1668.75, -1131.25, 3450.0, -750.0], "Ashenvale": [4672.9, 829.2, 1700.0, -4066.7]}
e = {p["label"]: p for p in entrances.on_map("Silverpine Forest", bb, continent, looks)}
assert set(e) == {"Shadowfang Keep (18)", "Molten Core (60)"}, e          # this continent's only; Dalaran left out
k = e["Shadowfang Keep (18)"]
assert k["kind"] == "dungeon" and k["used"] and 40 < k["fx"] < 50 and 62 < k["fy"] < 73, k   # the keep: about 45, 68
assert e["Molten Core (60)"]["kind"] == "raid" and e["Molten Core (60)"]["fy"] > 100          # far south of this map
a = entrances.on_map("Ashenvale", bb, continent, looks)
assert [p["label"] for p in a] == ["Blackfathom Deeps (22)"] and 5 < a[0]["fx"] < 25 and 5 < a[0]["fy"] < 20, a
assert entrances.on_map("Nowhere", bb, continent, looks) == [] and entrances.on_map("Ashenvale", {}, continent, looks) == []
assert entrances.on_map("Ashenvale", bb, continent, None) == []

# an instance without a spot in the game's data is drawn where you walked in (the spot seen most often)
own = {"City Of Dalaran": [(100.0, 200.0, 1, 50), (300.0, 400.0, 2, 10)], "Shadowfang Keep": [(0.0, 0.0, 9, 9)]}
e = {p["label"]: p for p in entrances.on_map("Silverpine Forest", bb, continent, looks, own)}
d = e["City Of Dalaran (28)"]
N, S, W, E = bb["Silverpine Forest"]
assert d["learned"] and d["fx"] == round((W - 400.0) / (W - E) * 100, 2) and d["fy"] == round((N - 300.0) / (N - S) * 100, 2), d
assert e["Shadowfang Keep (18)"] == k and "learned" not in k              # the game's own spot is not replaced
assert entrances.best([]) is None and entrances.best([(1, 2, 1, 5), (3, 4, 1, 9)]) == (3, 4)
w = entrances.Watch()
assert w.feed(0, "Eastern Kingdoms", (10.0, 20.0), 100) is None
assert w.feed(0, "Eastern Kingdoms", (11.0, 21.0), 101, taxi=True) is None          # (a flight: not a spot)
assert w.feed(2998, None, None, 130, instance=True) == (2998, "Eastern Kingdoms", 10.0, 20.0)
assert w.feed(2998, None, None, 131, instance=True) is None                          # once a visit
assert w.feed(0, "Eastern Kingdoms", (12.0, 22.0), 900) is None
assert w.feed(2998, None, None, 900 + entrances.GAP_IN + 1, instance=True) is None   # too long ago to say
w = entrances.Watch()
assert w.feed(2998, None, None, 5, instance=True) is None                            # started inside: no spot
import places, tempfile, os
pl = places.Places(os.path.join(tempfile.mkdtemp(), "p.json"))
assert pl.add("entrance", "X", "Kalimdor", 1.0, 2.0) == "new" and pl.add("entrance", "X", "Kalimdor", 30.0, 2.0) == "seen"

# the real list: every entry either has a spot or is named as missing
real = list((json.load(open("dungeons.json", encoding="utf-8")).get("_by_map") or {}).values())
assert len([x for x in real if entrances.spot(x)]) + len(entrances.missing(real)) == len(real)
print("ALL PASS")
