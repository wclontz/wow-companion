"""Instance entrances for the maps: every dungeon and raid the game's own data gives an entrance for.

The spots are this game's (the dungeon list the companion already looks up, dungeons.json: "Entrance: Kalimdor,
world coordinates 4249.1, 748.4"), so they are exact here, unlike a list typed from memory. An instance whose
entry has no entrance coordinates is left out (`missing` names them).
"""
import re

_AT = re.compile(r"Entrance:\s*([A-Za-z' ]+?),\s*world coordinates\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)")


def name_of(look):
    return look.get("zone") or " ".join(w.capitalize() for w in (look.get("slug") or "").split("-"))


def spot(look):
    """-> (continent, north-south, west-east) of an instance's entrance, or None when its entry has none."""
    m = _AT.search((look or {}).get("entrance") or "")
    return (m.group(1).strip(), float(m.group(2)), float(m.group(3))) if m else None


def on_map(frame, bounds, continent, looks):
    """The entrances of the continent of `frame` (the zone the map is drawn in), in that zone's coords:
    [{"kind": "dungeon" | "raid", "label": "Shadowfang Keep (18)", "fx", "fy", "used": True}].
    `looks` = the dungeon list's entries (instance.DungeonInfo.all())."""
    fb = bounds.get(frame)
    here = continent(frame)
    if not fb or not here or fb[0] == fb[1] or fb[2] == fb[3]:
        return []
    fn, fs, fw, fe = fb
    out = []
    for look in looks or ():
        at = spot(look)
        if not at or at[0] != here:
            continue
        lvl = re.match(r"\d+", str(look.get("level") or ""))
        out.append({"kind": "raid" if look.get("kind") == "raids" else "dungeon", "used": True,
                    "label": name_of(look) + (" (" + lvl.group() + ")" if lvl else ""),
                    "fx": round((fw - at[2]) / (fw - fe) * 100, 2), "fy": round((fn - at[1]) / (fn - fs) * 100, 2)})
    return out


def missing(looks):
    """The instances that can't be drawn: no entrance coordinates in the game's data."""
    return sorted(name_of(x) for x in looks or () if not spot(x))
