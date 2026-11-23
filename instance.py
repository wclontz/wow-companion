"""Instance (dungeon) tracker: everything about each dungeon, kept forever, one file per instance.

First visit: a lookup of the dungeon (level, bosses in order) from wow-forever.gg, plus per-mob
lookups (mobinfo). Every run then records: mobs met (ids, levels, where they stand, kills), boss
encounters (attempts, kills, fight length, where), your route, deaths, loot (chat log) and XP.
From the second visit on, the page shows the previous route with bosses on it and points you
at the next boss you haven't killed this run.

Runs are keyed by start time, so re-reading old logs after a restart updates runs instead of
duplicating them.
"""
import html
import json
import math
import os
import re
import threading
import time
import urllib.request
from collections import defaultdict

from group import SPELL_CLASS
from itemdb import ItemDB

HERE = os.path.dirname(os.path.abspath(__file__))
INST_DIR = os.path.join(HERE, "instances")
DUNGEONS_FILE = os.path.join(HERE, "dungeons.json")
OPEN_WORLD = {0, 1, 530, 571, 870, 1220, 1643, 2444}     # continents: everything else is an instance
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WoWCompanion/1.0 (personal second-screen tool)"
T_NPC, T_PLAYER, FRIENDLY = 0x800, 0x400, 0x10


def _get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _lines(h):
    h = re.sub(r"<script.*?</script>|<style.*?</style>", "", h, flags=re.S)
    return [ln.strip() for ln in html.unescape(re.sub(r"<[^>]+>", "\n", h)).split("\n") if ln.strip()]


def safe(name):
    return re.sub(r"[^A-Za-z0-9' -]", "", name).strip().replace(" ", "_") or "instance"


# ------------------------------------------------------------------ dungeon lookups (wow-forever.gg)


def _far(p):
    """World yards (thousands) rather than an instance's own (hundreds)?"""
    return bool(p) and p[0] is not None and (abs(p[0]) > 3000 or abs(p[1]) > 3000)


def two_halves(runs):
    """Forever's Deadmines is ONE run in two sets of numbers: the open-world mine logs world yards, the instance
    behind its portal its own - 11,000 yd apart. True when these runs hold both kinds of points. (Only then is
    anything split: other dungeons' own numbers can be large too.)"""
    far = near = False
    for r in runs:
        for p in (r or {}).get("trail") or []:
            if _far(p[1:]):
                far = True
            else:
                near = True
            if far and near:
                return True
    return False


def portal_of(look):
    """The dungeon's entrance in world yards, from its lookup ('... world coordinates -11207.8, 1681.2, ...')."""
    m = re.search(r"(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)", (look or {}).get("entrance") or "")
    return [float(m.group(1)), float(m.group(2))] if m else None


def _dump(path, obj, indent=0):
    """Whole file to a side file, then swapped in: a kill mid-write can't leave half a file (a half-written
    record used to read as 'no runs', and the next save wrote that over the dungeon's whole history)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=indent)
    for i in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 5:
                raise
            time.sleep(0.05)


def map_place(cur):
    """The minimap map a run draws on: its dungeon's name, or '<name> (open world)' for Forever's open-world
    version of it (world yards instead of the instance's own)."""
    return (cur or {}).get("map_place") or (cur or {}).get("name")

class DungeonInfo(threading.Thread):
    """map id -> {name, level, req_level, players, bosses:[{name, encounter}], entrance}. Cached forever."""

    def __init__(self):
        super().__init__(daemon=True)
        try:
            with open(DUNGEONS_FILE, encoding="utf-8") as _f:
                self.db = json.load(_f)
        except (OSError, ValueError):
            self.db = {}
        self.ready = threading.Event()

    def run(self):
        if not self.db.get("_by_map"):
            try:
                self._build()
            except Exception:
                pass
        self.ready.set()

    def _build(self):
        listing = _get("https://wow-forever.gg/dungeons/") + _get("https://wow-forever.gg/raids/")
        slugs = sorted(set(re.findall(r'href="/(dungeons|raids)/([a-z0-9-]+)/"', listing)))
        by_map = {}
        for kind, slug in slugs:
            try:
                L = _lines(_get(f"https://wow-forever.gg/{kind}/{slug}/"))
            except Exception:
                continue
            info = {"slug": slug, "kind": kind, "bosses": []}
            for i, ln in enumerate(L):
                nxt = L[i + 1] if i + 1 < len(L) else ""
                if ln == "Map id" and nxt.isdigit():
                    info["map_id"] = int(nxt)
                elif ln == "Level" and re.fullmatch(r"[\d\-–]+", nxt) and "level" not in info:
                    info["level"] = nxt
                elif ln == "Required level" and nxt.isdigit():
                    info["req_level"] = int(nxt)
                elif ln == "Players" and nxt.isdigit():
                    info["players"] = int(nxt)
                elif ln == "Instance zone" and "zone" not in info:
                    info["zone"] = nxt
                elif ln.startswith("Encounter ") and ln[10:].isdigit() and i >= 1:
                    name = L[i - 1]
                    if not any(b["name"] == name for b in info["bosses"]):
                        info["bosses"].append({"name": name, "encounter": int(ln[10:])})
                elif ln.startswith("Entrance:"):
                    info["entrance"] = ln
            if info.get("map_id"):
                by_map[str(info["map_id"])] = info
            time.sleep(0.4)
        self.db = {"_by_map": by_map, "t": time.time()}
        _dump(DUNGEONS_FILE, self.db, indent=1)

    def for_map(self, map_id):
        return (self.db.get("_by_map") or {}).get(str(map_id))

    def for_name(self, name):
        n = " ".join((name or "").lower().replace("the ", "").split()).strip(" .")
        for v in (self.db.get("_by_map") or {}).values():
            z = (v.get("zone") or v["slug"].replace("-", " ")).lower().replace("the ", "")
            if z == n or v["slug"].replace("-", " ") == n:
                return v
        return None

    def all(self):
        return sorted((self.db.get("_by_map") or {}).values(),
                      key=lambda v: int(re.match(r"\d+", v.get("level") or "99").group()))


# ------------------------------------------------------------------ tracking

def _new_run(t, map_id, name):
    return {"start": t, "end": t, "map_id": map_id, "name": name, "kills": defaultdict(int), "fights": [],
            "mobs": {}, "bosses": {}, "deaths": [], "trail": [], "loot": [], "party": [], "xp": 0}


def members(r):
    """[{name, lvl, lvl_end, cls}] for a run, in the order they were first seen (you first)."""
    info = r.get("party_info") or {}

    def one(n, m):
        if "gear" not in m:                     # saved before we knew that number was item level
            m = {"cls": m.get("cls"), "gear": m.get("lvl"), "gear_end": m.get("lvl_end")}
        did = (r.get("tally") or {}).get(n) or {}
        return dict(name=n, lvl=m.get("lvl"), lvl_end=m.get("lvl_end"), gear=m.get("gear"),
                    gear_end=m.get("gear_end"), cls=m.get("cls"),
                    dmg=did.get("dmg", 0), heal=did.get("heal", 0), taken=did.get("taken", 0))
    out = [one(n, info.get(n, {})) for n in r.get("party", [])]
    out += [one(n, m) for n, m in info.items() if n not in r.get("party", [])]
    return out[:5]


def run_end(r, n_bosses=None):
    """A run ends when its last boss dies (if every boss was killed); otherwise when you left.
    Kill time = that boss's pull + fight length."""
    if r.get("cleared_t"):
        return r["cleared_t"]
    bs = r.get("bosses") or {}
    killed = [b for b in bs.values() if b.get("killed") and b.get("start")]
    if n_bosses and len(killed) >= n_bosses:
        return max(b["start"] + (b.get("dur") or 0) for b in killed)
    return r["end"]


class InstanceTracker:
    def __init__(self):
        os.makedirs(INST_DIR, exist_ok=True)
        self.cur = None                      # current run dict while inside an instance
        self.cur_file = None
        self.records = {}                    # instance name -> record (loaded from disk)
        self.last_save = 0
        self.me = None
        self.ppos = {}                       # player guid -> last (x, y) seen in the log
        self._last_exit = None
        self._won = {}
        self.on_kill_now = None              # callback(t_kill, (t, x, y) your last logged spot, place)
        self.on_me_died = None               # callback(t, place): you died in this run
        self.on_my_pos = None                # callback(t, x, y, place): your logged position (minimap stitcher)                       # item -> (winner, t) from "Loot: X won: item"
        self.items = ItemDB()                # loot quality + vendor value (Wowhead)
        self.lookup = DungeonInfo()
        self.lookup.start()

    # -------------------------------------------------- persistence
    def _path(self, name):
        return os.path.join(INST_DIR, safe(name) + ".json")

    def record(self, name):
        rec = self.records.get(name)
        if rec is None:
            try:
                with open(self._path(name), encoding="utf-8") as _f:
                    rec = json.load(_f)
                if not isinstance(rec, dict) or not isinstance(rec.get("runs"), dict):
                    raise ValueError
            except OSError:
                rec = {"name": name, "map_id": None, "runs": {}}
            except ValueError:                           # a broken file: keep it, never save an empty one over it
                try:
                    os.replace(self._path(name), self._path(name) + time.strftime(".broken_%Y%m%d_%H%M%S"))
                except OSError:
                    pass
                rec = {"name": name, "map_id": None, "runs": {}}
            self.records[name] = rec
        return rec

    def save(self, force=False):
        if not self.cur or self.cur.get("pending") or (not force and time.time() - self.last_save < 30):
            return
        rec = self.record(self.cur["name"])
        rec["map_id"] = self.cur["map_id"]
        run = dict(self.cur)
        run["kills"] = dict(run["kills"])
        for k in [k for k, r in rec["runs"].items()
                  if k != str(int(self.cur["start"])) and r["start"] <= self.cur["end"]
                  and r.get("end", r["start"]) >= self.cur["start"]]:
            del rec["runs"][k]                       # a piece of this same run saved separately
        rec["runs"][str(int(self.cur["start"]))] = run
        try:
            _dump(self._path(rec["name"]), rec)
        except (OSError, RuntimeError):                  # (RuntimeError: another thread changed it mid-write)
            pass
        self.last_save = time.time()

    # -------------------------------------------------- events from the combat log
    def _is_dungeon(self, map_id, difficulty):
        known = self.lookup.db.get("_by_map")
        if known:
            return str(map_id) in known                  # only real dungeons/raids (not the tram etc.)
        return difficulty > 0

    def on_zone(self, t, map_id, name, difficulty=1):
        # the minimap map's own name: Forever's open-world Deadmines (zone 0) logs Eastern Kingdoms WORLD yards,
        # the real instance (map 36) its own local yards - one map holding both put rooms 11,000 yd apart
        mp = (f"{name} (open world)" if map_id in OPEN_WORLD else name) if name else name
        if map_id in OPEN_WORLD and name:
            # WoW Forever runs some dungeons in the open world (the Deadmines: zone 0, mobs on map 0).
            # A zone named like a dungeon from the list IS that dungeon.
            dg = self.lookup.for_name(name)
            if dg and dg.get("map_id"):
                map_id = int(dg["map_id"])
                name = dg.get("zone") or name
        if map_id in OPEN_WORLD or not name or not self._is_dungeon(map_id, difficulty):
            if self.cur:
                if self.cur.get("pending"):          # left before killing anything: no run at all
                    self.cur = None
                    self._fight = None
                    return
                self._close_fight()
                if t - self.cur["end"] < 300:        # (logged out inside: the next login's zone line isn't when it ended -
                    self.cur["end"] = t              #  one run came out 8.4 hours long)
                self.save(force=True)
                self._last_exit = (self.cur["name"], t, self.cur)
                self.cur = None
            return
        if self.cur and self.cur["map_id"] == map_id:
            self.cur["map_place"] = mp               # (through the portal: open world <-> instance, same run)
            return                                   # moved around inside the same instance
        if self.cur:
            self.save(force=True)
        rec = self.record(name)
        # back in within 30 min of leaving = the same run (WoW keeps your instance)
        if self._last_exit and self._last_exit[0] == name and t - self._last_exit[1] < 1800 and self._last_exit[2]                 and not self._cleared(self._last_exit[2]):      # a finished clear + going back in = a new run
            self.cur = self._last_exit[2]
            self.cur["map_place"] = mp
            return
        self.cur = _new_run(t, map_id, name)
        self.cur["map_place"] = mp
        self.cur["pending"] = True                   # the clock starts at the first kill
        self._fight = None

    def note_tally(self, kind, name, amt):
        """Damage done / healing done / damage taken by a party member (pets count for their owner: the group
        tracker works that out), added up for the run you are in. Kept with the run as "tally"."""
        cur = self.cur
        if cur:
            one = cur.setdefault("tally", {}).setdefault(name, {"dmg": 0, "heal": 0, "taken": 0})
            one[kind] = one.get(kind, 0) + amt

    def _start_clock(self, t):
        """First kill: the run becomes real. It starts when that first fight began."""
        cur = self.cur
        f = getattr(self, "_fight", None)
        cur["start"] = (f["start"] if f and f.get("start") else t)
        cur.pop("pending", None)
        old = self.record(cur["name"])["runs"].get(str(int(cur["start"])))
        if old:                                      # replaying a log we've seen: rebuild that run
            self._carry(old)
        self.save(force=True)

    def _carry(self, old):
        """Fields a log replay can't rebuild (chat loot, screen-read levels, snapshots)."""
        self.cur["xp"] = old.get("xp", 0)
        self.cur["loot"] = old.get("loot", [])   # loot comes from the chat log, which may be gone now
        self.cur["copper"] = old.get("copper", 0)
        self.cur["copper_seen"] = old.get("copper_seen", [])
        self.cur["snaps"] = old.get("snaps", [])
        # screen-read levels can't be replayed from the log: keep them
        self.cur["party_info"] = {n: {"lvl": m.get("lvl"), "lvl_end": m.get("lvl_end"), "gear": None,
                                      "gear_end": None, "cls": m.get("cls"), "guid": m.get("guid")}
                                  for n, m in (old.get("party_info") or {}).items() if "gear" in m}

    def _boss_by_death(self, t, name):
        """Open-world dungeons (Forever's Deadmines) send no ENCOUNTER events: a mob named like one of the
        dungeon's bosses dying = that boss killed, timed from when its fight started."""
        cur = self.cur
        look = self.lookup.for_map(cur.get("map_id")) or self.lookup.for_name(cur.get("name"))
        if not look or name not in {b["name"] for b in look.get("bosses") or []}:
            return
        b = cur["bosses"].setdefault(name, {"encounter": None, "attempts": 0, "killed": False,
                                            "pos": None, "dur": None, "start": None})
        if b["killed"]:
            return
        f = getattr(self, "_fight", None)
        b["start"] = b["start"] or (f["start"] if f else t)
        b["attempts"] = max(1, b["attempts"])
        b["dur"] = round(t - b["start"], 1)
        b["killed"] = True
        if b["pos"] is None and cur["trail"]:
            b["pos"] = cur["trail"][-1][1:]
        if f is not None:
            f["boss"] = f.get("boss") or name
        if not cur.get("cleared_t") and self._cleared(cur):
            cur["cleared_t"] = round(t, 1)
        self.save(force=True)

    def _cleared(self, run):
        """All of the dungeon's bosses dead in this run."""
        look = self.lookup.for_map(run.get("map_id")) or self.lookup.for_name(run.get("name"))
        n = len((look or {}).get("bosses") or [])
        return bool(n) and sum(1 for b in run.get("bosses", {}).values() if b.get("killed")) >= n

    def on_encounter(self, t, ev, c):
        if not self.cur or len(c) < 3:
            return
        name = c[2]
        b = self.cur["bosses"].setdefault(name, {"encounter": c[1], "attempts": 0, "killed": False,
                                                 "pos": None, "dur": None, "start": None})
        if ev == "ENCOUNTER_START":
            b["attempts"] += 1
            b["start"] = t
            if self.cur["trail"]:
                b["pos"] = self.cur["trail"][-1][1:]
        else:
            ok = len(c) > 5 and c[5] == "1"
            b["killed"] = b["killed"] or ok
            if len(c) > 6 and c[6].isdigit():
                b["dur"] = round(int(c[6]) / 1000, 1)
            elif b["start"]:
                b["dur"] = round(t - b["start"], 1)
            if ok and not self.cur.get("cleared_t") and self._cleared(self.cur):
                self.cur["cleared_t"] = round(t, 1)          # last boss down: the run is done
                self.save(force=True)
        self.cur["end"] = t

    def _member(self, cur, g, n):
        """Per-run party roster: name -> {lvl (on entering), lvl_end, cls, guid}."""
        info = cur.setdefault("party_info", {})
        nm = n.split("-")[0]
        m = info.get(nm)
        if m is None and len(info) < 5:
            m = info[nm] = {"lvl": None, "lvl_end": None, "gear": None, "gear_end": None, "cls": None, "guid": g}
        return m

    def hud_map(self):
        """Inside a dungeon there's no zone picture: give the HUD the route from the log instead
        (world x = north, y = west). This run's path + pulls, and the last earlier run's path/pulls in grey."""
        cur = self.cur
        if not cur:
            le = self._last_exit
            if not (le and le[2] and time.time() - le[1] < 600):
                return None
            cur = le[2]
        rec = self.record(cur["name"])
        past = sorted((r for r in rec["runs"].values() if int(r["start"]) != int(cur["start"]) and r.get("trail")),
                      key=lambda r: r["start"])
        thin = lambda tr: [p[1:] for p in tr[::max(1, len(tr) // 400)]]
        pulls = lambda r: [[f["pos"][0], f["pos"][1], f["size"], f.get("boss")] for f in r.get("fights", []) if f.get("pos")]
        # Forever's open-world version logs world yards (thousands), the instance its own (hundreds): show only the
        # points of the half you're in, or the route draws an 11,000 yd line between them
        world = map_place(cur).endswith("(open world)")
        split = two_halves([cur] + past[-1:])
        same = (lambda p: _far(p) == world) if split else (lambda p: True)
        look = self.lookup.for_map(cur.get("map_id")) or self.lookup.for_name(cur.get("name"))
        portal = portal_of(look) if world else None
        me = (self.live_me(map_place(cur)) if getattr(self, "live_me", None) else None) or             (self.ppos.get(self.me) if self.me else None)
        return {"name": cur["name"], "map": map_place(cur), "live": cur is self.cur,
                "portal": portal if portal and _far(portal) else None,     # the way in, while you're in the open-world mine
                "trail": [p for p in thin(cur["trail"]) if same(p)], "pulls": [p for p in pulls(cur) if same(p)],
                "old_trail": [p for p in thin(past[-1]["trail"]) if same(p)] if past else [],
                "old_pulls": [p for p in pulls(past[-1]) if same(p)] if past else [],
                "deaths": [[d[2], d[3], d[1]] for d in cur["deaths"] if d[2] is not None and same((d[2], d[3]))],
                "me": list(me) if me else (cur["trail"][-1][1:] if cur["trail"] else None)}

    def note_level(self, name, lvl):
        """Character level read off the screen (your portrait / your target's frame) for a party member."""
        cur = self.cur
        if not cur or not name or not lvl:
            return
        key = lambda s: re.sub(r"[^a-z]", "", s.split()[0].split("-")[0].lower()) if s and s.split() else ""
        k = key(name)
        for n, m in (cur.get("party_info") or {}).items():
            kn = key(n)
            if kn and k and (kn == k or (len(k) >= 4 and (kn.startswith(k) or k.startswith(kn)))):
                if not m.get("lvl"):
                    m["lvl"] = lvl
                if lvl >= (m.get("lvl") or 0) and lvl - (m.get("lvl") or lvl) <= 3:     # OCR guard
                    m["lvl_end"] = max(m.get("lvl_end") or 0, lvl)
                return

    def on_event(self, t, ev, c, sg, sn, sf, dg, dn, df, adv_at, spell=None):
        cur = self.cur
        if not cur:
            return
        cur["end"] = t
        for g, n, f in ((sg, sn, sf), (dg, dn, df)):
            if g.startswith("Creature-") and n and f & T_NPC and not f & FRIENDLY and not f & (0x1000 | 0x2000)                     and not f & 0x7 and f & 0x60:
                parts = g.split("-")
                m = cur["mobs"].setdefault(n, {"id": int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else None,
                                               "guids": [], "levels": [], "pos": []})
                if g not in m["guids"] and len(m["guids"]) < 200:
                    m["guids"].append(g)
        if adv_at is not None:
            ig = c[adv_at]
            try:
                x, y, lvl = float(c[adv_at + 14]), float(c[adv_at + 15]), int(float(c[adv_at + 18]))
            except (ValueError, IndexError):
                x = y = None
            if ig.startswith("Player-") and x is not None and lvl > 0:
                pn, pf = (sn, sf) if ig == sg else (dn, df)
                if pn and (ig == self.me or pf & self.PARTY_FLAGS):
                    m = self._member(cur, ig, pn)
                    if m is not None:                   # players: this number is average item level
                        m["gear"] = m.get("gear") or lvl
                        m["gear_end"] = max(m.get("gear_end") or 0, lvl)
            if x is not None and (x, y) != (0.0, 0.0):
                if ig.startswith("Player-"):
                    self.ppos[ig] = (round(x, 1), round(y, 1))
                if ig == self.me:
                    # which map this position is on: Forever's Deadmines portal logs no zone change - only this
                    # number tells: 0 = inside the instance (its own yards), a world map id (1415 Eastern
                    # Kingdoms) = the open-world mine outside it (world yards). Each gets its own minimap map.
                    try:
                        um = int(float(c[adv_at + 16]))
                    except (ValueError, IndexError):
                        um = None
                    if um is not None:
                        want = cur["name"] if um == 0 else f"{cur['name']} (open world)"
                        if cur.get("map_place") != want:
                            cur["map_place"] = want
                    self.my_last = (t, x, y)
                    if self.on_my_pos:
                        try:
                            self.on_my_pos(t, x, y, map_place(cur))
                        except Exception:
                            pass
                    tr = cur["trail"]
                    if not tr or math.hypot(x - tr[-1][1], y - tr[-1][2]) > 4:
                        tr.append([round(t, 1), round(x, 1), round(y, 1)])
                elif ig.startswith("Creature-"):
                    nm = sg if False else (sn if ig == sg else dn)
                    m = cur["mobs"].get(nm)
                    if m is not None:
                        if len(m["pos"]) < 40 and all(math.hypot(x - p[0], y - p[1]) > 8 for p in m["pos"]):
                            m["pos"].append([round(x, 1), round(y, 1)])
                        if lvl > 0 and lvl not in m["levels"]:
                            m["levels"].append(lvl)
        self._fight_event(t, ev, c, sg, sn, sf, dg, dn, df, adv_at)
        if ev == "UNIT_DIED":
            if df & T_NPC and dn in cur["mobs"]:
                self._boss_by_death(t, dn)
                # the log flushes on a kill: a fresh kill + your last logged spot = where you are right now
                ml = getattr(self, "my_last", None)
                if ml and t - ml[0] < 15:
                    self.kill_pos = (t, ml[1], ml[2])        # where you stood when the fight ended
                if self.on_kill_now and ml and time.time() - t < 600:      # late logs too (frames kept ~10 min)
                    try:
                        self.on_kill_now(t, ml, map_place(cur))
                    except Exception:
                        pass
                if cur.get("pending"):
                    self._start_clock(t)
                cur["kills"][dn] += 1
            elif df & T_PLAYER:
                if dg == self.me and self.on_me_died:
                    try:
                        self.on_me_died(t, map_place(cur))
                    except Exception:
                        pass
                p = self.ppos.get(dg) or ((cur["trail"][-1][1], cur["trail"][-1][2]) if cur["trail"] else (None, None))
                cur["deaths"].append([round(t, 1), dn.split("-")[0], p[0], p[1]])
        if spell in SPELL_CLASS and sf & T_PLAYER and sn and (sg == self.me or sf & self.PARTY_FLAGS):
            m = self._member(cur, sg, sn)
            if m is not None and not m["cls"]:
                m["cls"] = SPELL_CLASS[spell]
        if sf & T_PLAYER and sn and len(cur["party"]) < 5:
            nm = sn.split("-")[0]
            if nm not in cur["party"] and (sg == self.me or sf & 0x6):
                cur["party"].append(nm)
        self.save()

    PARTY_FLAGS = 0x1 | 0x2 | 0x4

    def _party(self, g, f):
        return g == self.me or (f & T_PLAYER and f & self.PARTY_FLAGS) or (f & (0x1000 | 0x2000) and f & self.PARTY_FLAGS)

    def _fight_event(self, t, ev, c, sg, sn, sf, dg, dn, df, adv_at):
        cur = self.cur
        f = getattr(self, "_fight", None)
        if f and t - f["end"] > 6:
            self._close_fight()
            f = None
        is_dmg = ev.endswith("_DAMAGE") and not ev.endswith("_LANDED")
        is_heal = ev in ("SPELL_HEAL", "SPELL_PERIODIC_HEAL")
        mob_g = mob_n = None
        if is_dmg and self._party(sg, sf) and dn in cur["mobs"]:
            mob_g, mob_n = dg, dn
        elif is_dmg and sn in cur["mobs"] and self._party(dg, df):
            mob_g, mob_n = sg, sn
        if mob_g:
            if f is None:
                f = self._fight = {"start": t, "end": t, "mobs": {}, "kills": 0, "taken": 0, "dealt": 0,
                                   "healed": 0, "deaths": [], "pos": None, "boss": None}
            f["end"] = t
            f["mobs"][mob_g] = mob_n
            amt = None
            s = (adv_at + 19) if adv_at is not None else (9 + (0 if ev.startswith("SWING") else 3))
            try:
                amt = int(float(c[s]))
            except (ValueError, IndexError):
                amt = 0
            if mob_g == dg:
                f["dealt"] += amt
            else:
                f["taken"] += amt
            if f["pos"] is None and adv_at is not None:
                try:
                    x, y = float(c[adv_at + 14]), float(c[adv_at + 15])
                    if (x, y) != (0.0, 0.0):
                        f["pos"] = [round(x, 1), round(y, 1)]
                except (ValueError, IndexError):
                    pass
            for bn, b in cur["bosses"].items():
                if b.get("start") and abs(t - b["start"]) < 120 and mob_n == bn:
                    f["boss"] = bn
        elif f and is_heal and self._party(sg, sf):
            s = (adv_at + 19) if adv_at is not None else 12
            try:
                f["healed"] += int(float(c[s])) - int(float(c[s + 2]))
            except (ValueError, IndexError):
                pass
        elif f and ev == "UNIT_DIED":
            if dg in f["mobs"]:
                f["kills"] += 1
            elif df & T_PLAYER and df & self.PARTY_FLAGS:
                f["deaths"].append(dn.split("-")[0])

    def _close_fight(self):
        f = getattr(self, "_fight", None)
        self._fight = None
        if not f or not self.cur:
            return
        if f["pos"] is None and self.cur["trail"]:
            f["pos"] = self.cur["trail"][-1][1:]
        names = defaultdict(int)
        for n in f["mobs"].values():
            names[n] += 1
        self.cur["fights"].append({"t": round(f["start"], 1), "dur": round(f["end"] - f["start"], 1),
                                   "size": len(f["mobs"]), "mobs": dict(names), "kills": f["kills"],
                                   "taken": f["taken"], "dealt": f["dealt"], "healed": f["healed"],
                                   "deaths": f["deaths"], "pos": f["pos"], "boss": f["boss"]})

    @staticmethod
    def _deaths(run, roles, recaps):
        out = []
        for d in run.get("deaths", []):
            if len(d) < 4 or d[2] is None:
                continue
            t, name, x, y = d[:4]
            role, cls = roles.get(name, ("dps", None))
            rc = next((r for r in recaps if r.get("name") == name and abs(r.get("t", 0) - t) < 5), None)
            killer = rc["hits"][-1] if rc and rc.get("hits") else None
            out.append({"t": t, "name": name, "pos": [x, y], "role": role, "cls": cls,
                        "killer": f"{killer[1]} {killer[2]} {killer[3]}" if killer else None,
                        "hits": [f"{h[1]} {h[2]} {h[3]}" for h in (rc["hits"][-4:] if rc else [])]})
        return out

    # -------------------------------------------------- loot (chat log)
    def _run_for(self, t):
        """The run (live, just-left or saved) that time t falls in. Chat lines are read after the
        combat log, so loot often arrives when we're already outside: match on time, not 'inside'."""
        if self.cur and t >= self.cur["start"] - 30:
            return self.cur, None
        if self._last_exit and self._last_exit[2]:
            r = self._last_exit[2]
            if r["start"] - 30 <= t <= r["end"] + 120:
                return r, None
        try:
            names = [f[:-5] for f in os.listdir(INST_DIR) if f.endswith(".json")]
        except OSError:
            names = []
        for fn in names:
            p = os.path.join(INST_DIR, fn + ".json")
            rec = next((r for r in self.records.values() if self._path(r["name"]) == p), None)
            if rec is None:
                try:
                    with open(p, encoding="utf-8") as _f:
                        rec = json.load(_f)
                    self.records[rec["name"]] = rec
                except (OSError, ValueError, KeyError):
                    continue
            for r in rec["runs"].values():
                if r["start"] - 30 <= t <= r["end"] + 120:
                    return r, rec
        return None, None

    def _store(self, r, rec):
        if rec is None:
            if r is self.cur:
                self.save()
            else:                                   # the run we just left: write it straight away
                rec = self.record(r["name"])
                rec["runs"][str(int(r["start"]))] = dict(r, kills=dict(r["kills"]))
        if rec is not None:
            try:
                _dump(self._path(rec["name"]), rec)
            except (OSError, RuntimeError):
                pass

    def loot_rows(self, entries):
        """Loot entries -> one row per item (count, who got it, rolls, quality, value, tooltip), most valuable first."""
        loot = {}
        for e in entries:
            item, n = e[1], e[2]
            who = e[3] if len(e) > 3 else "You"
            d = loot.setdefault(item, {"item": item, "n": 0, "who": defaultdict(int), "rolls": 0, "last": 0})
            d["n"] += n
            d["who"][who] += n
            d["rolls"] += 1 if len(e) > 4 and e[4] == "roll" else 0
            d["last"] = max(d["last"], e[0])
        for d in loot.values():
            d["who"] = dict(d["who"])
            it = self.items.get(d["item"]) or {}
            d["q"], d["sell"], d["icon"], d["tt"] = it.get("q"), it.get("sell", 0), it.get("icon"), it.get("tt")
            d["value"] = d["sell"] * d["n"]
        return sorted(loot.values(), key=lambda x: (-x["value"], -(x["q"] or 0), -x["n"]))

    def on_roll_won(self, t, who, item):
        self._won[item] = (who, t)

    def on_loot(self, t, item, n, who="You"):
        r, rec = self._run_for(t)
        if r is None:
            return
        t = round(t, 1)
        if any(e[0] == t and e[1] == item and (len(e) < 4 or e[3] == who) for e in r["loot"]):
            return                                  # replayed line
        w = self._won.get(item)
        how = "roll" if w and w[0] == who and 0 <= t - w[1] < 30 else ""
        r["loot"].append([t, item, n, who, how])
        self._store(r, rec)

    def on_money(self, t, copper):
        r, rec = self._run_for(t)
        if r is None:
            return
        seen = r.setdefault("copper_seen", [])
        if round(t, 1) in seen:
            return
        seen.append(round(t, 1))
        r["copper"] = r.get("copper", 0) + copper
        self._store(r, rec)

    def add_xp(self, amount):
        if self.cur:
            self.cur["xp"] += amount

    # -------------------------------------------------- page data
    def snapshot(self, name=None, mob_moves=None, mob_heals=None, mobinfo=None, me_pos=None, roles=None, recaps=None,
                 half=None):
        """half: None = the half you're in, "world" / "instance" = that half of a two-part dungeon (the map page
        asks for the one its picture shows)."""
        if name and name.endswith(" (open world)"):      # the mapper's name for the open-world half
            name, half = name[:-len(" (open world)")], "world"
        names = sorted({os.path.splitext(f)[0].replace("_", " ") for f in os.listdir(INST_DIR) if f.endswith(".json")})
        if self.cur:
            self.save()
        target = name or (self.cur["name"] if self.cur else (names[0] if names else None))
        dungeons = [{"name": v.get("zone") or v["slug"].replace("-", " ").title(), "level": v.get("level"),
                     "req_level": v.get("req_level"), "bosses": len(v["bosses"]), "map_id": v.get("map_id"),
                     "kind": v.get("kind")} for v in self.lookup.all()]
        if not target:
            return {"inside": False, "visited": names, "dungeons": dungeons}
        rec = self.record(target) if os.path.exists(self._path(target)) or (self.cur and self.cur["name"] == target)             else {"name": target, "map_id": None, "runs": {}}
        runs = sorted(rec["runs"].values(), key=lambda r: r["start"])
        cur = self.cur if self.cur and self.cur["name"] == target else None
        if cur:
            runs = [r for r in runs if int(r["start"]) != int(cur["start"])]
        look = self.lookup.for_map(rec.get("map_id") or (cur or {}).get("map_id")) or self.lookup.for_name(target)

        # One rule for the two halves of a two-part dungeon, for every page: keep only the points of the half
        # being shown (both on one map are 11,000 yd apart: the route was squeezed into two corners).
        allr = runs + ([cur] if cur else [])
        split = two_halves(allr)
        world = (half == "world") if half else bool(cur and map_place(cur).endswith("(open world)"))
        same = (lambda p: bool(p) and _far(p) == world) if split else (lambda p: bool(p))
        portal = portal_of(look) if world else None
        if portal and not _far(portal):
            portal = None

        # bosses: lookup order first, then anything else we've fought
        order = [b["name"] for b in (look or {}).get("bosses", [])]
        bosses = {}
        for r in runs + ([cur] if cur else []):
            for bn, b in r["bosses"].items():
                agg = bosses.setdefault(bn, {"name": bn, "attempts": 0, "kills": 0, "best": None, "pos": None})
                agg["attempts"] += b["attempts"]
                agg["kills"] += 1 if b["killed"] else 0
                if b["killed"] and b.get("dur") and (agg["best"] is None or b["dur"] < agg["best"]):
                    agg["best"] = b["dur"]
                agg["pos"] = (b.get("pos") if same(b.get("pos")) else None) or agg["pos"]
        boss_list = []
        for bn in order + [b for b in bosses if b not in order]:
            agg = bosses.get(bn, {"name": bn, "attempts": 0, "kills": 0, "best": None, "pos": None})
            if bn not in bosses:
                # boss never engaged yet: if we've seen the mob itself, use where it stood
                for r in runs + ([cur] if cur else []):
                    here = [q for q in (r["mobs"].get(bn) or {}).get("pos") or [] if same(q)]
                    if here:
                        agg["pos"] = here[0]
            killed_now = bool(cur and cur["bosses"].get(bn, {}).get("killed"))
            boss_list.append({**agg, "killed_now": killed_now,
                              "moves": (mob_moves(bn) if mob_moves else [])[:5],
                              "heals": mob_heals(bn) if mob_heals else None,
                              "info": mobinfo(bn) if mobinfo else None})

        # mobs across all runs
        mobs = {}
        for r in runs + ([cur] if cur else []):
            for mn, m in r["mobs"].items():
                if mn in bosses or mn in order:
                    continue
                agg = mobs.setdefault(mn, {"name": mn, "id": m.get("id"), "kills": 0, "seen": 0, "levels": set(), "pos": []})
                agg["kills"] += r["kills"].get(mn, 0)
                agg["seen"] += len(m["guids"])
                agg["levels"].update(m["levels"])
                agg["pos"] = (agg["pos"] + [q for q in m["pos"] if same(q)])[:60]
        mob_list = []
        for mn, agg in sorted(mobs.items(), key=lambda kv: -kv[1]["seen"]):
            moves = mob_moves(mn) if mob_moves else []
            if not agg["kills"] and not any(m["kind"] in ("melee", "damage") for m in moves):
                continue                             # never fought: a friendly NPC or an object
            lv = sorted(agg["levels"])
            mob_list.append({"name": mn, "id": agg["id"], "kills": agg["kills"], "seen": agg["seen"],
                             "levels": f"{lv[0]}-{lv[-1]}" if len(lv) > 1 else (str(lv[0]) if lv else None),
                             "pos": agg["pos"][:20],
                             "moves": (mob_moves(mn) if mob_moves else [])[:4],
                             "heals": mob_heals(mn) if mob_heals else None,
                             "info": mobinfo(mn) if mobinfo else None})

        # direction: from where you are to the next boss not killed this run
        me = me_pos or ((cur["trail"][-1][1:]) if cur and cur["trail"] else None)
        if me and not same(me):
            me = None                                # you're in the other half
        nxt = None
        if cur and me:
            # in the open-world mine every boss is behind the portal: point at the portal
            goals = [("the portal into the instance", portal)] if portal else \
                [(b["name"], b["pos"]) for b in boss_list if not b["killed_now"] and b["pos"]]
            for gname, gpos in goals[:1]:
                dx_n, dy_w = gpos[0] - me[0], gpos[1] - me[1]
                dist = math.hypot(dx_n, dy_w)
                ang = (math.degrees(math.atan2(-dy_w, dx_n)) + 360) % 360      # 0 = north, 90 = east
                nxt = {"name": gname, "yards": round(dist), "bearing": round(ang),
                       "compass": ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((ang + 22.5) // 45) % 8]}

        def half_trail(tr):
            return [p for p in tr if same(p[1:])]

        def entrance():
            """Where this half's route begins: its first point after coming in from the other half (through the
            portal); when nothing is split, the first point of the earliest run."""
            for r in allr:
                tr = r.get("trail") or []
                for i, p in enumerate(tr):
                    if same(p[1:]) and (not split or world or (i and not same(tr[i - 1][1:]))):
                        return p[1:]
            return None
        shown = cur or (runs[-1] if runs else {})

        loot = self.loot_rows([e for r in runs + ([cur] if cur else []) for e in r["loot"]])

        def run_loot(r):
            return [{"t": e[0], "item": e[1], "n": e[2], "who": e[3] if len(e) > 3 else "You",
                     "roll": len(e) > 4 and e[4] == "roll"} for e in r.get("loot", [])]

        n_b = len((look or {}).get("bosses") or [])      # this dungeon's boss count
        def summary(r):
            return {"start": r["start"], "dur": round(run_end(r, n_b) - r["start"]), "cleared": bool(run_end(r, n_b) != r["end"]),
                    "kills": sum(r["kills"].values()),
                    "deaths": len(r["deaths"]), "bosses": sum(1 for b in r["bosses"].values() if b["killed"]),
                    "xp": r.get("xp", 0), "party": r.get("party", []), "members": members(r),
                    "snaps": [s for s in r.get("snaps", []) if s],
                    "loot": len(r["loot"]),
                    "copper": r.get("copper", 0)}

        return {"inside": bool(cur), "name": target, "visited": names, "dungeons": dungeons, "map_id": rec.get("map_id"),
                "lookup": look, "first_visit": not runs,
                "current": summary(cur) if cur else None,
                "runs": [summary(r) for r in runs][-10:][::-1],
                "bosses": boss_list, "mobs": mob_list[:40], "next": nxt, "me": me,
                "open_world": world, "two_halves": split, "portal": portal,
                "map_place": target + (" (open world)" if world else ""),
                "trails": [t_ for t_ in (half_trail(r["trail"]) for r in runs[-3:]) if t_],
                "fights": [f for f in shown.get("fights", []) if not f.get("pos") or same(f["pos"])],
                # where you come in (this half)
                "start": entrance(),
                "deaths": [d for d in self._deaths(shown, roles or {}, recaps or []) if same(d["pos"])],
                "cur_trail": half_trail(cur["trail"]) if cur else [],
                # rolled items (greens/blues) first, then by count
                # most valuable first (vendor price x count); items still being looked up go last
                "loot": loot[:80],
                "loot_value": sum(d["value"] for d in loot),
                "run_loot": run_loot(cur or (runs[-1] if runs else {}))}
