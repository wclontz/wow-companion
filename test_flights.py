"""The flight points table against spots measured in the game, and its drawing on a zone's map."""
import json
import math
import flights
from worldmap import continent

bb = json.load(open("zone_bounds.json", encoding="utf-8"))
# measured by the add-on (world yards) at the flight master
SEEN = {"Thor": (-10625.7, 1037.9), "Baldruc": (-3829.9, -4519.3), "Ariena Stormfeather": (-9434.7, -2236.7)}
for p in flights.POINTS:
    assert continent(p[1]), ("zone without a continent", p)
    assert 0 <= p[2] <= 100 and 0 <= p[3] <= 100, p
    if p[5] in SEEN:
        at = flights.world(p, bb)
        assert at and math.hypot(at[0] - SEEN[p[5]][0], at[1] - SEEN[p[5]][1]) < flights.NEAR / 2, (p, at)
assert len(flights.MASTERS) == len(flights.POINTS), "a flight master twice"
out = flights.on_map("Westfall", bb, continent, {"Thor": [(-10625.7, 1037.9)], "Ariena Stormfeather": [(-9901.2, -736.1)]})
by = {f["label"]: f for f in out}
assert abs(by["Sentinel Hill"]["fx"] - 56.6) < 0.15 and abs(by["Sentinel Hill"]["fy"] - 52.6) < 0.15 and by["Sentinel Hill"]["used"]
# used, but the only recorded spot was mid-flight (far away): the table's spot stays
lk = flights.world(next(p for p in flights.POINTS if p[4] == "Lakeshire"), bb)
assert by["Lakeshire"]["kind"] == "flight_a" and by["Lakeshire"]["used"] and by["Lakeshire"]["fx"] > 100
assert abs(by["Lakeshire"]["fy"] - (bb["Westfall"][0] - lk[0]) / (bb["Westfall"][0] - bb["Westfall"][1]) * 100) < 0.01
# a spot measured near the table's is drawn instead of it
m = {f["label"]: f for f in flights.on_map("Redridge Mountains", bb, continent, {"Ariena Stormfeather": [(-9901.2, -736.1), (-9434.7, -2236.7)]})}
assert abs(m["Lakeshire"]["fx"] - 25.59) < 0.05 and abs(m["Lakeshire"]["fy"] - 59.38) < 0.05, m["Lakeshire"]
assert by["Booty Bay"]["kind"] in ("flight_a", "flight_h") and "Ratchet" not in by
k = {f["label"]: f["kind"] for f in flights.on_map("The Barrens", bb, continent)}
assert k["Ratchet"] == "flight_n" and k["The Crossroads"] == "flight_h" and k["Theramore"] == "flight_a", k
assert flights.on_map("Nowhere", bb, continent) == []
# your side is the side of the flight masters you have used; unknown until then
mine = {f["label"] for f in flights.on_map("The Barrens", bb, continent, {"Baldruc": [(-3829.9, -4519.3)]}) if f["mine"]}
# (the zones drawn grow as zone_bounds.json learns more of them: check what must be in and what must not)
assert mine >= {"Auberdine", "Stonetalon Peak", "Theramore", "Ratchet"}, mine
assert not mine & {p[4] for p in flights.POINTS if p[0] == flights.H and p[4] not in ("Ratchet",)} - {p[4] for p in flights.POINTS if p[0] != flights.H}, mine
assert not any(f["mine"] for f in flights.on_map("The Barrens", bb, continent))
print("not drawn yet (zone edges unknown):", ", ".join(flights.missing(bb)))
print("ALL PASS")
