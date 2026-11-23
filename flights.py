"""The flight points of the old world, for the maps: Alliance, Horde and the ones both use.

A table typed in by hand (side, zone, x, y in that zone's map coords, the place, the flight master). A zone's
points are drawn once the zone's edges are known (zone_bounds.json: learned the first time you enter it), since
that is what turns map coords into a spot in the world. The coords are Classic's; this game's zone maps are not
always Classic's (Redridge's sits 110 yd to the side), so a point can be off by that much until you have used it:
a flight master you have talked to (the add-on's "flight" event, kept in places.json) is marked as used and drawn
where the add-on measured it, when that is within NEAR yards of the table's spot (farther: the event arrived when
you were already in the air, and the table is believed).
"""
import math

NEAR = 250.0
A, H, N = "a", "h", "n"

# Stormwind and Lakeshire are in THIS game's map coords (measured 2026-11-22: its Stormwind and Redridge maps are the
# later game's, not Classic's); the rest are Classic's until measured.
POINTS = [
    # Eastern Kingdoms: Alliance
    (A, "Stormwind City", 70.9, 72.5, "Stormwind", "Dungar Longdrink"),
    (A, "Ironforge", 55.5, 47.8, "Ironforge", "Gryth Thurden"),
    (A, "Westfall", 56.6, 52.6, "Sentinel Hill", "Thor"),
    (A, "Redridge Mountains", 25.6, 59.4, "Lakeshire", "Ariena Stormfeather"),
    (A, "Duskwood", 77.5, 44.3, "Darkshire", "Felicia Maline"),
    (A, "Loch Modan", 33.9, 50.9, "Thelsamar", "Thorgrum Borrelson"),
    (A, "Wetlands", 9.5, 59.7, "Menethil Harbor", "Shellei Brondir"),
    (A, "Hillsbrad Foothills", 49.3, 52.3, "Southshore", "Darla Harris"),
    (A, "Arathi Highlands", 45.8, 46.1, "Refuge Pointe", "Cedrik Prose"),
    (A, "The Hinterlands", 11.1, 46.2, "Aerie Peak", "Guthrum Thunderfist"),
    (A, "Western Plaguelands", 42.9, 85.1, "Chillwind Camp", "Bibilfaz Featherwhistle"),
    (A, "Eastern Plaguelands", 81.6, 59.3, "Light's Hope Chapel", "Khaelyn Steelwing"),
    (A, "Searing Gorge", 37.9, 30.8, "Thorium Point", "Lanie Reed"),
    (A, "Burning Steppes", 84.3, 68.3, "Morgan's Vigil", "Borgus Stoutarm"),
    (A, "Blasted Lands", 65.5, 24.3, "Nethergarde Keep", "Alexandra Constantine"),
    (A, "Stranglethorn Vale", 27.5, 77.8, "Booty Bay", "Gyll"),
    # Eastern Kingdoms: Horde
    (H, "Undercity", 63.3, 48.5, "Undercity", "Michael Garrett"),
    (H, "Silverpine Forest", 45.6, 42.6, "The Sepulcher", "Karos Razok"),
    (H, "Hillsbrad Foothills", 60.1, 18.6, "Tarren Mill", "Zarise"),
    (H, "Arathi Highlands", 73.1, 32.7, "Hammerfall", "Urda"),
    (H, "The Hinterlands", 81.7, 81.8, "Revantusk Village", "Gorkas"),
    (H, "Badlands", 4.0, 44.8, "Kargath", "Gorrik"),
    (H, "Searing Gorge", 34.8, 30.9, "Thorium Point", "Grisha"),
    (H, "Burning Steppes", 65.7, 24.2, "Flame Crest", "Vahgruk"),
    (H, "Swamp of Sorrows", 46.1, 54.8, "Stonard", "Breyk"),
    (H, "Stranglethorn Vale", 32.5, 29.4, "Grom'gol", "Thysta"),
    (H, "Stranglethorn Vale", 26.9, 77.1, "Booty Bay", "Gringer"),
    (H, "Eastern Plaguelands", 80.2, 57.0, "Light's Hope Chapel", "Georgia"),
    # Kalimdor: Alliance
    (A, "Teldrassil", 58.4, 94.0, "Rut'theran Village", "Vesprystus"),
    (A, "Darkshore", 36.3, 45.6, "Auberdine", "Caylais Moonfeather"),
    (A, "Ashenvale", 34.4, 48.0, "Astranaar", "Daelyshia"),
    (A, "Stonetalon Mountains", 36.4, 7.2, "Stonetalon Peak", "Teloren"),
    (A, "Desolace", 64.7, 10.5, "Nijel's Point", "Baritanas Skyriver"),
    (A, "Dustwallow Marsh", 67.5, 51.3, "Theramore", "Baldruc"),
    (A, "Feralas", 30.2, 43.2, "Feathermoon", "Fyldren Moonfeather"),
    (A, "Feralas", 89.5, 45.9, "Thalanaar", "Thyssiana"),
    (A, "Azshara", 11.9, 77.6, "Talrendis Point", "Jarrodenus"),
    (A, "Felwood", 62.5, 24.2, "Talonbranch Glade", "Mishellena"),
    (A, "Moonglade", 48.1, 67.3, "Nighthaven", "Sindrayl"),
    (A, "Winterspring", 62.3, 36.6, "Everlook", "Maethrya"),
    (A, "Tanaris", 51.0, 29.3, "Gadgetzan", "Bera Stonehammer"),
    (A, "Silithus", 50.6, 34.4, "Cenarion Hold", "Cloud Skydancer"),
    # Kalimdor: Horde
    (H, "Orgrimmar", 45.1, 63.9, "Orgrimmar", "Doras"),
    (H, "Thunder Bluff", 47.0, 49.8, "Thunder Bluff", "Tal"),
    (H, "The Barrens", 51.5, 30.3, "The Crossroads", "Devrak"),
    (H, "The Barrens", 44.4, 59.2, "Camp Taurajo", "Omusa Thunderhorn"),
    (H, "Stonetalon Mountains", 45.1, 59.8, "Sun Rock Retreat", "Tharm"),
    (H, "Ashenvale", 73.2, 61.6, "Splintertree Post", "Vhulgra"),
    (H, "Ashenvale", 12.2, 33.8, "Zoram'gar Outpost", "Andruk"),
    (H, "Desolace", 21.6, 74.1, "Shadowprey Village", "Thalon"),
    (H, "Thousand Needles", 45.1, 49.1, "Freewind Post", "Nyse"),
    (H, "Dustwallow Marsh", 35.6, 31.9, "Brackenwall Village", "Shardi"),
    (H, "Feralas", 75.4, 44.4, "Camp Mojache", "Shyn"),
    (H, "Felwood", 34.4, 54.0, "Bloodvenom Post", "Brakkar"),
    (H, "Azshara", 22.0, 49.6, "Valormok", "Kroum"),
    (H, "Moonglade", 32.1, 66.6, "Nighthaven", "Faustron"),
    (H, "Winterspring", 60.5, 36.3, "Everlook", "Yugrek"),
    (H, "Tanaris", 51.6, 25.4, "Gadgetzan", "Bulkrek Ragefist"),
    (H, "Silithus", 48.7, 36.7, "Cenarion Hold", "Runk Windtamer"),
    # both sides
    (N, "The Barrens", 63.1, 37.2, "Ratchet", "Bragok"),
    (N, "Un'Goro Crater", 45.2, 5.8, "Marshal's Refuge", "Gryfe"),
]
MASTERS = {p[5] for p in POINTS}


def world(point, bounds):
    """A point's spot in the world (north-south, west-east yards), or None while its zone's edges are unknown."""
    b = bounds.get(point[1])
    if not b or b[0] == b[1] or b[2] == b[3]:
        return None
    n, s, w, e = b
    return n - point[3] / 100 * (n - s), w - point[2] / 100 * (w - e)


def on_map(frame, bounds, continent, seen=None):
    """The flight points of the continent of `frame` (the zone the map is drawn in), in that zone's coords:
    [{"kind": "flight_a" | "flight_h" | "flight_n", "label", "who", "fx", "fy", "used"}]. `bounds` = zone ->
    (north, south, west, east); `continent` = zone -> its continent's name; `seen` = flight master -> the spots
    (north-south, west-east) recorded when you used them."""
    seen = seen or {}
    # your side: the side of the flight masters you have used (nobody says it otherwise); both-sides points count
    # for everyone once that is known
    sides = {p[0] for p in POINTS if p[5] in seen and p[0] != N}
    fb = bounds.get(frame)
    here = continent(frame)
    if not fb or not here or fb[0] == fb[1] or fb[2] == fb[3]:
        return []
    fn, fs, fw, fe = fb
    out = []
    for p in POINTS:
        if continent(p[1]) != here:
            continue
        at = world(p, bounds)
        if at is None:
            continue
        near = [s for s in seen.get(p[5], ()) if math.hypot(s[0] - at[0], s[1] - at[1]) <= NEAR]
        if near:
            at = min(near, key=lambda s: math.hypot(s[0] - at[0], s[1] - at[1]))
        out.append({"kind": "flight_" + p[0], "label": p[4], "who": p[5], "used": p[5] in seen,
                    "mine": bool(sides) and (p[0] == N or p[0] in sides),
                    "fx": round((fw - at[1]) / (fw - fe) * 100, 2), "fy": round((fn - at[0]) / (fn - fs) * 100, 2)})
    return out


def missing(bounds):
    """The zones whose flight points can't be drawn yet (edges unknown)."""
    return sorted({p[1] for p in POINTS if world(p, bounds) is None})
