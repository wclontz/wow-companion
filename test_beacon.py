"""The position strip: what the add-on draws (beacon.encode mirrors CompanionBeacon.lua) read back from a
picture, also through a brighter / darker / noisy screen; and a picture without a strip gives nothing."""
import math
import random

import numpy as np
from PIL import Image

import beacon


def draw(vals, gamma=1.0, lo=0, hi=255, noise=0, w=400, h=12):
    a = np.full((h, w, 3), 37, dtype=np.float32)
    for i, v in enumerate(vals):
        col = [((v >> 4) & 3) / 3, ((v >> 2) & 3) / 3, (v & 3) / 3]
        a[0:beacon.CELL, i * beacon.CELL:(i + 1) * beacon.CELL] = [lo + (hi - lo) * c ** gamma for c in col]
    if noise:
        a += np.random.default_rng(1).normal(0, noise, a.shape)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


ok = True
rnd = random.Random(3)
for n in range(300):
    N, W = rnd.uniform(-12000, 12000), rnd.uniform(-9000, 9000)
    S, E = N - rnd.uniform(500, 7000), W - rnd.uniform(700, 10000)
    mx, my = round(rnd.random() * 4000) / 4000, round(rnd.random() * 4000) / 4000
    d = {"seq": n, "inst": rnd.choice([0, 1]), "pos": (N - my * (N - S), W - mx * (W - E)), "map": rnd.randint(1411, 1458),
         "map_pos": (mx, my), "rect": [N, S, W, E], "facing": rnd.uniform(0, 2 * math.pi), "zoom": rnd.randint(0, 5),
         "indoors": rnd.random() < .3, "taxi": rnd.random() < .2, "mounted": rnd.random() < .3}
    for kw in ({}, {"gamma": 1.25}, {"gamma": 0.8}, {"lo": 12, "hi": 235}, {"noise": 5}):
        r = beacon.decode(draw(beacon.encode(d), **kw))
        good = (r and r["fits"] and not r.get("swapped") and abs(r["pos"][0] - d["pos"][0]) <= 0.011 and abs(r["pos"][1] - d["pos"][1]) <= 0.011
                and r["map"] == d["map"] and r["zoom"] == d["zoom"] and r["indoors"] == d["indoors"] and r["taxi"] == d["taxi"]
                and r["mounted"] == d["mounted"] and all(abs(a - b) <= 0.011 for a, b in zip(r["rect"], d["rect"]))
                and abs(r["facing"] - d["facing"]) < 0.002 and r["continent"] == beacon.CONTINENTS[d["inst"]])
        if not good:
            ok = False
            print("FAIL", n, kw, d, r)
            break
# the game handing the two numbers over the other way round is noticed and put right
d = {"pos": (-950.0, -4900.0), "map": 1455, "map_pos": (0.3, 0.626), "rect": [-4569.24, -5096.85, -713.59, -1504.22]}
d["pos"] = (-713.59 - 0.3 * (-713.59 + 1504.22), -4569.24 - 0.626 * (-4569.24 + 5096.85))
r = beacon.decode(draw(beacon.encode(d)))
if not (r and r.get("swapped") and abs(r["pos"][0] + 4899.5) < 1):
    ok = False
    print("FAIL swapped", r)
# in an instance: no position, no continent
r = beacon.decode(draw(beacon.encode({"instance": True, "inst": 36})))
if not (r and r["pos"] is None and r["continent"] is None and r["instance"]):
    ok = False
    print("FAIL instance", r)
# no strip / a damaged strip -> nothing
v = beacon.encode({"pos": (1.0, 2.0)})
v[20] ^= 5
for im in (draw([]), draw(v), Image.new("RGB", (400, 12), (255, 255, 255)), Image.new("RGB", (100, 3))):
    if beacon.decode(im) is not None:
        ok = False
        print("FAIL read something that isn't there")
# ---- rows 2-6: your numbers, and texts a page at a time


def draw_rows(rows, **kw):
    """Several rows of squares, one under the other (draw() does one row)."""
    ims = [np.asarray(draw(r, h=beacon.CELL, **kw)) for r in rows]
    return Image.fromarray(np.vstack(ims))


EMPTY = [0] * beacon.CELLS


def strip(stats, pages=()):
    """A whole 6-row strip: row 1, the numbers, then a page on each text row (pages: {row: squares})."""
    pages = dict(pages)
    return [row1, beacon.encode_stats(stats)] + [pages.get(r, EMPTY) for r in beacon.TEXT_ROWS]


row1 = beacon.encode({"instance": True, "inst": 33})
for n in range(200):
    st = {"level": rnd.randint(1, 60), "xp": rnd.randint(0, 200000), "xpmax": rnd.randint(400, 220000),
          "rested": rnd.choice([None, 0, rnd.randint(1, 300000)]), "hp": rnd.randint(0, 9000), "hpmax": rnd.randint(1, 9000),
          "power": rnd.randint(0, 9000), "powermax": rnd.randint(0, 9000), "ptype": rnd.choice([0, 1, 3]),
          "combat": rnd.random() < .5, "resting": rnd.random() < .5, "group": rnd.choice([0, 2, 5]),
          "target_level": rnd.choice([None, -1, rnd.randint(1, 62)]), "target_dead": False,
          "target_hp": rnd.choice([None, 0.0, 100.0, rnd.randint(0, 1000) / 10]),
          "target_reaction": rnd.choice([None, "hostile", "neutral", "friendly"]),
          "target_player": rnd.random() < .3, "target_attackable": rnd.random() < .5, "target_tapped": rnd.random() < .2,
          "money": rnd.randint(0, 900000000), "bag_free": rnd.randint(0, 63), "bag_all": rnd.randint(16, 120),
          "wear": rnd.randint(0, 1000), "quests": rnd.randint(0, 25), "party": rnd.randint(0, 4)}
    for kw in ({}, {"gamma": 1.25}, {"lo": 12, "hi": 235}, {"noise": 5}):
        r = beacon.decode(draw_rows(strip(st, {3: beacon.encode_page(1, n, b"Shadowfang Keep", 0, 3),
                                               6: beacon.encode_page(12, n, b"21\t0\tA quest", 0, 6)}), **kw))
        s = r and r.get("stats")
        want = dict(st, wear=st["wear"] / 10)
        if st["target_level"] is None:                    # no target: nothing about one is read
            want.update(target_hp=None, target_reaction=None, target_player=False, target_attackable=False, target_tapped=False)
        if not (s and all(s[k] == want[k] for k in want) and s["target"] == (st["target_level"] is not None)
                and [(g["chan"], g["bytes"]) for g in r["pages"]] == [(1, b"Shadowfang Keep"), (12, b"21\t0\tA quest")]
                and r["instance"] and r["inst"] == 33):
            ok = False
            print("FAIL stats", n, kw, st, r)
            break
# values the game keeps secret from add-ons (WoW Forever: health and power) come back as None, the rest still read
r = beacon.decode(draw_rows(strip({"level": 21, "xp": 9025, "xpmax": 25200})))
s = r and r.get("stats")
if not (s and s["level"] == 21 and (s["xp"], s["xpmax"]) == (9025, 25200) and s["hp"] is None and s["hpmax"] is None
        and s["power"] is None and s["powermax"] is None and s["ptype"] is None and s["money"] is None
        and s["bag_all"] is None and s["wear"] is None):
    ok = False
    print("FAIL secret health / power", s)
# texts: put together from their pages in any order, a changed text starts again, an empty one is ""
tx = beacon.Texts()
long_name = "Stormwind City: Cathedral Square".encode()          # 32 bytes = 2 pages
for chan, ident, text, page in ((1, 5, long_name, 1), (3, 1, b"", 0), (1, 5, long_name, 0), (2, 9, "Zul'Gurub \u00e9".encode(), 0)):
    for pg in beacon.decode(draw_rows(strip({"level": 21}, {3: beacon.encode_page(chan, ident, text, page, 3)})))["pages"]:
        tx.add(pg)
if tx.done != {"label": "Stormwind City: Cathedral Square", "target": "", "zone": "Zul'Gurub \u00e9"}:
    ok = False
    print("FAIL names", tx.done)
tx.add(beacon._page(beacon.encode_page(1, 6, b"x" * 30, 0), 3))       # the name changed, only its first page seen
if tx.done["label"] != "Stormwind City: Cathedral Square" or 1 in tx.parts[1][1]:
    ok = False
    print("FAIL a half-read new name replaced the old one", tx.done, tx.parts)
tx.add(beacon._page(beacon.encode_page(1, 6, b"x" * 30, 1), 3))
if tx.done["label"] != "x" * 30:
    ok = False
    print("FAIL second name", tx.done)
# a long text (a quest with objectives: 200 bytes = 10 pages, its length needs both length squares)
long_q = ("23\t0\tThe Tortured Soul\t-" + "Tortured Soul slain: 0/1" + "\t+" + "y" * 150).encode()
tx = beacon.Texts()
for page in (9, 3, 0, 1, 2, 4, 5, 6, 8, 7, 9):
    tx.add(beacon._page(beacon.encode_page(11, 2, long_q, page, 5), 5))
if tx.done.get("q1") != long_q.decode() or len(long_q) <= 63:
    ok = False
    print("FAIL long text", tx.done)
if beacon._page(beacon.encode_page(11, 2, long_q, 0, 5), 6) is not None:
    ok = False
    print("FAIL a row read with another row's checksum")
# the party and the quest log, as the companion gets them: whole or not at all
d = {"stats": dict(beacon._stats(beacon.encode_stats({"level": 21, "quests": 2, "party": 2}))),
     "texts": {"party1": "Sylvaria\t23\tPRIEST", "party2": "", "party3": "Dotz\t0\tWARRIOR", "party4": "",
               "q1": "23\t0\tThe Tortured Soul\t-Tortured Soul slain: 0/1",
               "q2": "14\t1\tHow Big a Threat?\t+Blackwood Pathfinder slain: 8/8\t+Blackwood Windtalker slain: 5/5"}}
pl, ql = beacon.party(d), beacon.quests(d)
if pl != [{"slot": 1, "name": "Sylvaria", "level": 23, "cls": "Priest"}, {"slot": 3, "name": "Dotz", "level": None, "cls": "Warrior"}]:
    ok = False
    print("FAIL party", pl)
if ql != [{"level": 23, "title": "The Tortured Soul", "details": ["0/1 Tortured Soul slain"], "done": False, "failed": False},
          {"level": 14, "title": "How Big a Threat?", "details": ["8/8 Blackwood Pathfinder slain", "5/5 Blackwood Windtalker slain"],
           "done": True, "failed": False}]:
    ok = False
    print("FAIL quests", ql)
del d["texts"]["q2"], d["texts"]["party4"]
if beacon.quests(d) is not None or beacon.party(d) is not None:
    ok = False
    print("FAIL a half-arrived quest log / party was handed over")
# gear: the text the add-on sends -> average item level and score; the team is everybody we have a text for
mine = "20.2 0 18.2 22.3 15.1 21.2 19.2 0 17.2 0 0 0 0 16.2 24.3t 0 12.1"
g = beacon.gear_score(mine)
if not (g and g["worn"] == 10 and g["avg"] == 18.4 and g["empty"] == ["neck", "wrist", "ring 1", "ring 2", "trinket 1", "trinket 2"]
        and g["whole"] and g["score"] == round(20 + 18 + 22 * 1.15 + 15 * .75 + 21 + 19 + 17 + 16 + 24 * 1.15 * 2 + 12 * .75)):
    ok = False
    print("FAIL gear score", g)
g2 = beacon.gear_score("20.2 ? 18.2 22.3 15.1 21.2 19.2 0 17.2 0 0 0 0 16.2 24.3 20.2 12.1")
if not (g2 and not g2["whole"] and g2["worn"] == 11 and "off hand" not in g2["empty"]) or beacon.gear_score("") is not None \
        or beacon.gear_score("1.2 2.2") is not None:
    ok = False
    print("FAIL gear score, unknown item / bad text", g2)
d = {"stats": dict(beacon._stats(beacon.encode_stats({"level": 21, "party": 2}))),
     "texts": {"party1": "Sylvaria\t23\tPRIEST", "party2": "Dotz\t22\tWARRIOR", "party3": "", "party4": "",
               "gear": mine, "gear1": "30.3 " * 16 + "30.3", "gear2": ""}}
gs = beacon.gear(d)
if not (gs and gs["me"]["score"] == g["score"] and gs["party"][0]["name"] == "Sylvaria" and gs["party"][0]["score"] == round(30 * 1.15 * 17)
        and gs["party"][1]["score"] is None and gs["team"]["of"] == 2 and gs["team"]["missing"] == ["Dotz"]
        and gs["team"]["score"] == round((g["score"] + round(30 * 1.15 * 17)) / 2)):
    ok = False
    print("FAIL team gear", gs)
tx = beacon.Texts()                                                  # the gear texts travel like any other (7 pages)
for page in range(7):
    tx.add(beacon._page(beacon.encode_page(8, 1, mine.encode(), page, 4), 4))
    tx.add(beacon._page(beacon.encode_page(52, 1, mine.encode(), page, 4), 4))
if tx.done != {"gear": mine, "gear2": mine}:
    ok = False
    print("FAIL gear texts", tx.done)
# where the minimap is (text 9): real pixels + the window's size -> the layout's 1920x1080 terms; nonsense -> None
if beacon.text_name(9) != "minimap" or beacon.text_name(10) != "name" or beacon.minimap({"texts": {"minimap": "1790.4 158.2 95.6 1920 1080"}}) != (1790.4, 158.2, 95.6) \
        or beacon.minimap({"texts": {"minimap": "3580.0 316.0 190.0 3840 2160"}}) != (1790.0, 158.0, 95.0) \
        or any(beacon.minimap({"texts": {"minimap": s}}) is not None for s in ("", "1 2 3", "a b c d e", "5000 10 95 1920 1080", "900 500 3 1920 1080")) \
        or beacon.minimap(None) is not None:
    ok = False
    print("FAIL minimap spot")
# the bars above the strip (version 7): how much of each is lit; blue, covered or broken up = not trusted
def bars_img(fracs, noise=0):
    w, c = beacon.CELLS * beacon.CELL, beacon.CELL
    im = np.zeros((beacon.BARS * c, w, 3), dtype=np.float32)
    for i, fr in enumerate(fracs):
        y = (beacon.BARS - 1 - i) * c
        if fr == "blue":
            im[y:y + c, :, 2] = 255
        elif fr == "window":
            im[y:y + c] = (200, 190, 180)
        elif fr == "holes":
            im[y:y + c, 0:40, 1] = 255
            im[y:y + c, 60:90, 1] = 255
        else:
            im[y:y + c, :round(fr * w), 1] = 255
    if noise:
        im = np.clip(im + rnd.uniform(-noise, noise), 0, 255)
    return Image.fromarray(im.astype(np.uint8))


for fracs in ([0, 0.5, 1], [0.25, 0.754, 0.011], [1, 1, 0], ["blue", 0.3, "window"], [0.6, "holes", 0.9]):
    for noise in (0, 12):
        got = beacon.read_bars(bars_img(fracs, noise))
        want = [None if isinstance(fr, str) else fr for fr in fracs]
        if not all((g is None) == (w_ is None) and (g is None or abs(g - w_) <= 0.004) for g, w_ in zip(got, want)):
            ok = False
            print("FAIL bars", fracs, noise, got)

# a damaged row is left out; the others are still read. Older strips (versions 1 and 2) give row 1 only.
bad2, bad3 = beacon.encode_stats({"level": 21, "xp": 500, "xpmax": 1000}), beacon.encode_page(1, 1, b"Elwynn Forest", 0, 3)
bad2[3] ^= 9
bad3[10] ^= 1
r = beacon.decode(draw_rows([row1, bad2, bad3, beacon.encode_page(4, 1, b"Bob\t20\tMAGE", 0, 4), EMPTY, EMPTY, EMPTY, EMPTY]))
if not (r and r["inst"] == 33 and r["stats"] is None and [g["chan"] for g in r["pages"]] == [4]):
    ok = False
    print("FAIL damaged rows", r)
r = beacon.decode(draw_rows([row1] + [EMPTY] * 7))                   # rows not drawn yet (black)
if not (r and r["stats"] is None and r["pages"] == []):
    ok = False
    print("FAIL black rows", r)
for ver in (1, 2, 3, 4):
    old = beacon.encode({"instance": True, "inst": 36})
    old[7] = ver                                                     # its version square, the checksum redone
    cs = sum(([0] + old)[i] * (i - 7) for i in range(8, 46)) % 4096
    old[45], old[46] = cs >> 6, cs & 63
    r = beacon.decode(draw_rows([old] + [EMPTY] * 5))
    if not (r and r["version"] == ver and r["inst"] == 36 and ("stats" in r) == (ver >= 3)):
        ok = False
        print("FAIL older strip", ver, r)
# version 8: 8 rows and 7 bars (party health on bars 4-7); a version 7 strip (6 rows, 3 bars) in the same corner
# is still read while the owner has not reloaded yet
def corner(rows, fracs):
    """The window's corner as _look grabs it: (ROWS + BARS) * CELL high, the strip at the bottom, its bars above."""
    H, w, c = (beacon.ROWS + beacon.BARS) * beacon.CELL, beacon.CELLS * beacon.CELL, beacon.CELL
    im = np.full((H, w, 3), 90, dtype=np.uint8)                      # the game behind it
    st = np.asarray(draw_rows(rows))
    im[H - st.shape[0]:] = st[:, :w]
    top = H - st.shape[0]
    for i, fr in enumerate(fracs):
        y = top - (i + 1) * c
        im[y:y + c] = 0
        im[y:y + c, :round(fr * w), 1] = 255
    return Image.fromarray(im)


r = beacon.look_at(corner(strip({"level": 17, "target_level": 12}), [0.5, 0.25, 0.8, 1.0, 0.4, 0, 0]))
if not (r and r["version"] == 8 and abs(r["stats"]["hp_frac"] - 0.5) < 0.01 and abs(r["stats"]["target_frac"] - 0.8) < 0.01
        and [round(v, 1) for v in r["stats"]["party_frac"]] == [1.0, 0.4, 0.0, 0.0]):
    ok = False
    print("FAIL version 8 corner", r and r.get("stats"))
old7 = list(row1)
old7[7] = 7
cs = sum(([0] + old7)[i] * (i - 7) for i in range(8, 46)) % 4096
old7[45], old7[46] = cs >> 6, cs & 63
r = beacon.look_at(corner([old7, beacon.encode_stats({"level": 17})] + [EMPTY] * 4, [0.6, 0.3, 0.0]))
if not (r and r["version"] == 7 and abs(r["stats"]["hp_frac"] - 0.6) < 0.01 and "party_frac" not in r["stats"]):
    ok = False
    print("FAIL version 7 strip in the version 8 corner", r and r.get("stats"))
if beacon.look_at(corner([EMPTY] * 8, [0] * 7)) is not None:
    ok = False
    print("FAIL no strip")
# the version 8 texts
d = {"texts": {"buffs": "Demon Skin\t1000600\t0\nBlood Pact\t0\t0\nOld One\t999000\t0\nStacky\t1000030\t3",
               "cooldowns": "Shadowburn\t1000012", "supplies": "Soul Shard\t3\t15.0\nIce Cold Milk\t12\t0.5\nJunk\tx\t0.0",
               "events": "7\tvendor\tBob\n8\tgather\tHerb Gathering: Peacebloom\nbad line\n9\twhisper\t",
               "quest_areas": "1436\nWestfall Stew\t562\t310\nBad\t5000\t1\nShort\t1",
               "skills": "s\tTailoring\t75\t150\nt\tAffliction\t8\ns\tbroken", "reputation": "Stormwind\t5\t1200\t6000\nx\t99\t1\t1"}}
tm = beacon.timed(d, "buffs", now=1000000)
if [(e["name"], e["left"], e.get("stacks")) for e in tm] != [("Demon Skin", 600, None), ("Blood Pact", None, None), ("Stacky", 30, 3)]:
    ok = False
    print("FAIL buffs", tm)
if beacon.timed(d, "cooldowns", now=1000000) != [{"name": "Shadowburn", "left": 12}] or beacon.timed({}, "buffs") != []:
    ok = False
    print("FAIL cooldowns")
# cooldowns from the 2026-11-25 add-on: which button, the pet, worn and carried things, and the bar's own line
d2 = {"texts": {"cooldowns": "#bar\t0\t111100111111\t-\nShadowburn\t1000012\tb3\nDevour Magic\t1000008\tp5\n"
                             "Healing Potion\t1000100\ti\nOdd\t1000050\tzz9\nTrinket\t1000300\te13"}}
tm = beacon.timed(d2, "cooldowns", now=1000000)
if [(e["name"], e["left"], e.get("at"), e.get("stacks")) for e in tm] != [
        ("Shadowburn", 12, "b3", None), ("Devour Magic", 8, "p5", None), ("Healing Potion", 100, "i", None),
        ("Odd", 50, None, None), ("Trinket", 300, "e13", None)]:
    ok = False
    print("FAIL cooldowns with places", tm)
if beacon.bar(d2) != {"has": [True] * 4 + [False] * 2 + [True] * 6, "whole": True, "set": "111100111111/"} \
        or beacon.bar(d) is not None \
        or beacon.bar({"texts": {"cooldowns": "#bar\t0\t-\th\t12"}}) != {"has": None, "whole": False, "set": "-/12"}:
    ok = False
    print("FAIL bar", beacon.bar(d2))
import companion
scr = [{"slot": i, "key": str(i), "state": s} for i, s in enumerate(["ready", "cooldown", "ready", "ready", "cooldown", "ready"], 1)]
more = {"cooldowns": tm, "bar": beacon.bar(d2)}
got = companion.buttons_checked(scr, more)
if [b["state"] for b in got] != ["ready", "unusable", "cooldown", "ready", "empty", "empty"] or got[2].get("left") != 12 \
        or got[2].get("name") != "Shadowburn" or scr[1]["state"] != "cooldown" \
        or companion.buttons_checked(scr, {"cooldowns": [], "bar": None}) is not scr \
        or companion.buttons_checked(scr, {"cooldowns": [], "bar": {"has": None, "whole": False}})[1]["state"] != "cooldown" \
        or companion.buttons_checked([{"slot": 1, "state": "empty"}], more)[0]["state"] != "ready":
    ok = False
    print("FAIL buttons checked", got)
if beacon.supplies(d) != [{"name": "Soul Shard", "n": 3, "kind": "shard"}, {"name": "Ice Cold Milk", "n": 12, "kind": "food"}]:
    ok = False
    print("FAIL supplies", beacon.supplies(d))
if beacon.events(d) != [(7, "vendor", "Bob"), (8, "gather", "Herb Gathering: Peacebloom"), (9, "whisper", "")]:
    ok = False
    print("FAIL events", beacon.events(d))
if beacon.quest_areas(d) != (1436, [{"title": "Westfall Stew", "x": 56.2, "y": 31.0}]) or beacon.quest_areas({}) is not None:
    ok = False
    print("FAIL quest areas", beacon.quest_areas(d))
if beacon.skills(d) != {"skills": [{"name": "Tailoring", "rank": 75, "max": 150}], "talents": [{"name": "Affliction", "points": 8}]}:
    ok = False
    print("FAIL skills", beacon.skills(d))
if beacon.reputation(d) != [{"name": "Stormwind", "standing": "Friendly", "at": 1200, "of": 6000}]:
    ok = False
    print("FAIL reputation", beacon.reputation(d))
print("ALL PASS" if ok else "FAILED")
