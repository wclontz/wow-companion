"""Zone exits for the maps: where one zone's map hands over to the next.

Two sources. LEARNED: every time you cross a border on the ground the spot is kept (places.json, kind "exit",
name "Zone A|Zone B" in alphabetical order): exact, because the add-on gave the position at the moment its zone
map changed. SEED: a short hand-typed list of well-known passes, in Classic's map coords from memory, so a map is
not empty before you have walked it; drawn fainter, and dropped once a learned exit of the same two zones is
within NEAR yards.
"""
import math

NEAR = 400.0            # yd: a learned exit this close to a seed of the same two zones replaces it
FAST = 20.0             # yd / s: faster than a mount (a flight, a teleport, a boat's loading screen): not an exit
GAP = 20.0              # s: two readings further apart than this say nothing about how you got there

# (the zone the coords are in, x, y, the zone on the other side, the pass's name)
SEED = [
    ("Stonetalon Mountains", 78.2, 42.8, "Ashenvale", "Talondeep Path"),
    ("The Barrens", 35.3, 27.6, "Stonetalon Mountains", ""),
    ("Stonetalon Mountains", 30.0, 79.5, "Desolace", ""),
    ("The Barrens", 48.6, 5.5, "Ashenvale", ""),
    ("The Barrens", 62.3, 19.3, "Durotar", ""),
    ("The Barrens", 41.0, 58.5, "Mulgore", ""),
    ("The Barrens", 49.9, 78.6, "Dustwallow Marsh", ""),
    ("The Barrens", 44.2, 91.0, "Thousand Needles", "The Great Lift"),
    ("Ashenvale", 29.0, 14.5, "Darkshore", ""),
    ("Ashenvale", 55.8, 29.5, "Felwood", ""),
    ("Ashenvale", 93.5, 47.5, "Azshara", ""),
    ("Felwood", 65.0, 8.0, "Winterspring", "Timbermaw Hold"),
    ("Desolace", 44.0, 96.5, "Feralas", ""),
    ("Thousand Needles", 75.0, 93.5, "Tanaris", ""),
    ("Elwynn Forest", 32.0, 49.5, "Stormwind City", ""),
    ("Elwynn Forest", 20.8, 79.8, "Westfall", ""),
    ("Elwynn Forest", 93.0, 72.3, "Redridge Mountains", ""),
    ("Dun Morogh", 53.5, 35.0, "Ironforge", ""),
    ("Dun Morogh", 86.2, 51.2, "Loch Modan", "South Gate Pass"),
    ("Dun Morogh", 84.3, 31.1, "Loch Modan", "North Gate Pass"),
    ("Loch Modan", 25.4, 10.6, "Wetlands", "Dun Algaz"),
    ("Wetlands", 51.0, 10.0, "Arathi Highlands", "Thandol Span"),
    ("Arathi Highlands", 20.4, 30.4, "Hillsbrad Foothills", ""),
    ("Duskwood", 44.5, 88.0, "Stranglethorn Vale", ""),
]


def pair(a, b):
    return "|".join(sorted((a, b)))


class Watch:
    """Fed the add-on's readings one after another; says when you have just crossed a border on the ground."""

    def __init__(self):
        self.prev = None

    def feed(self, zone, continent, pos, t, taxi=False, instance=False):
        """-> (pair name, continent, north-south, west-east) at a crossing, else None. `zone` = the zone the
        add-on's map id names (None when unknown), `pos` = (north-south, west-east) or None."""
        now = (zone, continent, pos, t) if zone and continent and pos and not taxi and not instance else None
        prev, self.prev = self.prev, now
        if not now or not prev or prev[0] == zone or prev[1] != continent:
            return None
        dt = t - prev[3]
        if dt <= 0 or dt > GAP or math.hypot(pos[0] - prev[2][0], pos[1] - prev[2][1]) > FAST * max(dt, 1.0):
            return None
        return pair(prev[0], zone), continent, pos[0], pos[1]


def _label(a, b, frame, name=""):
    text = "to " + (b if a == frame else a) if frame in (a, b) else a + " / " + b
    return text + (" (" + name + ")" if name else "")


def on_map(frame, bounds, continent, learned=()):
    """The exits of the continent of `frame` (the zone the map is drawn in), in that zone's coords:
    [{"kind": "exit", "label", "fx", "fy", "used"}] (`used` False = from the seed list, not walked yet).
    `learned` = [(pair name, north-south, west-east)] of this continent."""
    fb = bounds.get(frame)
    here = continent(frame)
    if not fb or not here or fb[0] == fb[1] or fb[2] == fb[3]:
        return []
    fn, fs, fw, fe = fb

    def spot(ns, we, label, used):
        return {"kind": "exit", "label": label, "used": used,
                "fx": round((fw - we) / (fw - fe) * 100, 2), "fy": round((fn - ns) / (fn - fs) * 100, 2)}

    out, got = [], []
    for name, ns, we in learned:
        a, _, b = name.partition("|")
        if not b:
            continue
        got.append((name, ns, we))
        out.append(spot(ns, we, _label(a, b, frame), True))
    for zone, x, y, other, name in SEED:
        zb = bounds.get(zone)
        if continent(zone) != here or not zb or zb[0] == zb[1] or zb[2] == zb[3]:
            continue
        ns, we = zb[0] - y / 100 * (zb[0] - zb[1]), zb[2] - x / 100 * (zb[2] - zb[3])
        if any(n == pair(zone, other) and math.hypot(ns - s, we - w) <= NEAR for n, s, w in got):
            continue
        out.append(spot(ns, we, _label(zone, other, frame, name), False))
    return out
