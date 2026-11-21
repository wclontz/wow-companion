"""The position add-on's strip (addon/CompanionBeacon): rows of 47 squares of 4x4 screen pixels in the top-left
corner of the game window, 6 bits a square (red, green, blue each at one of 4 levels). Row 1: your exact
world position, continent, zone map and its rectangle, facing and minimap zoom. Row 2: your numbers (level,
XP, health, power, combat / group / target, money, bags, gear wear). Rows 3-6: texts, a page at a time (the
name above the minimap, the zone, the target; the party; the quest log). This reads them back. Nothing is guessed: no strip, a wrong mark or a wrong checksum
-> None; each row has its own checksum, and a bad row after the first is just left out.
Keep the layout in step with CompanionBeacon.lua.

read() is one look at the screen. start() runs a reader 10 times a second (the names come a page at a time,
so somebody has to keep looking); latest() is its newest reading, with "stats" and the finished "texts"."""
import math
import re
import threading
import time

import numpy as np

CELL, CELLS, ROWS, VERSION = 4, 47, 6, 7
BARS = 3                                                    # the bars above the strip (version 7)
PAGE = 20
TEXT_ROWS = (3, 4, 5, 6)


def text_name(chan):
    """What a text number is called in "texts": label, zone, target, party1-4, gear, q1-q40, gear1-4."""
    if chan in (1, 2, 3):
        return ("label", "zone", "target")[chan - 1]
    if 4 <= chan <= 7:
        return f"party{chan - 3}"
    if chan == 8:
        return "gear"
    if chan == 9:
        return "minimap"
    if chan == 10:
        return "name"
    if 51 <= chan <= 54:
        return f"gear{chan - 50}"
    if 11 <= chan <= 50:
        return f"q{chan - 10}"
    return None
CONTINENTS = {0: "Eastern Kingdoms", 1: "Kalimdor"}
# zone map ids (Classic). The log's MAP_CHANGE lines add to this (quadmap.note_map_id): a server's own zones.
MAP_IDS = {
    1411: "Durotar", 1412: "Mulgore", 1413: "The Barrens", 1416: "Alterac Mountains", 1417: "Arathi Highlands",
    1418: "Badlands", 1419: "Blasted Lands", 1420: "Tirisfal Glades", 1421: "Silverpine Forest",
    1422: "Western Plaguelands", 1423: "Eastern Plaguelands", 1424: "Hillsbrad Foothills", 1425: "The Hinterlands",
    1426: "Dun Morogh", 1427: "Searing Gorge", 1428: "Burning Steppes", 1429: "Elwynn Forest",
    1430: "Deadwind Pass", 1431: "Duskwood", 1432: "Loch Modan", 1433: "Redridge Mountains",
    1434: "Stranglethorn Vale", 1435: "Swamp of Sorrows", 1436: "Westfall", 1437: "Wetlands", 1438: "Teldrassil",
    1439: "Darkshore", 1440: "Ashenvale", 1441: "Thousand Needles", 1442: "Stonetalon Mountains",
    1443: "Desolace", 1444: "Feralas", 1445: "Dustwallow Marsh", 1446: "Tanaris", 1447: "Azshara",
    1448: "Felwood", 1449: "Un'Goro Crater", 1450: "Moonglade", 1451: "Silithus", 1452: "Winterspring",
    1453: "Stormwind City", 1454: "Orgrimmar", 1455: "Ironforge", 1456: "Thunder Bluff", 1457: "Darnassus",
    1458: "Undercity",
}


def _yards(v):
    return v / 50.0 - 100000.0


def encode(d):
    """The squares' 6-bit values for a reading (what the add-on draws): for tests."""
    v = [0] * (CELLS + 1)                         # 1-based, like the add-on
    v[1:8] = [63, 0, 21, 42, 48, 12, 3]

    def put(i, n, x):
        for k in range(n - 1, -1, -1):
            v[i + (n - 1 - k)] = (int(x) >> (6 * k)) & 63

    def y24(c):
        return max(0, min(16777215, int(math.floor((c + 100000) * 50 + 0.5))))
    pos, rect, mp = d.get("pos"), d.get("rect"), d.get("map_pos")
    flags = (1 if pos else 0) | (2 if d.get("indoors") else 0) | (4 if d.get("taxi") else 0) | \
            (8 if d.get("instance") else 0) | (16 if d.get("dead") else 0) | (32 if rect else 0)
    v[8], v[9], v[10] = VERSION, d.get("seq", 0) % 64, flags
    v[11] = (d.get("zoom", 0) & 7) | (8 if mp else 0) | (16 if d.get("mounted") else 0)
    put(12, 2, d.get("inst", 0))
    put(14, 4, y24(pos[0]) if pos else 0)
    put(18, 4, y24(pos[1]) if pos else 0)
    put(22, 2, int(d.get("facing", 0) / (2 * math.pi) * 4096) % 4096)
    put(24, 2, d.get("map", 0))
    put(26, 2, round(mp[0] * 4000) if mp else 0)
    put(28, 2, round(mp[1] * 4000) if mp else 0)
    for k in range(4):
        put(30 + 4 * k, 4, y24(rect[k]) if rect else 0)
    put(46, 2, sum(v[i] * (i - 7) for i in range(8, 46)) % 4096)
    return v[1:]


def _seal(row, cells):
    """A row's checksum (rows 2 and up) over its first 45 values."""
    return (1000 * row + sum(cells[k - 1] * k for k in range(1, 46))) % 4096


def encode_stats(st):
    """Row 2's squares for {"level", "xp", "xpmax", "rested", "hp", "hpmax", "power", "powermax", "ptype",
    "combat", "resting", "group", "target_level", "target_dead", "target_hp" (percent), "target_reaction",
    "target_player", "target_attackable", "target_tapped", "money", "bag_free", "bag_all", "wear",
    "quests", "party"}: for tests. No "hpmax" / "powermax" / "xpmax" / "money" / "bag_all" / "wear" = that
    value is secret or missing (not known)."""
    v = [0] * CELLS

    def put(i, n, x):
        for k in range(n):
            v[i - 1 + k] = (int(x) >> (6 * (n - 1 - k))) & 63
    tl = st.get("target_level")
    flags = (1 if st.get("combat") else 0) | (2 if st.get("resting") else 0) | (4 if st.get("rested") is not None else 0) \
        | (8 if tl is not None else 0) | (16 if st.get("target_dead") else 0) | (32 if st.get("group") else 0)
    v[0] = st.get("level", 0)
    put(2, 4, st.get("xp", 0)), put(6, 4, st.get("xpmax", 0)), put(10, 4, st.get("rested") or 0)
    put(14, 3, st.get("hp", 0)), put(17, 3, st.get("hpmax", 0))
    put(20, 3, st.get("power", 0)), put(23, 3, st.get("powermax", 0))
    v[25], v[26], v[27] = st.get("ptype", 0), flags, st.get("group", 0)
    v[28] = 0 if tl is None else (63 if tl < 0 else tl)
    v[29] = (1 if st.get("xpmax") else 0) | (2 if st.get("hpmax") else 0) | (4 if st.get("powermax") is not None else 0) \
        | (8 if st.get("money") is not None else 0) | (16 if st.get("bag_all") else 0) | (32 if st.get("wear") is not None else 0)
    put(31, 5, st.get("money") or 0)
    v[35] = min(63, st.get("bag_free") or 0)
    put(37, 2, st.get("bag_all") or 0), put(39, 2, st.get("wear") or 0)
    v[40], v[41] = st.get("quests", 0), st.get("party", 0)
    if tl is not None:
        if st.get("target_hp") is not None:
            put(43, 2, round(st["target_hp"] * 10) + 1)
        v[44] = {"hostile": 1, "neutral": 2, "friendly": 3}.get(st.get("target_reaction"), 0) \
            | (4 if st.get("target_player") else 0) | (8 if st.get("target_attackable") else 0) \
            | (16 if st.get("target_tapped") else 0)
    put(46, 2, _seal(2, v))
    return v


def encode_page(chan, ident, text, page, row=3):
    """A text row's squares for one page of a text (text: bytes): for tests."""
    v = [0] * CELLS
    v[0], v[1], v[2], v[3], v[44] = chan, ident % 64, len(text) % 64, page, len(text) // 64
    for k in range(PAGE):
        o = page * PAGE + k
        b = text[o] if o < len(text) else 0
        v[4 + 2 * k], v[5 + 2 * k] = b >> 6, b & 63
    s = _seal(row, v)
    v[45], v[46] = s >> 6, s & 63
    return v


def _stats(c):
    """Row 2's values -> your numbers, or None (wrong checksum / nothing drawn yet)."""
    def get(i, n):
        x = 0
        for k in range(n):
            x = x * 64 + c[i - 1 + k]
        return x
    if get(46, 2) != _seal(2, c) or not c[0]:
        return None
    flags, known = c[26], c[29]
    # (known: the game keeps some values secret from add-ons - WoW Forever: health and power. Those are None)
    return {"level": c[0], "xp": get(2, 4) if known & 1 else None, "xpmax": get(6, 4) if known & 1 else None,
            "rested": get(10, 4) if flags & 4 else None,
            "hp": get(14, 3) if known & 2 else None, "hpmax": get(17, 3) if known & 2 else None,
            "power": get(20, 3) if known & 4 else None, "powermax": get(23, 3) if known & 4 else None,
            "ptype": c[25] if known & 4 else None,
            "combat": bool(flags & 1), "resting": bool(flags & 2), "group": c[27] if flags & 32 else 0,
            "target": bool(flags & 8), "target_dead": bool(flags & 16),
            "target_level": (None if not flags & 8 or not c[28] else (-1 if c[28] == 63 else c[28])),
            # (version 6; an older strip draws 0 here: not known)
            "target_hp": (get(43, 2) - 1) / 10 if flags & 8 and 0 < get(43, 2) <= 1001 else None,     # percent
            "target_reaction": {1: "hostile", 2: "neutral", 3: "friendly"}.get(c[44] & 3) if flags & 8 else None,
            "target_player": bool(flags & 8 and c[44] & 4), "target_attackable": bool(flags & 8 and c[44] & 8),
            "target_tapped": bool(flags & 8 and c[44] & 16),
            "money": get(31, 5) if known & 8 else None,                      # copper
            "bag_free": c[35] if known & 16 else None, "bag_all": get(37, 2) if known & 16 else None,
            "wear": get(39, 2) / 10 if known & 32 else None,                 # most worn piece, percent left
            "quests": c[40], "party": c[41]}


def _page(c, row):
    """A text row's values -> one page {"chan", "id", "len", "page", "bytes"}, or None."""
    n = c[44] * 64 + c[2]
    if c[45] * 64 + c[46] != _seal(row, c) or not text_name(c[0]) or c[3] * PAGE > n:
        return None
    raw = [c[4 + 2 * k] * 64 + c[5 + 2 * k] for k in range(PAGE)]
    if max(raw) > 255:
        return None
    return {"chan": c[0], "id": c[1], "len": n, "page": c[3], "bytes": bytes(raw[:max(0, n - c[3] * PAGE)])}


def decode(img):
    """A grab of the window's top-left corner (at least CELLS*CELL x CELL pixels) -> the reading, or None."""
    a = np.asarray(img.convert("RGB"), dtype=np.float32)
    if a.shape[0] < CELL or a.shape[1] < CELLS * CELL:
        return None
    def row_px(r):
        y = r * CELL
        return np.stack([a[y + 1:y + 3, i * CELL + 1:i * CELL + 3].reshape(-1, 3).mean(0) for i in range(CELLS)])
    px = row_px(0)
    white, black, g1, g2 = px[0], px[1], px[2], px[3]
    lev = np.stack([black, g1, g2, white])                 # the 4 levels as this screen shows them, per colour
    if (np.diff(lev, axis=0) < 25).any():
        return None                                         # no strip here (or washed out)

    def values(p):
        q = np.abs(p[:, None, :] - lev[None, :, :]).argmin(1)   # each square's level 0-3 per colour
        return (q[:, 0] * 16 + q[:, 1] * 4 + q[:, 2]).tolist()
    v = values(px)
    if v[:7] != [63, 0, 21, 42, 48, 12, 3]:
        return None
    v = [0] + v                                             # 1-based, like the add-on

    def get(i, n):
        x = 0
        for k in range(n):
            x = x * 64 + v[i + k]
        return x
    if get(46, 2) != sum(v[i] * (i - 7) for i in range(8, 46)) % 4096 or v[8] not in (1, 2, 3, 4, 5, 6, VERSION):
        return None
    flags, z = v[10], v[11]
    d = {"seq": v[9], "indoors": bool(flags & 2), "taxi": bool(flags & 4), "instance": bool(flags & 8),
         "dead": bool(flags & 16), "zoom": z & 7, "mounted": bool(z & 16), "inst": get(12, 2),
         "facing": get(22, 2) / 4096 * 2 * math.pi, "map": get(24, 2),
         "pos": (_yards(get(14, 4)), _yards(get(18, 4))) if flags & 1 else None,     # (north-south, west-east)
         "map_pos": (get(26, 2) / 4000, get(28, 2) / 4000) if z & 8 else None,
         "rect": [_yards(get(30 + 4 * k, 4)) for k in range(4)] if flags & 32 else None}   # north, south, west, east
    # which of the two position numbers runs north-south: checked against the zone map's own rectangle
    # (the rectangle is as the log's MAP_CHANGE gives it; swapped if the game hands them over the other way)
    if d["pos"] and d["rect"] and d["map_pos"]:
        N, S, W, E = d["rect"]
        mx, my = d["map_pos"]

        def off(p):
            return abs((N - p[0]) / (N - S) - my) + abs((W - p[1]) / (W - E) - mx) if N != S and W != E else 9
        p = d["pos"]
        if off(p) > 0.02 and off((p[1], p[0])) <= 0.02:
            d["pos"] = (p[1], p[0])
            d["swapped"] = True
        d["fits"] = min(off(p), off((p[1], p[0]))) <= 0.02
    d["continent"] = CONTINENTS.get(d["inst"]) if not d["instance"] else None
    d["version"] = v[8]
    if v[8] >= 3 and a.shape[0] >= ROWS * CELL:             # the other rows: each right, or left out
        d["stats"] = _stats(values(row_px(1)))               # (a version 2 strip had other rows: row 1 only)
        d["pages"] = [pg for pg in (_page(values(row_px(r - 1)), r) for r in TEXT_ROWS) if pg]
    return d


def read_bars(img):
    """The picture of the BARS bars above the strip (the strip's width, BARS * CELL high; bar 1 is the lowest) ->
    how full each is, 0-1, or None for a bar that can't be trusted: blue (the game refused the add-on), covered by
    something, or not one lit stretch from the left."""
    a = np.asarray(img.convert("RGB"), dtype=np.float32)
    if a.shape[0] < BARS * CELL or a.shape[1] < CELLS * CELL:
        return [None] * BARS
    out = []
    for i in range(BARS):
        y = (BARS - 1 - i) * CELL
        px = a[y + 1:y + 3, :CELLS * CELL].mean(0)          # one colour per column, off the bar's middle rows
        r, g, b = px[:, 0], px[:, 1], px[:, 2]
        lit = (g > 128) & (r < 110) & (b < 110)
        dark = (r < 70) & (g < 70) & (b < 70)
        n = int(lit.sum())
        if (lit | dark).mean() < 0.98 or not lit[:n].all():
            out.append(None)
        else:
            out.append(round(n / lit.size, 4))
    return out


def read():
    """The strip off the screen right now -> the reading (with "t"), or None. With the background reader
    running, its look of the last half second is the answer (no second grab)."""
    r = READER
    if r is not None and r.is_alive() and time.time() - r.cur_t < 0.5:
        return r.cur
    return _look()


def _look():
    import fastgrab
    from screenread import find_window
    _, rect = find_window("World of Warcraft")
    if not rect:
        return None
    # the strip sits in the window's bottom-right corner (since 2026-11-19); an add-on from before that drew
    # it top-left, so that corner is looked at when the first has nothing
    w, h, bh = CELLS * CELL, ROWS * CELL, BARS * CELL
    img = fastgrab.grab((rect[2] - w, rect[3] - h - bh, rect[2], rect[3]))      # the strip with its bars above it
    d = decode(img.crop((0, bh, w, bh + h))) if img is not None else None
    if d:
        if d.get("version", 1) >= 7 and d.get("stats"):
            hp, power, target = read_bars(img.crop((0, 0, w, bh)))
            d["stats"].update(hp_frac=hp, power_frac=power, target_frac=target if d["stats"]["target"] else None)
        d["t"] = time.time()
        return d
    img = fastgrab.grab((rect[0], rect[1], rect[0] + w, rect[1] + h))           # (an add-on from before 2026-11-19)
    d = decode(img) if img is not None else None
    if d:
        d["t"] = time.time()
    return d


class Texts:
    """Puts the pages of the text rows back together: {"label" / "zone" / "target" / "party1" / "q1"...: the text}."""

    def __init__(self):
        self.parts, self.done = {}, {}

    def add(self, pg):
        if not pg:
            return
        key = (pg["id"], pg["len"])
        cur = self.parts.get(pg["chan"])
        if not cur or cur[0] != key:                        # the text changed: start it again
            cur = self.parts[pg["chan"]] = (key, {})
        cur[1][pg["page"]] = pg["bytes"]
        if all(k in cur[1] for k in range(max(1, -(-pg["len"] // PAGE)))):
            raw = b"".join(cur[1][k] for k in sorted(cur[1]))[:pg["len"]]
            self.done[text_name(pg["chan"])] = raw.decode("utf-8", "ignore")   # (ignore: cut mid-letter at the limit)

    def clear(self):
        self.parts, self.done = {}, {}


def party(d):
    """The reading's party members [{"name", "level", "cls"}] (not you), or None until all of them have arrived."""
    st, tx = (d or {}).get("stats"), (d or {}).get("texts") or {}
    if not st:
        return None
    out = []
    for i in range(1, 5):
        t = tx.get(f"party{i}")
        if t is None:
            return None
        if t:
            f = (t.split("\t") + ["", ""])[:3]
            out.append({"slot": i, "name": f[0], "level": int(f[1]) if f[1].isdigit() and int(f[1]) > 0 else None,
                        "cls": f[2].title() or None})
    return out if len(out) == st["party"] else None


GEAR_SLOTS = ("head", "neck", "shoulder", "chest", "waist", "legs", "feet", "wrist", "hands", "ring 1", "ring 2",
              "trinket 1", "trinket 2", "back", "main hand", "off hand", "ranged")
# what an item's colour is worth next to a green of the same item level (our own scale, not the GearScore add-on's)
QUALITY_WEIGHT = {0: 0.5, 1: 0.75, 2: 1.0, 3: 1.15, 4: 1.3, 5: 1.45}
QUALITY_NAME = {0: "poor", 1: "common", 2: "uncommon", 3: "rare", 4: "epic", 5: "legendary"}


def gear_score(text):
    """A gear text off the strip ("25.2 0 22.3t ...": item level . quality per slot, 0 = empty, ? = not known
    yet) -> {"avg": average item level of what is worn, "score", "worn", "empty": [slot names], "whole",
    "items": [{"slot", "ilvl", "quality"}]}, or None for no text.
    score = the sum over the 17 slots of item level x the quality's weight: an empty slot adds nothing, and a
    two-hander with nothing in the off hand counts twice (it fills both hands)."""
    if not text:
        return None
    parts = text.split()
    if len(parts) != len(GEAR_SLOTS):
        return None
    items, empty, whole, score = [], [], True, 0.0
    for name, e in zip(GEAR_SLOTS, parts):
        if e == "?":
            whole = False
            continue
        m = re.fullmatch(r"(\d+)\.(\d)(t?)", e)
        if not m or int(m.group(1)) == 0:
            empty.append(name)
            continue
        it = {"slot": name, "ilvl": int(m.group(1)), "quality": int(m.group(2)), "two_hand": bool(m.group(3))}
        items.append(it)
    by = {it["slot"]: it for it in items}
    for it in items:
        twice = it["slot"] == "main hand" and it["two_hand"] and "off hand" not in by
        score += it["ilvl"] * QUALITY_WEIGHT.get(it["quality"], 1.0) * (2 if twice else 1)
    mh = by.get("main hand")
    if mh and mh["two_hand"] and "off hand" in empty:
        empty.remove("off hand")                           # nothing is missing there
    return {"avg": round(sum(it["ilvl"] for it in items) / len(items), 1) if items else 0, "score": round(score),
            "worn": len(items), "empty": empty, "whole": whole, "items": items}


def gear(d):
    """The reading's gear scores: {"me": gear_score, "party": [{"slot", "name", + gear_score or "score": None}],
    "team": {"score", "avg", "of": how many are in it, "missing": [names not inspected yet]}} or None."""
    tx = (d or {}).get("texts") or {}
    me = gear_score(tx.get("gear"))
    if not me:
        return None
    out = {"me": me, "party": [], "team": None}
    for m in party(d) or []:
        g = gear_score(tx.get(f"gear{m['slot']}"))
        out["party"].append(dict(g or {"score": None, "avg": None}, slot=m["slot"], name=m["name"]))
    have = [me] + [g for g in out["party"] if g["score"] is not None]
    out["team"] = {"score": round(sum(g["score"] for g in have) / len(have)),
                   "avg": round(sum(g["avg"] for g in have) / len(have), 1), "of": len(have),
                   "missing": [g["name"] for g in out["party"] if g["score"] is None]}
    return out


def minimap(d):
    """Where the minimap is on this character's screen, from the add-on (text 9, strip version 4):
    (centre x, centre y, radius) in the 1920x1080 terms screen_layout.json uses, or None."""
    tx = (d or {}).get("texts") or {}
    try:
        x, y, r, w, h = (float(v) for v in (tx.get("minimap") or "").split())
    except ValueError:
        return None
    if w < 320 or h < 240 or not (0 <= x <= w and 0 <= y <= h) or not (20 <= r * 1080 / h <= 500):
        return None
    return (round(x * 1920 / w, 1), round(y * 1080 / h, 1), round(r * 1080 / h, 1))


def quests(d):
    """The reading's quest log [{"level", "title", "details": [...], "done", "failed"}] in the log's order, or
    None until every quest has arrived. An objective reads as the tracker shows it: "3/8 Thing slain"."""
    st, tx = (d or {}).get("stats"), (d or {}).get("texts") or {}
    if not st:
        return None
    out = []
    for i in range(1, st["quests"] + 1):
        t = tx.get(f"q{i}")
        if not t:
            return None
        f = t.split("\t")
        if len(f) < 3:
            return None
        flags = int(f[1]) if f[1].isdigit() else 0
        details, fin = [], []
        for o in f[3:]:
            fin.append(o[:1] == "+")
            txt = o[1:].strip()
            m = _OBJ.match(txt)
            details.append(f"{m.group(2)}/{m.group(3)} {m.group(1).strip()}" if m else txt)
        out.append({"level": int(f[0]) if f[0].isdigit() and int(f[0]) > 0 else None, "title": f[2], "details": details,
                    "done": bool(flags & 1) or (bool(fin) and all(fin)), "failed": bool(flags & 2)})
    return out


_OBJ = re.compile(r"^(.*?):\s*(\d+)\s*/\s*(\d+)$")              # "Thing slain: 3/8"


class Reader(threading.Thread):
    """Looks at the strip `hz` times a second. `cur` = this look's reading (None: no strip), `last` = the
    newest good one, both with "texts"."""

    def __init__(self, hz=10):
        super().__init__(daemon=True)
        self.hz, self.cur, self.last, self.cur_t, self.error = hz, None, None, 0.0, None
        self.texts = Texts()

    def run(self):
        while True:
            t0 = time.time()
            try:
                d = _look()
                if d:
                    for pg in d.get("pages") or ():
                        self.texts.add(pg)
                    d["texts"] = dict(self.texts.done)
                    self.last = d
                elif self.last and t0 - self.last["t"] > 3:
                    self.texts.clear()                      # strip gone (loading screen, add-on off): names are stale
                self.cur, self.cur_t, self.error = d, t0, None
            except Exception as e:
                self.cur, self.cur_t, self.error = None, t0, repr(e)
            time.sleep(max(0.02, 1 / self.hz - (time.time() - t0)))


READER = None


def start(hz=10):
    """Start the background reader (once)."""
    global READER
    if READER is None:
        READER = Reader(hz)
        READER.start()
    return READER


def latest(max_age=1.5):
    """The background reader's newest reading if it is fresh, else None."""
    r = READER
    d = r.last if r else None
    return d if d and time.time() - d["t"] <= max_age else None


def zone_name(d):
    """The zone the reading's map id names, or None."""
    try:
        import quadmap
        return quadmap.map_ids().get(str(d["map"])) or MAP_IDS.get(d["map"])
    except Exception:
        return MAP_IDS.get(d["map"])


if __name__ == "__main__":
    r = read()
    if r and r.get("version", 1) >= 3:                      # texts come a page at a time: watch until the quest
        start()                                             # log is whole (15 s at most)
        t_end = time.time() + 15
        while time.time() < t_end and (quests(latest()) is None or party(latest()) is None):
            time.sleep(0.25)
        r = latest() or r
        r["party_list"], r["quest_list"], r["gear_scores"] = party(r), quests(r), gear(r)
        r["minimap_spot"] = minimap(r)
        r.pop("pages", None)
    if not r:
        print("no strip on screen (is the add-on on, and the game window in front?)")
    else:
        for k, v in r.items():
            if k == "quest_list" and v:
                print("quest_list:")
                for q in v:
                    print("   ", q)
            else:
                print(f"{k}: {v}")
