"""Instance entrances for the maps: every dungeon and raid the game's own data gives an entrance for.

The spots are this game's (the dungeon list the companion already looks up, dungeons.json: "Entrance: Kalimdor,
world coordinates 4249.1, 748.4"), so they are exact here, unlike a list typed from memory. An instance whose
entry has no entrance coordinates is drawn where you walked in, once you have (`Watch`, kept in places.json as
kind "entrance"); until then it is left out (`missing` names them).
"""
import re

GAP_IN = 180.0          # s: an outdoor reading older than this says nothing about where you walked in

_AT = re.compile(r"Entrance:\s*([A-Za-z' ]+?),\s*world coordinates\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)")


def name_of(look):
    return look.get("zone") or " ".join(w.capitalize() for w in (look.get("slug") or "").split("-"))


def spot(look):
    """-> (continent, north-south, west-east) of an instance's entrance, or None when its entry has none."""
    m = _AT.search((look or {}).get("entrance") or "")
    return (m.group(1).strip(), float(m.group(2)), float(m.group(3))) if m else None


class Watch:
    """Fed the add-on's readings one after another; says where you stood just before you went into an instance.
    That is its entrance when you walked in. It can't tell a walk from being sent there from far away (a
    summon lands you outside, which is fine; a teleport straight in is not), so `best` goes by the spot seen most."""

    def __init__(self):
        self.out, self.inside = None, False

    def feed(self, inst, continent, pos, t, taxi=False, instance=False):
        """-> (instance id, continent, north-south, west-east) at the first reading inside, else None.
        `pos` = (north-south, west-east) or None; `continent` = the one the position is on."""
        if not instance:
            self.inside = False
            if continent and pos and not taxi:
                self.out = (continent, pos, t)
            return None
        if self.inside:
            return None
        self.inside = True
        out, self.out = self.out, None
        if not out or inst is None or t - out[2] > GAP_IN or t < out[2]:
            return None
        return inst, out[0], out[1][0], out[1][1]


def best(rows):
    """Of the spots kept for one instance [(north-south, west-east, times seen, when last)] the one to draw:
    seen most often, the newest of equals. None for none."""
    return max(rows, key=lambda r: (r[2], r[3]))[:2] if rows else None


def on_map(frame, bounds, continent, looks, learned=None):
    """The entrances of the continent of `frame` (the zone the map is drawn in), in that zone's coords:
    [{"kind": "dungeon" | "raid", "label": "Shadowfang Keep (18)", "fx", "fy", "used": True}].
    `looks` = the dungeon list's entries (instance.DungeonInfo.all()). `learned` = {instance name: [(north-south,
    west-east, times seen, when last)]} of this continent, from your own walks in: drawn (with "learned": True)
    for an instance the game's data gives no entrance for."""
    fb = bounds.get(frame)
    here = continent(frame)
    if not fb or not here or fb[0] == fb[1] or fb[2] == fb[3]:
        return []
    fn, fs, fw, fe = fb
    out = []
    for look in looks or ():
        at, own = spot(look), None
        if not at:
            own = best((learned or {}).get(name_of(look)) or [])
            at = own and (here, own[0], own[1])
        if not at or at[0] != here:
            continue
        lvl = re.match(r"\d+", str(look.get("level") or ""))
        e = {"kind": "raid" if look.get("kind") == "raids" else "dungeon", "used": True,
             "label": name_of(look) + (" (" + lvl.group() + ")" if lvl else ""),
             "fx": round((fw - at[2]) / (fw - fe) * 100, 2), "fy": round((fn - at[1]) / (fn - fs) * 100, 2)}
        if own:
            e["learned"] = True
        out.append(e)
    return out


def missing(looks):
    """The instances that can't be drawn: no entrance coordinates in the game's data."""
    return sorted(name_of(x) for x in looks or () if not spot(x))
