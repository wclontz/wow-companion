#!/usr/bin/env python3
"""WoW Forever second-screen companion.

Tails WoWCombatLog*.txt (and WoWChatLog*.txt if /chatlog is on) and serves a
leveling dashboard at http://127.0.0.1:8765. Read-only: never touches the game.

Combat log facts (verified against the Forever beta client, build 1.60.1):
- Advanced block is 19 fields: guid, owner, hp, maxhp, ap, sp, armor, ?, ?,
  absorb, powerType, power, maxPower, cost, x, y, mapId, facing, level.
- Every melee/spell hit is logged twice: X_DAMAGE (info = source) and
  X_DAMAGE_LANDED (info = target). Count damage from X_DAMAGE only.
- The game flushes the file in chunks: 30 s - 2 min behind real time.
- No XP in the combat log; XP is estimated from mob level unless the chat log
  supplies the real numbers.
"""
import csv
import glob
import json
import math
import os
import re
import sys
import threading
import time
import webbrowser
from collections import defaultdict, deque
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import wowpath

WOW_ROOT = wowpath.wow_root() or wowpath.DEFAULT_ROOT      # found on this PC: settings.json "wow_dir", the registry,
LOG_DIR = wowpath.log_dir()                                # the usual folders, a running Wow.exe (see wowpath.py)
PORT = wowpath.port()
HERE = os.path.dirname(os.path.abspath(__file__))


def map_place(cur):
    """The minimap map a dungeon run draws on (instance.map_place): '<name> (open world)' for Forever's
    open-world version, which logs world yards instead of the instance's own."""
    return (cur or {}).get("map_place") or (cur or {}).get("name")
STATE_FILE = os.path.join(HERE, "companion_state.json")
SESSION_HOURS = 12      # combat logs touched within this window count as "this session"
BREAK_GAP = 600.0       # seconds of log silence treated as a break, not play time
FIGHT_GAP = 6.0         # seconds without combat before a fight is considered over

# Combat log unit flags
MINE, PARTY, RAID = 0x1, 0x2, 0x4
FRIENDLY, NEUTRAL, HOSTILE = 0x10, 0x20, 0x40
T_PLAYER, T_NPC, T_PET, T_GUARDIAN = 0x400, 0x800, 0x1000, 0x2000

# Classic XP needed to go from level N to N+1 (index = N). Forever may differ;
# the chat log calibrates it if enabled.
XP_TABLE = [0, 400, 900, 1400, 2100, 2800, 3600, 4500, 5400, 6500, 7600, 8800, 10100, 11400, 12900,
            14400, 16000, 17700, 19400, 21300, 23200, 25200, 27300, 29400, 31700, 34000, 36400, 38900,
            41400, 44300, 47400, 50800, 54500, 58600, 62800, 67100, 71600, 76100, 80800, 85700, 90700,
            95800, 101000, 106300, 111800, 117500, 123200, 129100, 135100, 141200, 147500, 153900,
            160400, 167100, 173900, 180800, 187900, 195000, 202300, 209800]
MAX_LEVEL = 60

DAMAGE_EVENTS = {"SWING_DAMAGE", "SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE", "RANGE_DAMAGE",
                 "ENVIRONMENTAL_DAMAGE", "DAMAGE_SHIELD", "DAMAGE_SPLIT"}
HEAL_EVENTS = {"SPELL_HEAL", "SPELL_PERIODIC_HEAL"}
ADV_SUFFIXES = ("_DAMAGE", "_DAMAGE_LANDED", "_HEAL", "_ENERGIZE", "_DRAIN", "_LEECH")
REST_WORDS = ("drink", "food", "refreshment")
# buff durations in seconds (Classic values). SELF_BUFFS = ones you should keep up on yourself.
BUFF_DUR = {"Power Word: Fortitude": 1800, "Inner Fire": 600, "Divine Spirit": 1800, "Shadow Protection": 600,
            "Mark of the Wild": 1800, "Arcane Intellect": 1800, "Blessing of Might": 300,
            "Blessing of Wisdom": 300, "Blessing of Kings": 300, "Thorns": 600, "Well Fed": 900,
            "Demon Skin": 1800, "Demon Armor": 1800}
SELF_BUFFS = ("Power Word: Fortitude", "Inner Fire", "Divine Spirit", "Shadow Protection",
              "Demon Skin", "Demon Armor")
BETTER_BUFF = {"Demon Skin": "Demon Armor"}      # the one that replaces it once you use it: one reminder, not two
GROUP_BUFFS = ("Power Word: Fortitude", "Divine Spirit", "Shadow Protection")

TS_RE = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{4}))? (\d{1,2}):(\d{2}):(\d{2})\.(\d{3})")


def parse_ts(s):
    m = TS_RE.match(s)
    if not m:
        return None
    mo, d, y, hh, mm, ss, ms = m.groups()
    now = datetime.now()
    yy = int(y) if y else now.year
    try:
        dt = datetime(yy, int(mo), int(d), int(hh), int(mm), int(ss), int(ms) * 1000)
        if not y and dt > now + timedelta(days=2):
            dt = dt.replace(year=yy - 1)         # chat lines carry no year: in January, December isn't the future
        return dt.timestamp()
    except ValueError:
        return None


def flag(s):
    try:
        return int(s, 16)
    except (ValueError, TypeError):
        return 0


def num(s, default=0):
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return default


def ffloat(s, default=None):
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def is_guid(s):
    return s.startswith(("Player-", "Creature-", "Pet-", "Vehicle-", "GameObject-")) or s == "0000000000000000"


def short_name(n):
    return n.split("-")[0] if n else n


# ---------------------------------------------------------------- XP formula (Classic)

def zero_diff(lvl):
    for top, zd in ((7, 5), (9, 6), (11, 7), (15, 8), (19, 9), (29, 11), (39, 12), (44, 13),
                    (49, 14), (54, 15), (59, 16)):
        if lvl <= top:
            return zd
    return 17


def gray_level(lvl):
    if lvl <= 5:
        return 0
    if lvl <= 39:
        return lvl - lvl // 10 - 5
    if lvl <= 59:
        return lvl - lvl // 5 - 1
    return lvl - 9


def kill_xp(char_lvl, mob_lvl):
    if not char_lvl or not mob_lvl or mob_lvl <= 0:
        return 0
    base = char_lvl * 5 + 45
    if mob_lvl >= char_lvl:
        xp = base * (1 + 0.05 * min(mob_lvl - char_lvl, 4))
    elif mob_lvl <= gray_level(char_lvl):
        xp = 0
    else:
        xp = base * (1 - (char_lvl - mob_lvl) / zero_diff(char_lvl))
    return max(0, round(xp))


XP_SEEN = {}            # level -> XP the level needs, as the game itself says (the add-on's strip): beats the table


def xp_needed(lvl):
    return XP_SEEN.get(lvl) or (XP_TABLE[lvl] if 0 < lvl < len(XP_TABLE) else 0)


def strip_summary():
    """For the pages: what the add-on's strip is giving right now (None: no strip, or an old add-on)."""
    try:
        import beacon
        d = beacon.latest()
        st = d.get("stats") if d else None
        if not st:
            return None
        tx = d.get("texts") or {}
        ql = beacon.quests(d)
        return {"version": d.get("version"), "level": st["level"], "xp": st["xp"], "xpmax": st["xpmax"], "rested": st["rested"],
                "money": st["money"], "bag_free": st["bag_free"], "bag_all": st["bag_all"], "wear": st["wear"],
                "combat": st["combat"], "resting": st["resting"], "group": st["group"],
                "health_known": st["hpmax"] is not None, "power_known": st["powermax"] is not None,
                "label": tx.get("label"), "zone": tx.get("zone"), "target": tx.get("target"),
                "target_level": st["target_level"], "party": beacon.party(d), "gear": beacon.gear(d),
                "minimap": beacon.minimap(d),
                "money_trend": money_trend(tx.get("name"), st["money"]),
                "quests": st["quests"], "quests_read": None if ql is None else len(ql)}
    except Exception:
        return None


MONEY_SEEN = {}         # character -> (time, copper) at its first strip reading since the companion started


def money_trend(name, money, now=None):
    """Your money against what this character had when the companion first saw it: {"since", "net", "per_hour"}
    (copper; net = looted + sold - spent, so a repair or a trainer shows as a loss). per_hour is None for the
    first 5 minutes (one sale would read as a fortune an hour). None without a name or the money."""
    if not name or money is None:
        return None
    now = now or time.time()
    t0, m0 = MONEY_SEEN.setdefault(name, (now, money))
    net = money - m0
    return {"since": t0, "net": net, "per_hour": round(net / (now - t0) * 3600) if now - t0 >= 300 else None}


def gdi_handles():
    """GDI handles the companion holds (normal: under 20; at 10,000 Windows refuses every screen grab)."""
    try:
        import fastgrab
        return fastgrab.gdi_count()
    except Exception:
        return None


def beacon_stats(max_age=1.5):
    """Your numbers from the add-on's strip (level, XP, health, power...: beacon.py row 2), or None when the
    strip isn't being read (add-on off or old, loading screen): then the bars and the portrait are read."""
    try:
        import beacon
        b = beacon.latest(max_age)
        st = b.get("stats") if b else None
        return st if st and st.get("level") else None       # (xp / hp / power are each None when the game hides them)
    except Exception:
        return None


# ---------------------------------------------------------------- chat log patterns

CHAT_KILL_XP = re.compile(r"^(.+?) dies, you gain ([\d,]+) experience(?:.*?\(\+([\d,]+) exp Rested bonus\))?", re.I)
# (a quest turn-in writes "Experience gained: 40." AND "You gain 40 experience.": matching both counted it twice)
CHAT_XP = re.compile(r"You gain ([\d,]+) experience", re.I)
CHAT_LEVEL = re.compile(r"Congratulations, you have reached level (\d+)", re.I)   # (not anyone's chat about a level)
CHAT_LOOT = re.compile(r"^You (?:receive|create) (?:loot|item): (.+?)(?:x(\d+))?\.?$", re.I)
# party loot: "Kaylva Darkoak receives loot: Linen Clothx2."  roll results: "Loot: Kwoki Keno won: Malachite"
CHAT_PARTY_LOOT = re.compile(r"^(.+?) receives loot: (.+?)(?:x(\d+))?\.?$", re.I)
CHAT_ROLL_WON = re.compile(r"^Loot: (.+?) won: (.+?)\.?$", re.I)
# notes to Claude: type in a private channel (/join companionnotes, then /<its number> text); its name is
# "notes_channel" in settings.json
NOTES_CHANNEL = str(wowpath.settings().get("notes_channel") or "companionnotes")
CHAT_NOTE = re.compile(r"^\[\d+\. " + re.escape(NOTES_CHANNEL) + r"\] [^:]+: (.+)$", re.I)
CHAT_MONEY = re.compile(r"^(?:You loot|Your share of the loot is) (.+)", re.I)
MONEY_PART = re.compile(r"(\d+) (Gold|Silver|Copper)", re.I)
LINK_RE = re.compile(r"\|c\w{8}\|H[^|]+\|h\[([^\]]+)\]\|h\|r|\[([^\]]+)\]")


_WOW_START = {}


def wow_started():
    """When WoW (Wow.exe for Forever, WowB.exe for the old beta) started, as a timestamp (asked once per WoW process)."""
    import subprocess
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-Process Wow,WowB -ErrorAction SilentlyContinue | Sort-Object StartTime | Select-Object -First 1)."
                              "StartTime.ToUniversalTime().ToString('o')"],
                             capture_output=True, text=True, timeout=15, creationflags=0x08000000).stdout.strip()
        if not out:
            return None
        if out not in _WOW_START:
            from datetime import timezone
            _WOW_START[out] = datetime.fromisoformat(out.replace("Z", "+00:00")[:26] + "+00:00"
                                                     if "+" not in out[19:] else out).astimezone(timezone.utc).timestamp()
        return _WOW_START[out]
    except Exception:
        return None


def logs_state(comp):
    """Are the game's logs being written? WoW turns /combatlog and /chatlog off at every logout."""
    def age(pattern):
        fs = glob.glob(os.path.join(LOG_DIR, pattern))
        return round(time.time() - max(map(os.path.getmtime, fs))) if fs else None
    ca, ha = age("WoWCombatLog*.txt"), age("WoWChatLog*.txt")
    try:
        playing = bool(SCREEN is not None and SCREEN.snapshot().get("ok"))     # in game, not at the login screen
    except Exception:
        playing = False
    # /combatlog starts a NEW file, and logging only stops at logout. So a combat log file created after
    # you came into the game = it's on, however quiet (WoW holds it in a buffer out of combat).
    since = getattr(comp, "ingame_since", None)
    fs = glob.glob(os.path.join(LOG_DIR, "WoWCombatLog*.txt"))
    newest_created = max(map(os.path.getctime, fs)) if fs else 0
    on_since_login = bool(since and newest_created >= since - 5)
    # ...and so does a file WRITTEN since then. ("Back in game" is also set after a long loading screen or
    # 30 s on another window, which isn't a logout: the warning came up in the Deadmines twice on 2026-11-16
    # while the game was only holding the log back.)
    newest_written = max(map(os.path.getmtime, fs)) if fs else 0
    written_since = bool(since and newest_written >= since - 5)
    # A quiet log alone proves little: in a dungeon the game writes it in chunks 4-5 minutes apart. So: 7
    # quiet minutes with no sign it's on - or the sure sign: the XP bar moved (a kill) and 7 minutes later the
    # log still hasn't been written.
    QUIET = 420
    off = []
    kill_unlogged = False
    try:
        jt = comp.scr_jumps[-1][0] if comp.scr_jumps else 0
        kill_unlogged = bool(jt and jt > newest_written + 5 and time.time() - jt > QUIET)
    except Exception:
        pass
    if playing and (kill_unlogged or (not on_since_login and not written_since and (ca is None or ca > QUIET))):
        off.append("/combatlog")
        # WoW holds the chat log in a buffer and writes it in chunks, so a quiet chat log on its own
        # means nothing. Both turn off at logout, so only flag it alongside a quiet combat log.
        if ha is None or ha > QUIET:
            off.append("/chatlog")
    return {"combat_age": ca, "chat_age": ha, "off": off, "on_since_login": on_since_login or written_since}


def money_to_copper(text):
    total = 0
    for amt, unit in MONEY_PART.findall(text):
        total += int(amt) * {"gold": 10000, "silver": 100, "copper": 1}[unit.lower()]
    return total


def strip_links(s):
    return LINK_RE.sub(lambda m: m.group(1) or m.group(2), s).strip()


# ---------------------------------------------------------------- the model

class Companion:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()
        self.baselines = self._load_state()
        self.screen = None                   # latest ScreenReader state
        self.ocr_target = None               # target name read off the target frame
        self.portrait_level = None           # level read off the player portrait (truth)
        self.scr_last_xp = None              # (level, xp) last seen on the XP bar
        self.scr_seen_levelups = 0
        self.scr_jumps = []                  # [t, xp_gained] from XP bar changes
        self.scr_series = []                 # [t, cumulative gained] since baseline
        self.jump_names = {}                 # jump t -> quest name (XP that wasn't from a kill)
        self.scr_pending = None              # XP-bar value waiting for a second matching read
        self.last_refresh_t = 0              # when the user last pressed quest Refresh

    def reset(self):
        self.me = None
        self.me_name = None
        self.lines = 0
        self.first_t = None
        self.last_t = None
        self.segments = []                   # [start, end] of continuous play
        self.zone = None
        self.build = None
        self.cur_map = None
        self.map_bounds = {}                 # map name -> (north X, south X, west Y, east Y)
        self.units = {}                      # guid -> {name, level, hp, maxhp}
        self.player = {"hp": None, "maxhp": None, "power": None, "maxpower": None, "ptype": 0,
                       "absorb": 0, "x": None, "y": None, "map": None}
        self.trail = []                      # [t, x, y]
        self.distance = 0.0
        self.timeline = []                   # [t, hp%, mana%]
        self.my_auras = {}                   # name -> applied t
        self.rest_start = None
        self.rest_time = 0.0
        self.rest_count = 0
        self.dmg_done = 0
        self.dmg_taken = 0
        self.heal_done = 0
        self.overheal = 0
        self.heal_taken = 0
        self.shield_absorbed = 0
        self.spells = defaultdict(lambda: {"casts": 0, "dmg": 0, "heal": 0, "over": 0, "mana": 0,
                                           "hits": 0, "crits": 0, "max": 0})
        self.taken_by = defaultdict(lambda: {"dmg": 0, "hits": 0, "max": 0})
        self.failed = defaultdict(int)
        self.biggest_hit = None              # {amt, spell, target, crit, t}
        self.biggest_taken = None
        self.tagger = {}                     # mob guid -> True if I/party tagged first
        self.damaged_by_me = set()
        self.kills = []                      # {t, guid, name, level, xp, x, y}
        self.killed_guids = set()
        self.deaths = []                     # {t, x, y, by}
        self.last_hit_by = None
        self.last_target = None
        self.on_me = {}                      # mob guid -> (last time it hit me, name)
        self.my_removed = {}                 # buff -> when the log saw it come off
        self.cast_by = {}                    # character name -> the spells that character cast (buff reminders)
        self.stolen = 0
        self.fight = None
        self.pre_pull_mana = 0
        self.cast_start = {}                 # spell -> t of my last SPELL_CAST_START
        # spell -> {casts, cast_time, mana, by_mob: {mob: {casts, dmg, hits, crits}}}
        # mob type -> move -> {n, dmg, max, crits, kind}; learned from fights against any player
        from group import GroupTracker
        self.grp = GroupTracker()
        from instance import InstanceTracker
        self.inst = InstanceTracker()
        self.mob_moves = defaultdict(lambda: defaultdict(lambda: {"n": 0, "dmg": 0, "max": 0, "crits": 0,
                                                                  "kind": "", "absorbed": 0}))
        self.mob_heals = defaultdict(lambda: defaultdict(lambda: {"n": 0, "amt": 0, "casts": 0}))
        self.spell_eff = defaultdict(lambda: {"casts": 0, "cast_time": 0.0, "timed": 0, "mana": 0,
                                              "heal_eff": 0, "heal_over": 0, "heal_hits": 0,
                                              "by_mob": defaultdict(lambda: {"casts": 0, "dmg": 0, "hits": 0,
                                                                             "crits": 0})})
        self.fights = deque(maxlen=300)
        self.combat_time = 0.0
        self.lowest_hp_pct = None            # closest call (survived)
        self.lowest_hp_info = None
        self.players_seen = {}
        self.hostiles_seen = {}
        self.feed = deque(maxlen=40)
        self._dinged = set()
        # mob guid -> this fight: attacks (pace), its damage to you, both sides' attacks + misses
        self.duels = defaultdict(lambda: {"me": 0, "it": 0, "all": 0, "dmg": 0, "name": None, "t": 0,
                                          "m_att": 0, "m_miss": 0, "i_att": 0, "i_miss": 0})
        self.mob_fights = defaultdict(list)       # mob name -> finished fights (kept for good in mobfights.jsonl)
        self._mf_seen = set()
        self._load_mob_fights()
        self.mobstats = defaultdict(lambda: {"maxhp": 0, "my_dmg": defaultdict(lambda: [0, 0]),
                                             "their_dmg": 0, "their_hits": 0, "last": 0})
        # chat log
        self.chat_active = False
        self.chat_xp = []                    # {t, xp, rested, mob}
        self.chat_levels = []                # {t, level}
        self.loot = defaultdict(int)
        self.copper = 0

    # -- persistence of the level/xp baseline the user types in
    def _load_state(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.baselines, f, indent=2)
        except OSError:
            pass

    def set_baseline(self, level, xp):
        with self.lock:
            if not self.me_name:
                return False
            self.baselines[self.me_name] = {"level": int(level), "xp": int(xp), "t": time.time()}
            self._save_state()
            self.scr_last_xp, self.scr_series, self.scr_jumps = None, [], []
            if self.screen:
                self.scr_seen_levelups = self.screen.get("level_ups", 0)
            return True

    def update_screen(self, st):
        """Called ~2x/s with the ScreenReader state; turns XP-bar fill into exact XP."""
        with self.lock:
            self.screen = st
            now_ = time.time()
            if MAPPER is not None:                       # the dungeon capture skips anything not the game screen
                if st and st.get("ok"):                  # (Forever's loading screens are artwork, not black:
                    MAPPER.screen_ok_t = now_            # one got saved as a Deadmines map piece three times)
                else:
                    MAPPER.screen_bad_t = now_
            if st and st.get("ok"):
                if getattr(self, "_scr_bad_since", None) and now_ - self._scr_bad_since > 30:
                    self.ingame_since = now_             # back in game after the login screen / a loading gap
                    try:                                 # (kept, so a companion restart keeps this login)
                        with open(os.path.join(HERE, "login.json"), "w") as _f:
                            json.dump({"t": now_}, _f)
                    except OSError:
                        pass
                elif getattr(self, "ingame_since", None) is None:
                    ws = wow_started() or now_           # companion started mid-game: this WoW run's last login
                    try:
                        with open(os.path.join(HERE, "login.json"), encoding="utf-8") as _f:
                            lt = json.load(_f).get("t") or 0
                    except (OSError, ValueError):
                        lt = 0
                    self.ingame_since = lt if lt >= ws else ws
                self._scr_bad_since = None
            elif getattr(self, "_scr_bad_since", None) is None:
                self._scr_bad_since = now_
            if not st or not st.get("ok"):
                return
            base = self.baselines.get(self.me_name) if self.me_name else None
            if not base:
                return
            # the add-on's strip gives level and XP as the game has them: nothing to measure off the bar
            ex = st.get("exact")
            if ex:
                XP_SEEN[ex["level"]] = ex["xpmax"]
                self.portrait_level = ex["level"]
            if bool(ex) != getattr(self, "_xp_exact", False):
                self._xp_exact = bool(ex)
                self.scr_last_xp, self.scr_pending = None, None   # bar <-> strip: the few XP between them aren't a gain
            pl = self.portrait_level
            if pl and pl != base["level"]:
                if pl == base["level"] + 1:
                    self.push(time.time(), "level", f"DING! Level {pl}")
                base["level"], base["xp"], base["t"] = pl, (ex["xp"] if ex else round(st["xp"] * xp_needed(pl))), time.time()
                self._save_state()
                last = self.scr_last_xp
                self.scr_last_xp = None              # restart XP tracking at the right level
                if ex and last and last[0] == pl - 1 and xp_needed(last[0]) > last[1]:
                    # the strip knows both sides of the ding: the XP that carried you over it counts too
                    gain = xp_needed(last[0]) - last[1] + ex["xp"]
                    now = st.get("t", time.time())
                    self.scr_jumps.append([now, gain])
                    self.inst.add_xp(gain)
                    self.scr_series.append([now, (self.scr_series[-1][1] if self.scr_series else 0) + gain])
                    self.scr_last_xp = (pl, ex["xp"])
                self.scr_seen_levelups = st["level_ups"]
            ups = st["level_ups"] - self.scr_seen_levelups
            if pl:
                ups = 0                              # the portrait is the truth; it will move the level
            if ups > 0:
                self.scr_seen_levelups = st["level_ups"]
                base["level"] = min(MAX_LEVEL, base["level"] + ups)
                base["xp"], base["t"] = 0, time.time()
                self._save_state()
                self.push(time.time(), "level", f"DING! Level {base['level']}")
            lvl = base["level"]
            xp = ex["xp"] if ex else round(st["xp"] * xp_needed(lvl))
            now = st.get("t", time.time())
            if self.scr_last_xp is None:
                if not self.scr_series:
                    self.scr_series = [[now, 0]]
                self.scr_last_xp = (lvl, xp)
                return
            plvl, pxp = self.scr_last_xp
            if (lvl, xp) == (plvl, pxp):
                self.scr_pending = None              # back to the old value: the odd frame wasn't "seen twice in a row"
                return
            gain = (xp - pxp) if lvl == plvl else (xp_needed(plvl) - pxp) + xp
            if not ex:                              # (the strip is exact and checksummed: every XP counts at once)
                px = xp_needed(lvl) / 1180          # XP per pixel of bar: ignore jitter below ~1.5 px
                if lvl == plvl and gain < px * 1.5:
                    return                               # jitter, or the bar was hidden for a moment
                # a jump has to be seen twice in a row before it counts (one bad frame can't add XP)
                if self.scr_pending != (lvl, xp):
                    self.scr_pending = (lvl, xp)
                    return
            self.scr_pending = None
            self.scr_last_xp = (lvl, xp)
            if gain > 0:
                self.scr_jumps.append([now, gain])
                self.inst.add_xp(gain)
                cum = (self.scr_series[-1][1] if self.scr_series else 0) + gain
                self.scr_series.append([now, cum])

    def quests_refreshed(self, old, new):
        """Called after the user presses Refresh: quests that vanished since the last refresh were
        turned in, so they name the quest-XP jumps that happened in between."""
        gone = [o for o in old if not any(SequenceMatcher(None, o.lower(), n.lower()).ratio() > 0.7 for n in new)]
        now = time.time()
        with self.lock:
            since = self.last_refresh_t
            self.last_refresh_t = now
            if not gone:
                return
            kill_ts = [k["t"] for k in self.kills]
            jumps = [jt for jt, amt in self.scr_jumps
                     if amt >= 40 and jt >= since and jt not in self.jump_names
                     and not any(-1.5 <= jt - kt <= 4 for kt in kill_ts)]
            if len(jumps) == len(gone) == 1:
                self.jump_names[jumps[0]] = gone[0]
            else:
                for jt in jumps:
                    self.jump_names[jt] = " / ".join(gone)

    def screen_live(self):
        st = self.screen
        return bool(st and st.get("ok") and time.time() - st.get("t", 0) < 3)

    def push(self, t, kind, text):
        self.feed.appendleft({"t": t, "kind": kind, "text": text})

    # ------------------------------------------------------------ combat log
    def feed_combat(self, line):
        if "  " not in line:
            return
        ts_s, rest = line.split("  ", 1)
        t = parse_ts(ts_s)
        if t is None:
            return
        try:
            c = next(csv.reader([rest]))
        except (csv.Error, StopIteration):
            return
        if not c:
            return
        ev = c[0]
        self.lines += 1
        if self.first_t is None:
            self.first_t = t
        # play segments: a log silence over BREAK_GAP is a break (logged out, AFK)
        if not self.segments or t - self.segments[-1][1] > BREAK_GAP:
            self.segments.append([t, t])
        elif t > self.segments[-1][1]:
            self.segments[-1][1] = t
        self.last_t = t

        if ev == "COMBAT_LOG_VERSION":
            if "BUILD_VERSION" in c:
                self.build = c[c.index("BUILD_VERSION") + 1]
            return
        if ev == "ZONE_CHANGE" and len(c) > 2:
            self.zone = c[2]
            if c[1].lstrip("-").isdigit():
                self.last_zone = (t, int(c[1]), c[2])     # (log time, instance id: 0 = open world, name)
            self.inst.me = self.me
            if c[1].lstrip("-").isdigit():
                self.inst.on_zone(t, int(c[1]), c[2], int(c[3]) if len(c) > 3 and c[3].isdigit() else 1)
            return
        if ev in ("ENCOUNTER_START", "ENCOUNTER_END"):
            self.inst.on_encounter(t, ev, c)
            return
        if ev == "MAP_CHANGE" and len(c) >= 7:
            # MAP_CHANGE,uiMapID,name,north X,south X,west Y,east Y
            self.cur_map = c[2]
            self.cur_map_t = t                       # log time: the coords under the minimap use this map from then
            mc = getattr(self, "map_changes", None)
            if mc is None:
                mc = self.map_changes = []
            mc.append((t, c[2]))                     # (event time, map): matched against coords jumps
            del mc[:-200]
            self.map_bounds[c[2]] = tuple(float(v) for v in c[3:7])
            try:                                     # world position of every zone map: for the seamless map
                from location import CONTINENTS
                if c[2].strip().lower() not in CONTINENTS:
                    import quadmap
                    quadmap.note_bounds(c[2], [float(v) for v in c[3:7]])
                    if c[1].isdigit():
                        quadmap.note_map_id(int(c[1]), c[2])     # (the position add-on names zones by this id)
            except Exception:
                pass
            return
        if len(c) < 9:
            return

        sg, sn, sf = c[1], ("" if c[2] == "nil" else c[2]), flag(c[3])    # (no source: fire, a fall - not a mob called "nil")
        dg, dn, df = c[5], c[6], flag(c[7])

        # who you are: the player the log marks as yours. Another one later = you switched characters.
        for g, n, f in ((sg, sn, sf), (dg, dn, df)):
            if g != self.me and g.startswith("Player-") and f & MINE and f & T_PLAYER and short_name(n):
                if self.me is not None:                      # the buffs seen on / off were the other character's
                    self.my_auras, self.my_removed = {}, {}
                self.me, self.me_name = g, short_name(n)
                break

        for g, n, f in ((sg, sn, sf), (dg, dn, df)):
            if f & T_PLAYER and g != self.me and g.startswith("Player-"):
                nm = short_name(n)
                self.players_seen[nm] = t
                if f & HOSTILE:
                    prev = self.hostiles_seen.get(nm)
                    if prev is None or t - prev > 300:
                        self.push(t, "hostile", f"Hostile player nearby: {nm}")
                    self.hostiles_seen[nm] = t

        if ev == "UNIT_DIED":
            self.grp.me = self.me
            self.grp.on_event(t, ev, c, sg, sn, sf, dg, dn, df, "", 9, 9, False)
            self.inst.on_event(t, ev, c, sg, sn, sf, dg, dn, df, None)
            self._unit_died(t, dg, dn, df)
            return
        if ev == "PARTY_KILL":
            return  # handled through UNIT_DIED + tagging

        if ev.startswith("SWING"):
            pre, spell = 0, "Melee"
        elif ev.startswith("ENVIRONMENTAL"):
            # (advanced logging puts the 19-field block first: the type - Fire, Falling... - comes after it)
            pre, spell = 1, (c[28] if len(c) > 28 and is_guid(c[9]) else c[9] if len(c) > 9 else "Environment").title()
        elif ev.startswith(("SPELL", "RANGE", "DAMAGE_")):
            pre, spell = 3, (c[10] if len(c) > 10 else "?")
        else:
            pre, spell = 0, "?"
        a = 9 + pre

        s = a
        if ev.endswith(ADV_SUFFIXES) or ev in ("SPELL_CAST_SUCCESS", "DAMAGE_SHIELD", "DAMAGE_SPLIT"):
            if len(c) >= a + 19 and is_guid(c[a]):
                for g, n in ((sg, sn), (dg, dn)):
                    if g.startswith(("Creature-", "Vehicle-")):
                        self.units.setdefault(g, {"name": n})["name"] = n
                self._advanced(t, c, a)
                s = a + 19

        if MOBS is not None:
            for g_, n_ in ((sg, sn), (dg, dn)):
                if g_.startswith("Creature-"):
                    MOBS.note_guid(g_, n_)
        self.grp.me = self.me
        try:
            self.grp.on_event(t, ev, c, sg, sn, sf, dg, dn, df, spell, a, s, s == a + 19)
        except (ValueError, IndexError):
            pass
        if self.inst.cur is not None:
            self.inst.me = self.me
            try:
                self.inst.on_event(t, ev, c, sg, sn, sf, dg, dn, df, a if s == a + 19 else None, spell)
            except (ValueError, IndexError):
                pass
        src_mine = bool(sf & MINE) or sg == self.me
        dst_me = dg == self.me and dg is not None
        if (ev.endswith("_DAMAGE") or ev.endswith("_MISSED")) and "PERIODIC" not in ev and not ev.endswith("_LANDED"):
            # miss chance: MISS / DODGE / PARRY / BLOCK / RESIST ... (a full ABSORB by a shield isn't a miss)
            missed = False
            if ev.endswith("_MISSED"):
                mt = c[9] if ev.startswith("SWING") else (c[12] if len(c) > 12 else "")
                missed = mt not in ("ABSORB", "")
                if mt == "ABSORB":
                    missed = None                    # neither hit nor miss for the chance
            if missed is not None:
                if sg == self.me and df & T_NPC and dg:
                    d = self.duels[dg]
                    d["m_att"] += 1
                    d["m_miss"] += 1 if missed else 0
                    d["t"] = t
                elif dst_me and sf & T_NPC and not sf & FRIENDLY and sg:
                    d = self.duels[sg]
                    d["i_att"] += 1
                    d["i_miss"] += 1 if missed else 0
                    d["t"] = t
            if (sf & T_PLAYER or sf & (T_PET | T_GUARDIAN)) and df & T_NPC and dg:
                self.duels[dg]["all"] += 1           # everyone's attacks on it (is this really your 1-on-1?)
            if sg == self.me and df & T_NPC and dg:
                d = self.duels[dg]
                d["me"] += 1
                d["name"] = dn
                d["t"] = t
            elif dst_me and sf & T_NPC and not sf & FRIENDLY and sg:
                d = self.duels[sg]
                d["it"] += 1
                d["name"] = sn
                d["t"] = t
                if ev.endswith("_DAMAGE") and len(c) > s + 6:
                    d["dmg"] += num(c[s]) + num(c[s + 6])      # what it did to you (absorbed by a shield counts)
                elif ev.endswith("_MISSED"):                   # a hit your shield took whole is logged as a miss
                    i_ = 11 if ev.startswith("SWING") else 14
                    if len(c) > i_ and c[i_ - 2] == "ABSORB":
                        d["dmg"] += num(c[i_])

        if ev in DAMAGE_EVENTS and len(c) > s + 7:
            amt, over_k, absorbed = num(c[s]), num(c[s + 2]), num(c[s + 6])
            crit = c[s + 7] == "1"
            if src_mine and df & T_NPC and sg == self.me:
                bm = self.spell_eff[spell]["by_mob"][dn]
                bm["dmg"] += amt
                bm["hits"] += 1
                bm["crits"] += crit
            if src_mine and df & T_NPC:
                self._touch(t, dg, dn)
                self.mobstats[dn]["last"] = max(self.mobstats[dn]["last"], t)
                self.damaged_by_me.add(dg)
                self.last_target = dg
                self.dmg_done += amt
                sp = self.spells[spell]
                sp["dmg"] += amt
                sp["hits"] += 1
                sp["crits"] += crit
                sp["max"] = max(sp["max"], amt)
                if self.biggest_hit is None or amt > self.biggest_hit["amt"]:
                    if self.biggest_hit is not None and amt > 0:
                        self.push(t, "record", f"New biggest hit: {spell} {amt}{' (crit)' if crit else ''} on {dn}")
                    self.biggest_hit = {"amt": amt, "spell": spell, "target": dn, "crit": crit, "t": t}
                if self.fight:
                    self.fight["dealt"] += amt
                if amt > 0 and ev != "SPELL_PERIODIC_DAMAGE":
                    md = self.mobstats[dn]["my_dmg"][spell]
                    md[0] += amt
                    md[1] += 1
            if sf & T_NPC and df & T_PLAYER and not sf & FRIENDLY and sn:
                mv = self.mob_moves[sn][spell]
                if mv["kind"] in ("", "cast", "debuff", "buff"):   # it does damage: that's what it is
                    mv["kind"], mv["n"] = ("melee" if spell == "Melee" else "damage"), 0
                mv["n"] += 1
                mv["dmg"] += amt + absorbed
                mv["max"] = max(mv["max"], amt + absorbed)
                mv["crits"] += crit
            if (sf & T_PLAYER or sf & (T_PET | T_GUARDIAN)) and df & T_NPC and dg not in self.tagger:
                self.tagger[dg] = bool(sf & (MINE | PARTY | RAID)) or sg == self.me
            if dst_me:
                self._touch(t, sg, sn)
                if sf & T_NPC and not sf & FRIENDLY:
                    self.on_me[sg] = (t, sn)
                if sf & T_NPC:
                    self.mobstats[sn]["last"] = max(self.mobstats[sn]["last"], t)
                total = amt + absorbed
                self.dmg_taken += amt
                tb = self.taken_by[sn or spell]
                tb["dmg"] += amt
                tb["hits"] += 1
                tb["max"] = max(tb["max"], total)
                self.last_hit_by = sn or spell
                if self.biggest_taken is None or total > self.biggest_taken["amt"]:
                    self.biggest_taken = {"amt": total, "src": sn or spell, "spell": spell, "t": t}
                if self.fight:
                    self.fight["taken"] += amt
                if sf & T_NPC and total > 0:
                    ms = self.mobstats[sn]
                    ms["their_dmg"] += total
                    ms["their_hits"] += 1
            return

        if ev in HEAL_EVENTS and len(c) > s + 2:
            amt, over = num(c[s]), num(c[s + 2])
            if sg == self.me:
                se = self.spell_eff[spell]
                se["heal_eff"] += amt - over
                se["heal_over"] += over
                se["heal_hits"] += 1
            if sf & T_NPC and not sf & (MINE | PARTY | RAID) and not sf & FRIENDLY and amt > 0:
                mh = self.mob_heals[sn][spell]          # a hostile/neutral mob healed itself or a friend
                if mh["n"] == 0:
                    self.push(t, "hostile", f"{sn} can heal: {spell}")
                mh["n"] += 1
                mh["amt"] += amt
                mv = self.mob_moves[sn][spell]
                mv["kind"], mv["n"] = "heal", mv["n"] + 1
                mv["dmg"] += amt
                mv["max"] = max(mv["max"], amt)
            if src_mine:
                self.heal_done += amt
                self.overheal += over
                sp = self.spells[spell]
                sp["heal"] += amt
                sp["over"] += over
                sp["hits"] += 1
                if self.fight:
                    self.fight["healed"] += amt - over
            if dst_me:
                self.heal_taken += amt - over
            return

        if ev == "SPELL_ABSORBED" and len(c) >= 19:
            if c[-10] == self.me or flag(c[-8]) & MINE:
                amt = num(c[-3])
                self.shield_absorbed += amt
                self.spells[c[-5]]["heal"] += amt      # a shield's absorb is healing that landed
                self.spell_eff[c[-5]]["heal_eff"] += amt
            return

        if ev == "SPELL_CAST_START" and sg == self.me:
            self.cast_start[spell] = t
            return
        if ev == "SPELL_CAST_START" and sf & T_NPC and not sf & FRIENDLY and \
                re.search(r"heal|mend|renew|rejuv|regrowth|restor|flash of light|holy light", spell, re.I):
            self.mob_heals[sn][spell]["casts"] += 1
            return

        if ev == "SPELL_CAST_SUCCESS" and src_mine and sg == self.me:
            sp = self.spells[spell]
            sp["casts"] += 1
            self.cast_by.setdefault(short_name(sn), set()).add(spell)
            se = self.spell_eff[spell]
            se["casts"] += 1
            st0 = self.cast_start.pop(spell, None)
            se["cast_time"] += (t - st0) if st0 is not None and 0 < t - st0 < 10 else 0.0
            se["timed"] += 1
            if df & T_NPC and dn:
                se["by_mob"][dn]["casts"] += 1
            if len(c) >= a + 19 and c[a] == self.me:
                cost = num(c[a + 13])
                se["mana"] += cost
                sp["mana"] += cost
                if self.fight is not None and t - self.fight["last"] <= FIGHT_GAP:
                    self.fight["mana"] += cost
                else:
                    self.pre_pull_mana += cost      # opener cast before the first hit lands
            return

        if ev == "SPELL_CAST_FAILED" and sg == self.me:
            self.failed[f"{spell}: {c[-1]}"] += 1
            return

        if ev == "SPELL_AURA_APPLIED" and sf & T_NPC and not sf & FRIENDLY and sn:
            aura_type = c[-1] if c[-1] in ("BUFF", "DEBUFF") else (c[-2] if len(c) > 2 else "")
            mv = self.mob_moves[sn][spell]
            mv["n"] += 1 if mv["kind"] in ("", "debuff", "buff") else 0
            if not mv["kind"] or mv["kind"] in ("debuff", "buff"):
                mv["kind"] = "debuff" if df & T_PLAYER or aura_type == "DEBUFF" else "buff"
        if ev == "SPELL_CAST_SUCCESS" and sf & T_NPC and not sf & FRIENDLY and sn and spell not in self.mob_moves[sn]:
            self.mob_moves[sn][spell]["kind"] = "cast"
        if ev == "SPELL_CAST_SUCCESS" and sf & T_NPC and not sf & FRIENDLY and sn and \
                self.mob_moves[sn][spell]["kind"] == "cast":
            self.mob_moves[sn][spell]["n"] += 1
        if ev == "SPELL_AURA_REFRESH" and dst_me:
            self.my_auras[spell] = t
            return
        if ev == "SPELL_AURA_APPLIED" and dst_me and BUFFS is not None and c[-1] == "BUFF" and c[9].isdigit():
            BUFFS.want(spell, int(c[9]))            # learn this buff's icon for the screen reader
        if ev in ("SPELL_AURA_APPLIED", "SPELL_AURA_REMOVED") and dst_me:
            low = spell.lower()
            if ev == "SPELL_AURA_APPLIED":
                self.my_auras[spell] = t
                self.my_removed.pop(spell, None)
                if any(w in low for w in REST_WORDS) and (self.rest_start is None or t - self.rest_start > 60):
                    self.rest_start = t
                    self.rest_count += 1
            else:
                self.my_auras.pop(spell, None)
                self.my_removed[spell] = t
                if any(w in low for w in REST_WORDS) and self.rest_start is not None:
                    if not any(any(w in k.lower() for w in REST_WORDS) for k in self.my_auras):
                        self.rest_time += min(t - self.rest_start, 60.0)   # (food/drink lasts 30 s; a missed "removed" isn't hours of rest)
                        self.rest_start = None
            return

    def _advanced(self, t, c, a):
        g = c[a]
        hp, maxhp = num(c[a + 2]), num(c[a + 3])
        x, y = ffloat(c[a + 14]), ffloat(c[a + 15])
        lvl = num(c[a + 18])
        if g == self.me:
            p = self.player
            ptype = num(c[a + 10].split("|")[0])
            p.update(hp=hp, maxhp=maxhp, absorb=num(c[a + 9]), ptype=ptype,
                     power=num(c[a + 11].split("|")[0]), maxpower=num(c[a + 12].split("|")[0]),
                     map=num(c[a + 16]))
            if x is not None and y is not None and (x, y) != (0.0, 0.0):
                if p["x"] is not None:
                    d = math.hypot(x - p["x"], y - p["y"])
                    if d < 200:            # ignore teleports / hearth
                        self.distance += d
                p["x"], p["y"] = x, y
                if not self.trail or math.hypot(x - self.trail[-1][1], y - self.trail[-1][2]) > 3:
                    self.trail.append([t, round(x, 1), round(y, 1)])
                    if len(self.trail) > 4000:
                        self.trail = self.trail[::2]
            if maxhp:
                pct = hp / maxhp
                if self.fight is not None and (self.lowest_hp_pct is None or pct < self.lowest_hp_pct):
                    self.lowest_hp_pct = pct
                    self.lowest_hp_info = {"hp": hp, "maxhp": maxhp, "by": self.last_hit_by, "t": t}
                if self.fight is not None:
                    self.fight["min_hp"] = min(self.fight["min_hp"], pct)
                if not self.timeline or t - self.timeline[-1][0] >= 2:
                    mp = (p["power"] / p["maxpower"]) if p["maxpower"] else None
                    self.timeline.append([t, round(pct, 3), round(mp, 3) if mp is not None else None])
                    if len(self.timeline) > 3000:
                        self.timeline = self.timeline[::2]
        elif g.startswith("Creature-") or g.startswith("Vehicle-"):
            u = self.units.setdefault(g, {"name": None})
            u.update(hp=hp, maxhp=maxhp, x=x, y=y)
            nm = u.get("name")
            if nm and maxhp > self.mobstats[nm]["maxhp"]:
                self.mobstats[nm]["maxhp"] = maxhp
            if lvl > 0:
                u["level"] = lvl

    def _touch(self, t, other_guid, other_name):
        f = self.fight
        if f is None or t - f["last"] > FIGHT_GAP:
            self._close_fight()
            f = self.fight = {"start": t, "last": t, "dealt": 0, "taken": 0, "healed": 0,
                              "mobs": {}, "kills": 0, "xp": 0, "mana": self.pre_pull_mana,
                              "min_hp": 1.0}
            self.pre_pull_mana = 0
        f["last"] = t
        if other_guid and other_guid.startswith(("Creature-", "Vehicle-")):
            f["mobs"][other_guid] = other_name
            self.units.setdefault(other_guid, {"name": other_name})["name"] = other_name

    def _close_fight(self):
        f = self.fight
        if not f:
            return
        f["dur"] = max(1.0, f["last"] - f["start"])
        self.combat_time += f["dur"]
        self.fights.append(f)
        self.fight = None

    MOBFIGHTS_FILE = os.path.join(HERE, "mobfights.jsonl")

    def _load_mob_fights(self):
        try:
            with open(self.MOBFIGHTS_FILE, encoding="utf-8") as fh:
                for line in fh:
                    try:                             # one bad line (cut short by a kill) must not drop the rest
                        r = json.loads(line)
                        if r["id"] not in self._mf_seen:
                            self._mf_seen.add(r["id"])
                            self.mob_fights[r["name"]].append(r)
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            pass

    def _my_level_at(self, t):
        """Your level at time t: the chat log's 'reached level' lines, then your baseline / portrait."""
        ups = sorted((x["t"], x["level"]) for x in self.chat_levels)
        before = [lv for tt, lv in ups if tt <= t]
        if before:
            return before[-1]
        if ups:
            return ups[0][1] - 1                     # before the first ding we saw
        # Forever doesn't log level-ups: only a fight happening now gets a level (the live portrait)
        return self._level_now() if time.time() - t < 600 else None

    def _level_now(self):
        if self.portrait_level:
            return self.portrait_level
        prog = self._xp_progress_log()
        return prog["level"] if prog else None

    def _close_duel(self, guid):
        """A mob's fight is over: file it under that mob type and your level at the time (kept for good)."""
        d = self.duels.pop(guid, None)
        if not d or not d["name"] or not (d["me"] or d["it"]):
            return
        rid = f"{guid}:{round(d['t'])}"
        if rid in self._mf_seen:
            return                                   # replaying a log we've already filed
        rec = {"id": rid, "name": d["name"], "t": round(d["t"], 1), "lvl": self._my_level_at(d["t"]),
               "me": d["me"], "it": d["it"], "dmg": d["dmg"],
               # only YOUR fights count for pace (in a group it pounds you while you heal)
               "solo": d["me"] >= 2 and d["it"] >= 2 and d["me"] >= 0.6 * max(1, d["all"]),
               "m_att": d["m_att"], "m_miss": d["m_miss"], "i_att": d["i_att"], "i_miss": d["i_miss"]}
        self._mf_seen.add(rid)
        self.mob_fights[d["name"]].append(rec)
        try:
            with open(self.MOBFIGHTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    def _fights_for(self, name):
        """This mob's fights at your level (current or one below). If none, the latest older level, marked."""
        recs = list(self.mob_fights.get(name, []))
        cur = self._level_now()
        live = [{"lvl": cur, "me": d["me"], "it": d["it"], "dmg": d["dmg"], "m_att": d["m_att"], "m_miss": d["m_miss"],
                 "i_att": d["i_att"], "i_miss": d["i_miss"],
                 "solo": d["me"] >= 1 and d["it"] >= 1 and d["me"] >= 0.6 * max(1, d["all"])}
                for d in self.duels.values() if d["name"] == name and time.time() - d["t"] < 300]   # going on now

        if cur is None:
            return recs + live, None
        now = [r for r in recs if r.get("lvl") in (cur, cur - 1)]
        if now or live:
            return now + live, None
        older = [r for r in recs if r.get("lvl") is not None and r["lvl"] < cur]
        if older:
            last = max(r["lvl"] for r in older)
            return [r for r in older if r["lvl"] == last], last      # stale: from level `last`
        unknown = [r for r in recs if r.get("lvl") is None]
        return (unknown, f"<{cur}") if unknown else ([], None)       # stale: before levels were kept = below your level now

    def pace(self, name):
        """You : it, in attacks, at your level. Its damage per attack from the same fights."""
        recs, stale = self._fights_for(name) if name else ([], None)
        solo = [r for r in recs if r.get("solo")]
        tm, ti, dm = sum(r["me"] for r in solo), sum(r["it"] for r in solo), sum(r["dmg"] for r in solo)
        if tm < 3 or ti < 3:
            return None
        return {"ratio": round(ti / tm, 2), "mine": tm, "theirs": ti, "per_attack": round(dm / ti, 1),
                "fights": len(solo), "stale_lvl": stale}

    def _unit_died(self, t, dg, dn, df):
        self._close_duel(dg)
        if dg == self.me:
            self.deaths.append({"t": t, "x": self.player["x"], "y": self.player["y"], "by": self.last_hit_by})
            self.push(t, "death", f"You died{' to ' + self.last_hit_by if self.last_hit_by else ''}. Bagem has been tagem'd.")
            self._close_fight()
            return
        if not (df & T_NPC) or dg in self.killed_guids or dg not in self.damaged_by_me:
            return
        self.killed_guids.add(dg)
        if not self.tagger.get(dg, True):
            self.stolen += 1
            return
        u = self.units.get(dg, {})
        lvl = u.get("level")
        k = {"t": t, "guid": dg, "name": dn, "level": lvl, "xp": None, "map": self.cur_map,
             "x": u.get("x") if u.get("x") else self.player["x"],
             "y": u.get("y") if u.get("y") else self.player["y"]}
        self.kills.append(k)
        if self.fight:
            self.fight["kills"] += 1

    # ------------------------------------------------------------ chat log
    def _save_note(self, t, text):
        """A note typed in game: saved with what we knew at that moment, for Claude to read later."""
        try:
            done = set()
            p = os.path.join(HERE, "notes.jsonl")
            if os.path.exists(p):
                done = {(round(n["t"], 1), n["text"]) for n in map(json.loads, open(p, encoding="utf-8"))}
            if (round(t, 1), text) in done:
                return                                   # replayed line
            cur = self.inst.cur
            ctx = {"t": round(t, 1), "text": text, "when": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)),
                   "zone": LOC.zone if LOC else None,
                   "instance": cur["name"] if cur else None,
                   "run_min": round((t - cur["start"]) / 60, 1) if cur else None,
                   "pulls_done": len(cur.get("fights", [])) if cur else None,
                   "party": cur.get("party") if cur else None,
                   "target": getattr(NAMER, "name", None) if NAMER else None,
                   "minimap": MAPPER.status if MAPPER else None,
                   "level": self.portrait_level}
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(ctx, ensure_ascii=False) + "\n")
            self.push(t, "note", "📝 note saved: " + text[:60])
        except Exception as e:
            print("note save failed:", e, file=sys.stderr)

    def feed_chat(self, line):
        t = None
        text = line
        if "  " in line:
            ts_s, rest = line.split("  ", 1)
            t = parse_ts(ts_s)
            if t is not None:
                text = rest
        if t is None:
            t = time.time()
        text = strip_links(text.strip())
        m = CHAT_NOTE.search(text)
        if m:
            self._save_note(t, m.group(1).strip())
            return
        m = CHAT_KILL_XP.search(text)
        if m:
            self.chat_active = True
            xp = num(m.group(2).replace(",", ""))
            rested = num((m.group(3) or "0").replace(",", ""))
            self.chat_xp.append({"t": t, "xp": xp, "rested": rested, "mob": m.group(1), "kind": "kill"})
            return
        m = CHAT_LEVEL.search(text)
        if m:
            lvl = int(m.group(1))
            self.chat_levels.append({"t": t, "level": lvl})
            self.push(t, "level", f"DING! Level {lvl}")
            return
        m = CHAT_XP.search(text)
        if m:
            self.chat_active = True
            self.chat_xp.append({"t": t, "xp": num(m.group(1).replace(",", "")), "rested": 0, "mob": None,
                                 "kind": "quest"})
            return
        m = CHAT_ROLL_WON.search(text)
        if m:
            who = (self.me_name or "You") if m.group(1) == "You" else m.group(1)
            self.inst.on_roll_won(t, who, m.group(2).strip())
            return
        m = CHAT_LOOT.search(text)
        if m:
            item, cnt = m.group(1).strip(), int(m.group(2) or 1)
            self.loot[item] += cnt
            if re.search(r"receive loot", text, re.I):     # "receive item" = bought/quest/made: not a drop
                self.inst.on_loot(t, item, cnt, self.me_name or "You")
            return
        m = CHAT_PARTY_LOOT.search(text)
        if m:
            self.inst.on_loot(t, m.group(2).strip(), int(m.group(3) or 1), m.group(1).strip())
            return
        m = CHAT_MONEY.search(text)
        if m:
            cp = money_to_copper(m.group(1))
            if cp:
                self.copper += cp
                self.inst.on_money(t, cp)

    # ------------------------------------------------------------ snapshot for the page
    def snapshot(self):
        with self.lock:
            return self._snapshot()

    def maps_snapshot(self, place=None):
        """For the Map page: every minimap-built map, the one asked for (default: where you are),
        your live spot on it, and for dungeons the recorded routes/pulls/bosses."""
        out = {"maps": MAPPER.places_list() if MAPPER else [], "here": None, "place": None, "info": None}
        if MAPPER is None:
            return out
        with self.lock:
            here = MAPPER.place_now
            out["here"] = here
            place = place or here or (out["maps"][0]["place"] if out["maps"] else None)
            out["place"] = place
            if not place:
                return out
            info = MAPPER.info(place)
            out["info"] = info
            unit = (info or {}).get("unit")
            if unit == "coords" and LOC is not None and MAPPER.place_now and                     (place == MAPPER.place_now or (info or {}).get("frame") == MAPPER.place_now):
                out["me"] = [LOC.x, LOC.y] if LOC.x is not None else None
                out["trail"] = [[p[1], p[2]] for p in (LOC.history or [])[-1500:]]
            elif unit == "world":
                snap = self.inst.snapshot(place, half="world" if place.endswith(" (open world)") else "instance")
                out["routes"] = [[p[1:] for p in tr] for tr in (snap.get("trails") or [])]
                out["cur_trail"] = [p[1:] for p in (snap.get("cur_trail") or [])]
                out["pulls"] = [[f["pos"][0], f["pos"][1], f["size"], f.get("boss")] for f in (snap.get("fights") or []) if f.get("pos")]
                out["bosses"] = [[b["pos"][0], b["pos"][1], b["name"]] for b in (snap.get("bosses") or []) if b.get("pos")]
                if self.inst.cur and place in (map_place(self.inst.cur), self.inst.cur["name"]):
                    me = self.inst.ppos.get(self.inst.me)
                    out["me"] = list(me) if me else None
            return out

    def _zone_map(self):
        """The minimap-built map of the zone you're in (outdoors), lined up with the map coords."""
        if MAPPER is None or self.inst.cur or LOC is None or not (LOC.map or LOC.zone):
            return None
        from location import CONTINENTS
        if MAPPER.mapping_on and MAPPER.grids.cur:       # Mapping: the grid you're being followed on
            return MAPPER.info(MAPPER.grids.cur["gid"])
        # the zone the coords are on, as settled by the mapper (coords jumps + label + picture), else the log
        # (right after a coords jump, before the mapper has settled, the label is the best guess: it switches
        # first at a zone line, and the dot must be drawn in the frame of the zone the coords are on)
        lab = self.resolve_zone(LOC.zone, fallback=False) if LOC.zone else None
        name = (MAPPER.place_now if MAPPER.unit_now == "coords" else None) or lab or             getattr(MAPPER, "last_coords_place", None) or self.cur_map
        if not name or name.strip().lower() in CONTINENTS:
            return None
        info = MAPPER.info(name)
        return info if info and info.get("pinned") and info.get("unit") == "coords" else None

    def _dungeon_map(self):
        dm = self.inst.hud_map()
        if dm and MAPPER is not None:
            info = MAPPER.info(dm.get("map") or dm["name"])
            dm["underlay"] = info if info and (info["unit"] in ("world", None) or not info["pinned"]) else None
        return dm

    def _xp_progress(self):
        prog = self._xp_progress_log()
        if prog and self.screen_live() and self.scr_last_xp:
            lvl, xp = self.scr_last_xp
            rs = (self.screen or {}).get("rested") or 0
            prog.update(rested=round(rs * xp_needed(lvl)) if rs else 0)
            prog.update(level=lvl, xp=xp, need=xp_needed(lvl), exact=True, source="screen",
                        gained=self.scr_series[-1][1] if self.scr_series else 0,
                        series=self.scr_series[-200:],
                        since=self.scr_series[0][0] if self.scr_series else prog["since"])
            used = set()                     # match XP-bar jumps to kills -> exact XP per kill
            J, j0, last_t = self.scr_jumps, 0, None   # (both lists are in time order: was kills x jumps on every poll)
            for k in self.kills:
                if last_t is not None and k["t"] < last_t:
                    j0 = 0                           # (the join between two log files)
                last_t = k["t"]
                while j0 < len(J) and J[j0][0] - k["t"] < -1.5:
                    j0 += 1
                for i in range(j0, len(J)):
                    if J[i][0] - k["t"] > 4:
                        break
                    if i not in used:
                        k["xp_real"] = J[i][1]
                        used.add(i)
                        break
            prog["other_xp"] = sum(a for i, (_, a) in enumerate(self.scr_jumps) if i not in used)
            prog["other_gains"] = [{"t": jt, "xp": amt, "name": self.jump_names.get(jt)}
                                   for i, (jt, amt) in enumerate(self.scr_jumps) if i not in used][-30:]
        return prog

    def _xp_progress_log(self):
        """Walk gains after the user's baseline, rolling over levels."""
        base = self.baselines.get(self.me_name) if self.me_name else None
        if not base:
            return None
        lvl, xp, t0 = base["level"], base["xp"], base["t"]
        gains = []
        if self.chat_active:
            gains = [(g["t"], g["xp"]) for g in self.chat_xp if g["t"] >= t0]
        else:
            for k in self.kills:
                if k["t"] >= t0:
                    gains.append((k["t"], None, k))
        levels = sorted((lv["t"], lv["level"]) for lv in self.chat_levels if lv["t"] >= t0)
        li = 0
        gained = 0
        series = [[t0, 0]]
        for g in sorted(gains, key=lambda g: g[0]):
            while li < len(levels) and levels[li][0] <= g[0]:
                if levels[li][1] > lvl:
                    xp = max(0, xp - xp_needed(lvl)) if xp >= xp_needed(lvl) else 0
                    lvl = levels[li][1]
                li += 1
            if g[1] is not None:
                amt = g[1]
            else:
                k = g[2]
                amt = kill_xp(lvl, k["level"])
                k["xp"] = amt
            gained += amt
            series.append([g[0], gained])
            xp += amt
            while lvl < MAX_LEVEL and xp >= xp_needed(lvl) and not levels:
                xp -= xp_needed(lvl)
                lvl += 1
                self._ding_est(g[0], lvl)
        while li < len(levels):
            if levels[li][1] > lvl:
                xp = max(0, xp - xp_needed(lvl))
                lvl = levels[li][1]
            li += 1
        return {"level": lvl, "xp": xp, "need": xp_needed(lvl), "gained": gained, "since": t0,
                "series": series[-400:],
                "exact": self.chat_active}

    def _ding_est(self, t, lvl):
        if lvl not in self._dinged:
            self._dinged.add(lvl)
            self.push(t, "level", f"DING! Level {lvl} (estimated)")

    def _live_hp(self):
        ex = beacon_stats()
        if ex and ex["hpmax"]:
            return ex["hp"]
        if self.screen_live() and self.player["maxhp"]:
            return self.screen["hp"] * self.player["maxhp"]
        return self.player["hp"]

    def _target_now(self):
        """What's targeted right now, from the screen only: name OCR'd off the target frame,
        health from its bar. The log just supplies averages (your hit size, its hit, max HP)."""
        scr_t = self.screen.get("target") if self.screen_live() else None
        if scr_t is None:
            return None
        ocr_name = self.ocr_target
        name = None
        if ocr_name:
            known = {n for n, ms in self.mobstats.items() if ms["my_dmg"] or ms["their_hits"] or ms["maxhp"]}
            best = max(known, key=lambda n: SequenceMatcher(None, n.lower(), ocr_name.lower()).ratio(), default=None)
            name = best if best and SequenceMatcher(None, best.lower(), ocr_name.lower()).ratio() >= 0.75                 else ocr_name.title()
        hm = self._hits_math(name) if name else self._hits_math(None)
        maxhp = hm["mob_hp"]
        hp = round(scr_t * maxhp) if maxhp else None
        left = math.ceil(hp / hm["best_avg"]) if hp and hm["best_avg"] else None
        lv = sorted({x.get("level") for x in self.units.values() if name and x.get("name") == name and x.get("level")})
        return {"name": name or "Reading name…", "level": (lv[0] if len(lv) == 1 else f"{lv[0]}-{lv[-1]}") if lv else None,
                "hp": hp, "maxhp": maxhp, "hits_left": left, "live": True, "assumed": False, "frac": scr_t,
                "reaction": self.screen.get("reaction"),
                "heals": self.mob_heal_info(name) if name else None,
                "info": MOBS.lookup(name) if MOBS and name and self.screen.get("reaction") not in ("player",) else None,
                "moves": self.mob_moves_info(name) if name else [],
                "known": bool(hm["best_avg"] or hm["their_avg"]), **hm, **self._race(name, left, hm)}

    SUBZONES_FILE = os.path.join(HERE, "subzones.json")

    def _subzones(self):
        if not hasattr(self, "_subz"):
            try:
                with open(self.SUBZONES_FILE, encoding="utf-8") as f:
                    self._subz = json.load(f)
            except (OSError, ValueError):
                self._subz = {}
        return self._subz

    ROOMS_FILE = os.path.join(HERE, "dungeon_rooms.json")

    @property
    def dungeon_rooms(self):
        """Minimap labels seen inside a dungeon (its rooms) -> dungeon name."""
        if not hasattr(self, "_rooms"):
            try:
                with open(self.ROOMS_FILE, encoding="utf-8") as _f:
                    self._rooms = json.load(_f)
            except (OSError, ValueError):
                self._rooms = {}
        return self._rooms

    def save_rooms(self):
        try:
            with open(self.ROOMS_FILE, "w", encoding="utf-8") as _f:
                json.dump(self._rooms, _f, indent=1, sort_keys=True)
        except OSError:
            pass

    def room_of(self, label):
        lab = (label or "").strip(" .").lower()
        for k, v in self.dungeon_rooms.items():
            if k == lab or SequenceMatcher(None, k, lab).ratio() >= 0.85:
                return v
        return None

    def is_outdoor(self, label):
        """True if a minimap label is an open-world zone or a learned outdoor area (not a dungeon)."""
        lab = (label or "").strip(" .").lower()
        if not lab or self.inst.lookup.for_name(label) or self.room_of(label):
            return False
        from location import ZONES
        zones = {z.lower() for z in ZONES} | {z.lower() for z in self.map_bounds} | {v.lower() for v in self._subzones().values()}
        if lab in zones or lab in self._subzones():
            return True
        return any(SequenceMatcher(None, lab, z).ratio() >= 0.85 for z in zones)

    def resolve_zone(self, label, fallback=True):
        """Which zone map you're on, from the live minimap label (zone or subzone name).
        Learns subzone -> zone whenever the combat log is fresh enough to be trusted.
        fallback=False: None when the label doesn't tell (instead of the log's map)."""
        label = (label or "").strip(" .")
        if not label:
            return self.cur_map if fallback else None
        from location import ZONES
        known = set(ZONES) | set(self.map_bounds) | {os.path.splitext(f)[0].replace("_", " ")
                                        for f in os.listdir(os.path.join(HERE, "maps"))
                                        if f.endswith(".png")} if os.path.isdir(os.path.join(HERE, "maps")) else set(ZONES) | set(self.map_bounds)
        best = max(known, key=lambda z: SequenceMatcher(None, z.lower(), label.lower()).ratio(), default=None)
        if best and SequenceMatcher(None, best.lower(), label.lower()).ratio() >= 0.85:
            return best                                      # the label is a zone name itself
        subz = self._subzones()
        key = label.lower()
        fresh = self.last_t and time.time() - self.last_t < 20 and self.cur_map
        if MAPPER is not None and getattr(MAPPER, "last_jump_t", 0) > getattr(self, "cur_map_t", 0):
            fresh = False             # coords jumped since the log last named the map: it may be the old zone
        clean = re.fullmatch(r"[A-Z][A-Za-z']+(?: (?:[A-Z][A-Za-z']+|of|the|and))*", label) and len(label) >= 4
        from location import CONTINENTS
        if MAPPER is not None and MAPPER.unit_now == "coords":
            fresh = False             # outdoor mapping: area names are learned from zone crossings instead (the
                                      # log can name the zone you were in minutes ago; border areas straddle zones)
        if fresh and clean and not self.inst.cur and subz.get(key) != self.cur_map                 and self.cur_map.strip().lower() not in CONTINENTS:      # never inside a dungeon, never a continent
            subz[key] = self.cur_map                         # learn: this subzone is in that zone
            try:
                with open(self.SUBZONES_FILE, "w", encoding="utf-8") as f:
                    json.dump(subz, f, indent=1, sort_keys=True)
            except OSError:
                pass
        for k, z in subz.items():                            # tolerate small OCR differences
            if k == key or SequenceMatcher(None, k, key).ratio() >= 0.85:
                if z:
                    return z
                break                                        # an area on both sides of a zone line: no help
        return self.cur_map if fallback else None

    def to_map_pct(self, zone, wx, wy):
        """World (x, y) -> zone map percent, using the bounds from MAP_CHANGE."""
        b = self.map_bounds.get(zone)
        if not b or wx is None or wy is None:
            return None
        nx, sx_, wy0, ey = b
        if nx == sx_ or wy0 == ey:
            return None
        return [round((wy0 - wy) / (wy0 - ey) * 100, 2), round((nx - wx) / (nx - sx_) * 100, 2)]

    def spell_effectiveness(self):
        """Per spell: cast time, mana per cast, damage per cast, per mana and per second spent casting,
        overall and per mob type. Instants count the 1.5 s global cooldown as their cast time."""
        stamp = (len(self.kills), len(self.spell_eff))
        cache = getattr(self, "_se_cache", None)
        if cache and cache[0] == stamp:
            return cache[1]
        out = []
        for spell, se in self.spell_eff.items():
            dmg_all = sum(m["dmg"] for m in se["by_mob"].values())
            if not dmg_all:
                continue                                    # shields, buffs, heals: not damage spells
            if spell == "Melee":                            # no cast events: count swings instead
                se["casts"] = sum(m["hits"] for m in se["by_mob"].values())
                for m in se["by_mob"].values():
                    m["casts"] = m["hits"]
            casts = max(1, se["casts"])
            avg_cast = se["cast_time"] / max(1, se["timed"])
            busy = max(1.5, avg_cast)                       # time the cast occupies you
            mana_pc = se["mana"] / casts
            row = {"spell": spell, "casts": se["casts"], "cast_time": round(avg_cast, 2),
                   "instant": avg_cast < 0.2, "mana": round(mana_pc, 1),
                   "dmg_per_cast": round(dmg_all / casts, 1),
                   "dmg_per_mana": round(dmg_all / se["mana"], 2) if se["mana"] else None,
                   "dps_casting": round(dmg_all / casts / busy, 1), "by_mob": {}}
            for mob, m in se["by_mob"].items():
                c_ = max(1, m["casts"])
                row["by_mob"][mob] = {"casts": m["casts"], "dmg": m["dmg"], "dmg_per_cast": round(m["dmg"] / c_, 1),
                                      "dmg_per_mana": round(m["dmg"] / (mana_pc * c_), 2) if mana_pc else None,
                                      "dps_casting": round(m["dmg"] / c_ / busy, 1),
                                      "crit": round(m["crits"] / m["hits"] * 100) if m["hits"] else 0}
            out.append(row)
        result = sorted(out, key=lambda r: -r["casts"])
        self._se_cache = (stamp, result)
        return result

    def heal_effectiveness(self):
        out = []
        for spell, se in self.spell_eff.items():
            raw = se["heal_eff"] + se["heal_over"]
            if not raw:
                continue
            casts = max(1, se["casts"])
            avg_cast = se["cast_time"] / max(1, se["timed"])
            busy = max(1.5, avg_cast)
            mana_pc = se["mana"] / casts
            out.append({"spell": spell, "casts": se["casts"], "cast_time": round(avg_cast, 2),
                        "instant": avg_cast < 0.2, "mana": round(mana_pc, 1),
                        "heal_per_cast": round(se["heal_eff"] / casts, 1),
                        "overheal": round(se["heal_over"] / raw * 100) if raw else 0,
                        "heal_per_mana": round(se["heal_eff"] / se["mana"], 2) if se["mana"] else None,
                        "hps_casting": round(se["heal_eff"] / casts / busy, 1)})
        return sorted(out, key=lambda r: -r["casts"])

    def mob_moves_info(self, name):
        mm = self.mob_moves.get(name)
        if not mm:
            return []
        out = []
        for sp, v in mm.items():
            if not v["n"]:
                continue
            out.append({"spell": sp, "kind": v["kind"], "n": v["n"],
                        "avg": round(v["dmg"] / v["n"], 1) if v["dmg"] else None, "max": v["max"] or None,
                        "crit": round(v["crits"] / v["n"] * 100) if v["kind"] in ("melee", "damage") else None})
        order = {"melee": 0, "damage": 1, "debuff": 2, "heal": 3, "buff": 4, "cast": 5}
        return sorted(out, key=lambda r: (order.get(r["kind"], 9), -r["n"]))

    def mob_heal_info(self, name):
        """[{spell, n, avg, casts}] for a mob type that's been seen healing, else None."""
        mh = self.mob_heals.get(name)
        if not mh:
            return None
        return [{"spell": sp, "n": v["n"], "casts": v["casts"], "avg": round(v["amt"] / v["n"]) if v["n"] else None}
                for sp, v in sorted(mh.items(), key=lambda kv: -(kv[1]["n"] + kv[1]["casts"]))]

    def _screen_party(self):
        if not self.screen_live() or not self.screen.get("party"):
            return None
        names = PARTY_NAMES.names if PARTY_NAMES is not None else {}
        sp = getattr(self, "strip_party", None)          # the add-on's strip: party1-4 are the frames top to bottom
        if sp is not None:
            by = {m["slot"] - 1: m for m in sp}
            return [{**f, "name": (by.get(f["slot"]) or {}).get("name"), "level": (by.get(f["slot"]) or {}).get("level"),
                     "cls": (by.get(f["slot"]) or {}).get("cls")} for f in self.screen["party"]]
        return [{**f, "name": names.get(f["slot"])} for f in self.screen["party"]]

    def buff_status(self):
        """Reminders for your own buffs, and party members missing the ones you hand out."""
        now = time.time()
        mine = []
        on_screen = BUFFS.names() if BUFFS is not None and self.screen_live() else None
        try:
            if on_screen is not None and MAPPER is not None and MAPPER._spot():
                on_screen = None                             # another character's layout: the buff bar is not where
        except Exception:                                    # the reader looks, so the log decides
            pass
        known = self.cast_by.get(self.me_name) or ()         # (this character's: a session can hold two characters)
        for sp in SELF_BUFFS:
            if sp not in known:
                continue                                     # you don't have/use it
            if BETTER_BUFF.get(sp) in known:
                continue                                     # replaced by the better one
            if on_screen is not None and sp in (BUFFS.lib if BUFFS else {}):
                # the buff bar on screen is the truth (instant); the log is the fallback
                if sp not in on_screen:
                    mine.append({"spell": sp, "state": "missing", "source": "screen"})
                else:
                    left = BUFFS.left(sp)
                    if left is not None and left <= 120:
                        mine.append({"spell": sp, "state": "expiring", "left": left, "source": "screen"})
                continue
            # buff timers pause while you're logged out, so don't guess from durations: it's on from
            # the log's APPLIED until its REMOVED. Missing only when the log saw it come off.
            if sp in self.my_auras:
                continue
            if sp in self.my_removed:
                mine.append({"spell": sp, "state": "missing", "since": self.my_removed[sp]})
        group = []
        for sp in GROUP_BUFFS:
            if sp not in known:
                continue
            missing = [m["name"] for m in self.grp.members.values() if not m["me"] and now - m["seen"] < 600
                       and sp not in m["auras"]]
            if missing:
                group.append({"spell": sp, "missing": missing})
        return {"mine": mine, "group": group}

    def aggro_status(self):
        """Mobs that hit you in the last 8 s and aren't dead, plus the player-frame threat glow."""
        now = time.time()
        on = []
        for g, (t, name) in list(self.on_me.items()):
            if now - t > 8 or g in self.killed_guids or (self.units.get(g, {}).get("hp") == 0):
                if now - t > 60:
                    self.on_me.pop(g, None)
                continue
            on.append(name)
        glow = bool(self.screen_live() and self.screen.get("aggro_glow"))
        return {"mobs": on, "count": len(on), "glow": glow}

    def dungeon_snapshot(self, guide=None):
        from dungeon import live_view
        with self.lock:
            me = None                            # inside, your position comes from this run's route
            if MAPPER is not None and self.inst.cur:
                me = MAPPER.live_pos(map_place(self.inst.cur))  # live: last kill spot + minimap tracking
            roles = {m["name"]: (self.grp.role(m), m["cls"]) for m in self.grp.members.values()}
            recaps = [{"name": r["name"], "t": r["t"], "hits": r["hits"]} for r in self.grp.deaths]
            v = live_view(self.inst, guide, mob_moves=self.mob_moves_info, mob_heals=self.mob_heal_info,
                          me_pos=me, roles=roles, recaps=recaps)
            snap = self._snapshot()
            for k in ("group", "player", "target", "aggro", "buffs", "mana_per_kill", "spells", "screen", "logs"):
                v[k] = snap.get(k)
            v["screen"] = {k: (snap.get("screen") or {}).get(k) for k in ("ok", "hp", "mana")}
            if MAPPER is not None and v.get("name"):
                info = MAPPER.info(v["name"])
                v["underlay"] = info if info and (info["unit"] in ("world", None) or not info["pinned"]) else None
                v["minimap_status"] = MAPPER.status
                v["minimap_dbg"] = getattr(MAPPER, "dbg", None)
            return v

    def instance_snapshot(self, name=None):
        with self.lock:
            me = (self.player["x"], self.player["y"]) if self.player["x"] is not None else None
            roles = {m["name"]: (self.grp.role(m), m["cls"]) for m in self.grp.members.values()}
            recaps = [{"name": r["name"], "t": r["t"], "hits": r["hits"]} for r in self.grp.deaths]
            v = self.inst.snapshot(name, mob_moves=self.mob_moves_info, mob_heals=self.mob_heal_info,
                                   mobinfo=(MOBS.lookup if MOBS else None),
                                   me_pos=me if self.inst.cur else None, roles=roles, recaps=recaps)
            if MAPPER is not None and v.get("name"):
                info = MAPPER.info(v.get("map_place") or v["name"])    # (the open-world half has its own picture)
                v["underlay"] = info if info and info.get("pinned") and info.get("url") and info.get("unit") != "coords" else None
            return v

    def _race(self, name, my_left, hm):
        """Attack pace vs this mob, and the race: while you land the attacks it takes to kill it, how many
        attacks does it get, and how much of your HP is that (its average per attack, misses included)."""
        pc = self.pace(name) if name else None
        recs, stale = self._fights_for(name) if name else ([], None)
        ma, mm = sum(r["m_att"] for r in recs), sum(r["m_miss"] for r in recs)
        ia, im = sum(r["i_att"] for r in recs), sum(r["i_miss"] for r in recs)
        out = {"pace": pc, "race": None,
               "miss": {"mine": round(100 * mm / ma) if ma >= 5 else None, "mine_n": ma,
                        "theirs": round(100 * im / ia) if ia >= 5 else None, "theirs_n": ia, "stale_lvl": stale}}
        if not pc or not my_left:
            return out
        their_attacks = math.ceil(my_left * pc["ratio"])
        per_attack = pc.get("per_attack")
        dmg = round(their_attacks * per_attack) if per_attack else None
        hp = self._live_hp()
        out["race"] = {"my_attacks": my_left, "their_attacks": their_attacks, "dmg": dmg,
                       "hp_after": (hp - dmg) if hp and dmg is not None else None,
                       "pct_of_hp": round(100 * dmg / self.player["maxhp"]) if dmg and self.player["maxhp"] else None}
        return out

    def _hits_math(self, name):
        """How many of my hits to kill this mob type, and how many of its hits I can take."""
        ms = self.mobstats.get(name)
        out = {"mob_hp": None, "best_spell": None, "best_avg": None, "hits_to_kill": None,
               "their_avg": None, "hits_to_die": None, "hits_left_now": None}
        if not ms:
            return out
        out["mob_hp"] = ms["maxhp"] or None
        best = None
        for sp, (tot, n) in ms["my_dmg"].items():
            if n >= 2 or best is None:
                avg = tot / n
                if best is None or n > best[2]:
                    best = (sp, avg, n)
        if best:
            out["best_spell"], out["best_avg"] = best[0], round(best[1])
            if ms["maxhp"]:
                out["hits_to_kill"] = math.ceil(ms["maxhp"] / best[1])
        if ms["their_hits"]:
            avg = ms["their_dmg"] / ms["their_hits"]
            out["their_avg"] = round(avg, 1)
            if self.player["maxhp"]:
                out["hits_to_die"] = math.ceil(self.player["maxhp"] / avg)
            hp_now = self._live_hp()
            if hp_now:
                out["hits_left_now"] = math.ceil(hp_now / avg)
        return out

    def active_since(self, t0):
        return sum(max(0.0, e - max(s, t0)) for s, e in self.segments)

    def _snapshot(self):
        now = time.time()
        last = self.last_t or now
        first = self.first_t or now
        active = max(1.0, self.active_since(0))
        sitting = self.segments[-1][0] if self.segments else first
        cur_fight = self.fight
        combat = self.combat_time + (max(1.0, cur_fight["last"] - cur_fight["start"]) if cur_fight else 0)
        rest = self.rest_time + ((last - self.rest_start) if self.rest_start else 0)
        idle = max(0.0, active - combat - rest)

        live = self.screen_live()
        player = dict(self.player)
        player["live"] = live
        if live:
            if player["maxhp"]:
                player["hp"] = round(self.screen["hp"] * player["maxhp"])
            if player["maxpower"]:
                player["power"] = round(self.screen["mana"] * player["maxpower"])
        prog = self._xp_progress()
        char_lvl = prog["level"] if prog else None

        # Per-mob table
        mobs = {}
        fight_secs = defaultdict(list)
        for f in self.fights:
            names = set(f["mobs"].values())
            if len(names) == 1 and f["kills"]:
                fight_secs[next(iter(names))].append(f["dur"] / max(1, f["kills"]))
        chat_by_mob = defaultdict(list)
        for g in self.chat_xp:
            if g["mob"]:
                chat_by_mob[g["mob"]].append(g["xp"])
        for k in self.kills:
            m = mobs.setdefault(k["name"], {"name": k["name"], "kills": 0, "levels": set(), "xp_list": []})
            m["kills"] += 1
            if k["level"]:
                m["levels"].add(k["level"])
        for name, ms in self.mobstats.items():       # fought but not (yet) killed
            if ms["my_dmg"] or ms["their_hits"]:
                mobs.setdefault(name, {"name": name, "kills": 0, "levels": set(), "xp_list": []})
        for g, u in self.units.items():
            if u.get("name") in mobs and u.get("level"):
                mobs[u["name"]]["levels"].add(u["level"])
        rows = []
        for name, m in mobs.items():
            lv = sorted(m["levels"])
            real = [k["xp_real"] for k in self.kills if k["name"] == name and k.get("xp_real")]
            if real:
                avg_xp = sum(real) / len(real)
            elif chat_by_mob.get(name):
                avg_xp = sum(chat_by_mob[name]) / len(chat_by_mob[name])
            elif char_lvl and lv:
                avg_xp = sum(kill_xp(char_lvl, l) for l in lv) / len(lv)
            else:
                avg_xp = None
            secs = fight_secs.get(name)
            avg_secs = sum(secs) / len(secs) if secs else None
            remaining = (prog["need"] - prog["xp"]) if prog else None
            rows.append({**(lambda h: {**h, **self._race(name, h["hits_to_kill"], h)})(self._hits_math(name)), "last": self.mobstats[name]["last"] if name in self.mobstats else 0,
                "name": name, "kills": m["kills"],
                "levels": f"{lv[0]}" if len(lv) == 1 else (f"{lv[0]}-{lv[-1]}" if lv else "?"),
                "avg_xp": round(avg_xp) if avg_xp else avg_xp,
                "avg_secs": round(avg_secs, 1) if avg_secs else None,
                "xp_per_min": round(avg_xp / avg_secs * 60) if avg_xp and avg_secs else None,
                "to_level": math.ceil(remaining / avg_xp) if remaining and avg_xp else None,
            })
        rows.sort(key=lambda r: -r["kills"])

        xp_rate = None
        if prog and prog["gained"]:
            kills_gained = max(0, prog["gained"] - prog.get("other_xp", 0))   # quest/exploration XP left out
            span = max(300.0, self.active_since(prog["since"]))
            xp_rate = kills_gained / span * 3600 if kills_gained else None

        last_kill = self.kills[-1] if self.kills else None
        last_row = next((r for r in rows if last_kill and r["name"] == last_kill["name"]), None)

        spells = []
        for name, sp in self.spells.items():
            if sp["casts"] or sp["dmg"] or sp["heal"]:
                eff = sp["heal"] - sp["over"]
                spells.append({
                    "name": name, "casts": sp["casts"], "dmg": sp["dmg"], "heal": sp["heal"], "over": sp["over"],
                    "eff_heal": eff, "mana": sp["mana"], "hits": sp["hits"], "crits": sp["crits"], "max": sp["max"],
                    "per_mana": round((sp["dmg"] + eff) / sp["mana"], 2) if sp["mana"] else None,
                })
        spells.sort(key=lambda r: -(r["dmg"] + r["heal"]))

        fights = []
        for f in list(self.fights)[-12:][::-1]:
            fights.append({"start": f["start"], "dur": round(f["dur"], 1),
                           "mobs": sorted(set(f["mobs"].values())), "kills": f["kills"],
                           "dealt": f["dealt"], "taken": f["taken"], "healed": f["healed"],
                           "mana": f["mana"],
                           "min_hp": round(f["min_hp"] * 100)})

        mana_used_fights = [f["mana"] for f in self.fights if f["kills"]]
        kills_n = len(self.kills)

        recent_players = sorted((n for n, tt in self.players_seen.items() if last - tt < 120))
        recent_hostiles = sorted((n for n, tt in self.hostiles_seen.items() if last - tt < 120))

        return {
            "now": now, "me": self.me_name, "zone": self.zone, "build": self.build,
            "lines": self.lines, "first_t": self.first_t, "last_t": self.last_t, "sitting_t": sitting,
            "sitting_secs": (last - sitting) if self.segments else 0,
            "lag": (now - self.last_t) if self.last_t else None,
            "player": player,
            "screen": dict(self.screen) if self.screen else None,
            "auras": sorted(self.my_auras.keys()),
            "time": {"active": active, "combat": combat, "rest": rest, "idle": idle},
            "xp": prog, "xp_rate": xp_rate,
            "eta_secs": ((prog["need"] - prog["xp"]) / xp_rate * 3600) if prog and xp_rate else None,
            "last_kill": last_kill, "last_kill_row": last_row,
            "mobs": rows,
            "target": self._target_now(),
            "kills": kills_n, "deaths": len(self.deaths), "stolen": self.stolen,
            "kills_per_hour": kills_n / active * 3600 if kills_n else 0,
            "avg_fight": (sum(f["dur"] for f in self.fights) / len(self.fights)) if self.fights else None,
            "mana_per_kill": (sum(mana_used_fights) / max(1, sum(f["kills"] for f in self.fights if f["kills"])))
            if mana_used_fights else None,
            "kills_per_rest": (kills_n / self.rest_count) if self.rest_count else None,
            "rest_count": self.rest_count,
            "dmg_done": self.dmg_done, "dmg_taken": self.dmg_taken,
            "heal_done": self.heal_done, "overheal": self.overheal, "heal_taken": self.heal_taken,
            "shield_absorbed": self.shield_absorbed,
            "biggest_hit": self.biggest_hit, "biggest_taken": self.biggest_taken,
            "closest_call": self.lowest_hp_info,
            "spells": spells,
            "spell_eff": self.spell_effectiveness(),
            "buffs": {**self.buff_status(), "on": sorted(BUFFS.names() or []) if BUFFS else None,
                      "timers": [{"name": b["name"], "left": b.get("left")} for b in (BUFFS.buffs if BUFFS and BUFFS.names() is not None else []) if b["name"]]},
            "aggro": self.aggro_status(),
            "group": self.grp.snapshot(self.last_t or now, (self.player["x"], self.player["y"]),
                                       self.to_map_pct, self.cur_map, screen_party=self._screen_party()),
            "heal_eff": self.heal_effectiveness(),
            "mob_heals": {n: self.mob_heal_info(n) for n in self.mob_heals},
            "taken_by": sorted(({"name": k, **v} for k, v in self.taken_by.items()), key=lambda r: -r["dmg"])[:8],
            "failed": sorted(({"what": k, "n": v} for k, v in self.failed.items()), key=lambda r: -r["n"])[:6],
            "fights": fights,
            "trail": self.trail[-500:],
            "kill_times": [k["t"] for k in self.kills[-500:]],
            "dungeon_map": self._dungeon_map(),
            "zone_map": self._zone_map(),
            "minimap_status": MAPPER.status if MAPPER else None,
            "mapping": {"on": MAPPER.mapping_on, "map": (MAPPER.grids.grids.get((MAPPER.grids.cur or {}).get("gid")) or {}).get("name"),
                        "following": bool(MAPPER.grids.cur), "lost": MAPPER.grids.lost, "need": MAPPER.grids.need,
                        "status": MAPPER.grids.status, "known": MAPPER.grids.known_spots() if MAPPER.grids.need else []}
                       if MAPPER else None,
            "logs": logs_state(self),
            "kills_map": [[*pct, k["name"], k["map"]] for k in self.kills[-400:]
                          if (pct := self.to_map_pct(k.get("map"), k["x"], k["y"]))],
            "log_pos_map": self.to_map_pct(self.cur_map, self.player["x"], self.player["y"]),
            "cur_map": self.cur_map,
            "kill_marks": [[k["x"], k["y"], k["name"]] for k in self.kills[-300:] if k["x"] is not None],
            "death_marks": [[d["x"], d["y"]] for d in self.deaths if d["x"] is not None],
            "distance": self.distance,
            "timeline": self.timeline[-600:],
            "players_nearby": recent_players[:30], "players_total": len(self.players_seen),
            "hostiles": recent_hostiles,
            "feed": list(self.feed)[:25],
            "chat_active": self.chat_active,
            "copper": self.copper,
            "copper_per_hour": self.copper / active * 3600 if self.copper else 0,
            "loot": sorted(({"item": k, "n": v} for k, v in self.loot.items()), key=lambda r: -r["n"])[:10],
        }


# ---------------------------------------------------------------- file tailing

class Tailer(threading.Thread):
    def __init__(self, comp):
        super().__init__(daemon=True)
        self.comp = comp
        self.offsets = {}
        self.partial = {}

    def _files(self, pattern):
        cutoff = time.time() - SESSION_HOURS * 3600
        files = [f for f in glob.glob(os.path.join(LOG_DIR, pattern)) if os.path.getmtime(f) >= cutoff]
        return sorted(files, key=os.path.getmtime)

    def _read_new(self, path):
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        off = self.offsets.get(path, 0)
        if size < off:            # truncated / rotated
            off = 0
            self.partial[path] = b""
        if size == off:
            return []
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read(size - off)
        self.offsets[path] = size
        # the unfinished last line is kept as BYTES: decoded per read, an accented name cut in two by the
        # game's buffer became two broken characters - a player or mob that never existed
        raw = (self.partial.get(path) or b"") + data
        cut = raw.rfind(b"\n") + 1
        self.partial[path] = raw[cut:]
        lines = raw[:cut].decode("utf-8", errors="replace").split("\n")
        return [ln.rstrip("\r") for ln in lines if ln.strip()]

    def poll_once(self):
        for path in self._files("WoWCombatLog*.txt"):
            lines = self._read_new(path)
            if lines:
                with self.comp.lock:
                    for ln in lines:
                        try:                         # (the file position has moved on: one bad line must not
                            self.comp.feed_combat(ln)   #  throw away the rest of this chunk)
                        except Exception as e:
                            print("bad combat line:", repr(e), ln[:160], file=sys.stderr)
        cutoff = time.time() - SESSION_HOURS * 3600
        for path in self._files("WoWChatLog*.txt"):
            lines = self._read_new(path)
            if lines:
                with self.comp.lock:
                    for ln in lines:
                        t = parse_ts(ln)
                        if t is not None and t < cutoff:
                            continue
                        try:
                            self.comp.feed_chat(ln)
                        except Exception as e:
                            print("bad chat line:", repr(e), ln[:160], file=sys.stderr)

    _started_t = time.time()

    def _map_place(self):
        """Which zone map the coords under the minimap are on. Never a continent (tunnels/caves switch to
        the continent map: far too coarse, and a different coordinate system), never a dungeon.
        Coords only change map when the coords JUMP (zone line, hearth, flight); while they move smoothly
        they stay on the zone we settled on. Deciding a zone (at startup / after a jump) - only evidence that
        is about the coords RIGHT NOW counts:
          1. the picture: the live minimap clearly matches that zone's map at these coords;
          2. the crossing: the new coords put you a few yards from where you just were (zone world rects);
          3. last resort: label AND log agree, both newer than the jump, and you're 250+ yd from any other
             zone. (Alone, each lies at borders: the label switches early going in and late coming back;
             the log names wherever you were when it last flushed.)"""
        from location import CONTINENTS
        inst = self.comp.inst

        def ok(z):
            return bool(z) and z.strip().lower() not in CONTINENTS and not inst.lookup.for_name(z)
        fl = getattr(MAPPER, "flight", None) if MAPPER else None
        if fl:                                            # in the air: the mapper's flight chain knows the zone
            self._coord_zone, self._coord_zone_t = fl["zone"], time.time()
            return fl["zone"]
        landed = getattr(MAPPER, "flight_landed", None) if MAPPER else None
        if landed and landed[3] > getattr(self, "_landed_seen", 0):
            self._landed_seen = landed[3]
            self._coord_zone, self._coord_zone_t = landed[0], time.time()
            self._prev_pos = (landed[0], landed[1], landed[2], landed[3])
            self._zone_log(f"{landed[0]} - landed (followed by the flight chain) at {landed[1]},{landed[2]}")
            return landed[0]
        conflict = (getattr(MAPPER, "place_conflict_t", 0) or 0) if MAPPER else 0
        if conflict > getattr(self, "_conflict_seen", 0):
            self._conflict_seen = conflict              # a shot said the coords didn't jump with the zone switch
            # (the last sure spot stays: it's from the zone that WAS verified; the new switch was the wrong one)
            self._zone_log(f"refused switch to {getattr(MAPPER, 'place_now', None)}: coords didn't jump")
            self._coord_zone = None
        jump = max(getattr(LOC, "jump_t", 0) or 0, (getattr(MAPPER, "last_jump_t", 0) or 0) if MAPPER else 0, conflict)
        cz, czt = getattr(self, "_coord_zone", None), getattr(self, "_coord_zone_t", 0)
        if cz and czt < jump and self._still_walking(cz):
            self._coord_zone_t = time.time()             # "jump" but the coords are a walk away on the same map
            if MAPPER is not None:
                MAPPER.zone_decided_t = time.time()
        if cz and getattr(self, "_coord_zone_t", 0) >= jump:
            if LOC.x is not None and (LOC.t or 0) > getattr(self, "_coord_zone_t", 0) - 1:
                self._prev_pos = (cz, LOC.x, LOC.y, LOC.t)   # last spot on a settled map (for the next jump)
                if time.time() - getattr(self, "_prev_saved", 0) > 5:     # kept across restarts
                    self._prev_saved = time.time()
                    try:
                        json.dump(list(self._prev_pos), open(os.path.join(HERE, "last_pos.json"), "w"))
                    except OSError:
                        pass
            return cz                                     # coords moved smoothly since: same map
        try:
            import quadmap
            from worldmap import continent
            every = [z for z in quadmap.zone_bounds() if continent(z)]   # every zone's reading of these coords
        except Exception:
            every = []
        lab = self.comp.resolve_zone(LOC.zone, fallback=False) if LOC is not None and LOC.zone else None
        log, log_t = self.comp.cur_map, getattr(self.comp, "cur_map_t", 0) or 0
        z, how = self._zone_by_crossing(), "crossing"
        if not z:
            z, how = self._zone_by_picture([c for c in [lab, log] + every if ok(c)]), "picture"
            if z and ok(lab) and lab != z:
                self._zone_log(f"picture said {z} but the label names {lab}: not deciding")
                z = None                              # old mislabelled data can match a picture; a clear label wins
        if not z:
            lab_ok = lab if ok(lab) and (not jump or (getattr(LOC, "zone_ok_t", 0) or 0) > jump + 1) else None
            # the log's map only counts if it was named since the last jump - or, with no jump seen yet,
            # since this companion started (one from before a restart/relog says nothing about now)
            log_ok = log if ok(log) and log_t > (jump or getattr(self, "_started_t", 0)) else None
            if not log_ok and not jump and ok(log) and not lab and time.time() - log_t < 900:
                log_ok = log          # just restarted, the label is an area we can't place: a recent log map is fine
            if lab_ok and log_ok and lab_ok.strip().lower() == log_ok.strip().lower() and                     not self._near_other_zone(lab_ok, LOC.x, LOC.y):
                z, how = lab_ok, "label+log"
            elif lab_ok and not log_ok and time.time() - (getattr(LOC, "zone_changed_t", 0) or 0) >= 10                     and time.time() >= (getattr(MAPPER, "flying_until", 0) or 0):
                # the log hasn't named a map since the jump/login (it writes late): go with the label (read
                # twice since). It can be wrong right at a zone line - the audit moves those shots once the
                # log catches up.
                z, how = lab_ok, "label (log not caught up)"
        if not z:
            if time.time() - getattr(self, "_undecided_logged", 0) > 30:
                self._undecided_logged = time.time()
                self._zone_log(f"undecided at {LOC.x},{LOC.y}: label {LOC.zone!r}->{lab}, log {log} "
                               f"(t {log_t:.0f}), jump {jump:.0f}, near border {self._near_other_zone(lab, LOC.x, LOC.y) if ok(lab) else '-'}"
                               f", zone_ok_t {getattr(LOC, 'zone_ok_t', 0) or 0:.0f}")
            return None                                   # not sure which map these coords are on: don't map
        if False and how == "crossing" and LOC.zone and lab != z:   # (off: the label lags at crossings - it taught
                                                          # "Fargodeep Mine = Duskwood" on a border flight)
            lbl = LOC.zone.strip(" .")
            from location import ZONES
            if re.fullmatch(r"[A-Z][A-Za-z']+(?: (?:[A-Z][A-Za-z']+|of|the|and))*", lbl) and len(lbl) >= 4                     and lbl.lower() not in {q.lower() for q in ZONES}:
                subz = self.comp._subzones()
                # seen on both sides of a zone line (a river bank, a pass): never use it to pick a zone
                subz[lbl.lower()] = "" if lab and lab != z else z
                try:
                    with open(self.comp.SUBZONES_FILE, "w", encoding="utf-8") as f:
                        json.dump(subz, f, indent=1, sort_keys=True)
                except OSError:
                    pass
        if z != getattr(self, "_coord_zone_last", None) or how != getattr(self, "_zone_how", None):
            self._zone_log(f"{z} by {how} at {LOC.x},{LOC.y} (label {LOC.zone!r}, log {log})")
        self._coord_zone_last = z
        self._coord_zone, self._coord_zone_t, self._zone_how = z, time.time(), how
        if MAPPER is not None:
            MAPPER.await_map_after = 0
            MAPPER.zone_decided_t = time.time()           # (the mapper waits for this after a coords jump)
        return z

    def _audit_zones(self):
        """Self-correction: the combat log names the map for every moment (MAP_CHANGE, timestamped when it
        happened) but is written late. Once it has caught up past a shot, a shot filed under another zone
        is moved to the zone the log says; one taken on a continent map (tunnels) is dropped."""
        if MAPPER is None:
            return
        from location import CONTINENTS
        mc = sorted(getattr(self.comp, "map_changes", None) or [])
        covered = self.comp.last_t or 0                    # the log is written up to here
        if not mc or not covered:
            return
        q = MAPPER.quad
        moved = {}
        for p in q.places_list():
            place = p["place"]
            m = q.meta(place)
            for c in m["clips"]:
                if c.get("audited") or c["t"] < mc[0][0] or c["t"] > covered - 2:
                    continue
                truth = None
                for t_, name in mc:
                    if t_ <= c["t"] - 1:                  # (a second of slack: the switch moment itself)
                        truth = name
                    else:
                        break
                c["audited"] = True
                if truth is None or truth == place:
                    continue
                key = (place, None if truth.strip().lower() in CONTINENTS or self.comp.inst.lookup.for_name(truth) else truth)
                moved.setdefault(key, []).append(c["id"])
            q.save(place)
        for (src, dst), ids in moved.items():
            n = q.refile(src, ids, dst)
            self._zone_log(f"audit: {n} shots filed under {src} were in {dst or 'a continent/dungeon map'} (log) - "
                           + ("moved" if dst else "dropped"))

    def _zone_log(self, msg):
        try:
            with open(os.path.join(HERE, "zone_decisions.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S ") + msg + "\n")
        except OSError:
            pass

    def _near_other_zone(self, zone, x, y, yd=250):
        """Coords (x, y) on zone's map are within yd of another zone's rectangle (a border area)."""
        if x is None:
            return True
        import quadmap
        bb = quadmap.zone_bounds()
        b = bb.get(zone)
        if not b:
            return True
        wx, wy = b[0] - y / 100 * (b[0] - b[1]), b[2] - x / 100 * (b[2] - b[3])
        for z, o in bb.items():
            if z == zone:
                continue
            if o[0] >= b[0] and o[1] <= b[1] and o[2] >= b[2] and o[3] <= b[3]:
                continue                                  # a zone around this one (Dun Morogh around Ironforge)
            dx = max(o[1] - wx, 0, wx - o[0])            # north X > south X
            dy = max(o[3] - wy, 0, wy - o[2])            # west Y > east Y
            if math.hypot(dx, dy) < yd:
                return True
        return False

    def _still_walking(self, zone):
        """The coords now are within walking distance (on the same map) of the last settled spot."""
        prev = getattr(self, "_prev_pos", None)
        if not prev or prev[0] != zone or LOC.x is None:
            return False
        dt = max(1.0, (LOC.t or time.time()) - prev[3])
        import quadmap
        b = quadmap.zone_bounds().get(zone)
        if not b:
            return math.hypot(LOC.x - prev[1], LOC.y - prev[2]) < 2 + 0.3 * dt
        dyd = math.hypot((LOC.x - prev[1]) / 100 * (b[2] - b[3]), (LOC.y - prev[2]) / 100 * (b[0] - b[1]))
        return dyd < 30 + 15 * dt                       # mounted speed with room

    def _zone_by_crossing(self, jump=0):
        """Each zone reads the coords on the minimap as a different world spot, miles apart. The right zone is
        the one that puts you clearly nearest the last spot we were sure of, and close enough to have got
        there in the time since (walking or mounted). Works over a zone line, through a paused stretch, for
        areas never seen before. A hearth/teleport/flight lands too far from everything (-> None)."""
        prev = getattr(self, "_prev_pos", None)
        if prev is None and not getattr(self, "_prev_loaded", False):
            self._prev_loaded = True                      # the last sure spot from before a restart
            try:
                prev = self._prev_pos = tuple(json.load(open(os.path.join(HERE, "last_pos.json"))))
            except (OSError, ValueError, TypeError):
                prev = None
        if not prev or LOC.x is None or (LOC.t or 0) <= prev[3]:
            return None
        dt = (LOC.t or 0) - prev[3]
        if dt > 20:
            return None             # an old spot: a gryphon covers miles, and a wrong zone can fit by chance
        import quadmap
        bb = quadmap.zone_bounds()

        def world(z, x, y):
            b = bb.get(z)
            return None if not b else (b[0] - y / 100 * (b[0] - b[1]), b[2] - x / 100 * (b[2] - b[3]))
        w0 = world(prev[0], prev[1], prev[2])
        if not w0:
            return None
        d = sorted((math.hypot(w[0] - w0[0], w[1] - w0[1]), z) for z in bb for w in [world(z, LOC.x, LOC.y)] if w)
        if not d or d[0][0] > 60 + 14 * dt:
            return None                                   # nowhere reachable: a hearth / flight / teleport
        if len(d) > 1 and (d[1][0] < 3 * d[0][0] or d[1][0] - d[0][0] < 400):
            return None                                   # two zones fit about as well: don't guess
        return d[0][1]

    def _zone_by_picture(self, cands):
        """Label and log disagree: the zone whose existing map clearly matches the live minimap at these
        coords (and the other doesn't). Checked at most every 5 s."""
        if MAPPER is None or LOC is None or LOC.x is None or not cands:
            return None
        now = time.time()
        if now - getattr(self, "_zbp_t", 0) < 5:
            return getattr(self, "_zbp", None)
        self._zbp_t = now
        sims = {}
        for z in dict.fromkeys(cands):
            try:
                sims[z] = MAPPER.looks_like(z, LOC.x, LOC.y)
            except Exception:
                sims[z] = None
        good = [z for z, s in sims.items() if s is not None and s >= 0.6]
        bad_others = all(s is None or s < 0.3 for z, s in sims.items() if z not in good)
        self._zbp = good[0] if len(good) == 1 and bad_others else None
        return self._zbp

    def _group_snap(self):
        """Screenshot of the party frames for the run's record: 20 s after zoning in (past the loading
        screen) and again at 3 min in case someone joined late. Saved to instances/snaps/."""
        cur = self.comp.inst.cur
        if not cur:
            return
        age = time.time() - cur["start"]
        shots = cur.setdefault("snaps", [])
        if age > 600 or not ((age > 20 and len(shots) == 0) or (age > 180 and len(shots) == 1)):   # live runs only
            return
        try:
            from screenread import find_window, region
            _, rect = find_window("World of Warcraft")
            if not rect:
                return
            party = region("party") or {}
            ys = (party.get("hp_y") or [694, 1025])
            box = [0, max(0, min(ys) - 70), 300, min(1080, max(ys) + 40)]
            img = LOC._grab(rect, box)
            os.makedirs(os.path.join(HERE, "instances", "snaps"), exist_ok=True)
            fn = f"{int(cur['start'])}_{len(shots)}.png"
            img.save(os.path.join(HERE, "instances", "snaps", fn))
            shots.append(fn)
            self.comp.inst.save(force=True)
        except Exception as e:
            print("group snapshot failed:", e, file=sys.stderr)
            cur["snaps"] = shots + [None]          # don't retry every half second

    def _strip_feed(self):
        """What the add-on's strip has besides the numbers: the party (names, levels, classes) and the quest log
        go where the screen readers' results went; returns the target's name ("" = no target), or None when the
        strip has no texts (add-on off or old): then the target frame, party frames and tracker are read."""
        try:
            import beacon
            d = beacon.latest()
            if not d or not d.get("stats"):
                self.comp.strip_party = None
                return None
            pl, ql = beacon.party(d), beacon.quests(d)
        except Exception as e:
            print("strip feed error:", repr(e), file=sys.stderr)
            return None
        name = ((d.get("texts") or {}).get("name") or "").strip()
        with self.comp.lock:
            if name and name != self.comp.me_name:       # the add-on names your character at once (the log only
                self.comp.me_name = name                 # at its first fight): a switch of characters shows now
            self.comp.strip_party = pl
            for m in pl or ():
                if m["level"] and m["name"]:
                    self.comp.inst.note_level(m["name"], m["level"])
        if ql is not None and QUESTS is not None and not QUESTS.busy:
            old, new = [q["title"] for q in QUESTS.quests], [q["title"] for q in ql]
            if ql != QUESTS.quests:
                QUESTS.quests, QUESTS.t, QUESTS.error = ql, time.time(), None
            if getattr(self, "_strip_quests", False) and sorted(old) != sorted(new):
                self.comp.quests_refreshed(old, new)     # a quest left the log: it names the XP jump just before
            self._strip_quests = True                    # (not the first time: that list came off the tracker)
        return (d.get("texts") or {}).get("target")

    def _beacon_dungeon(self):
        """The dungeon the position add-on says you are inside right now (its "in an instance" flag + the
        instance id, read twice running), or None. Known the moment the loading screen ends, with the combat
        log off and whatever the minimap label reads."""
        try:
            import beacon
            b = beacon.read()
        except Exception:
            b = None
        now = b["inst"] if b and b.get("instance") else None
        prev, self._bdg_prev = getattr(self, "_bdg_prev", None), now
        if now is None or now != prev:
            return None
        dg = self.comp.inst.lookup.for_map(now)
        return dg if dg and dg.get("map_id") else None

    def run(self):
        while True:
            try:
                bs = beacon_stats()
                if SCREEN is not None:
                    st = SCREEN.snapshot()
                    if bs and st.get("ok"):              # the add-on's numbers instead of the bars' pixels
                        # (each only if the game lets add-ons have it: Forever keeps health and power secret,
                        #  so those two stay on the bars)
                        if bs["xpmax"]:
                            st["exact"] = bs
                            st["xp"] = round(bs["xp"] / bs["xpmax"], 4)
                            st["rested"] = round((bs["rested"] or 0) / bs["xpmax"], 4)
                        if bs["hpmax"]:
                            st["hp"] = round(bs["hp"] / bs["hpmax"], 4)
                        if bs["ptype"] == 0 and bs["powermax"]:
                            st["mana"] = round(bs["power"] / bs["powermax"], 4)
                        with self.comp.lock:
                            pl_ = self.comp.player
                            if bs["hpmax"]:
                                pl_["maxhp"] = bs["hpmax"]
                            if bs["ptype"] == 0 and bs["powermax"]:
                                pl_["maxpower"] = bs["powermax"]
                    self.comp.update_screen(st)
                tgt = self._strip_feed()                 # "" = no target, None = the strip has no texts
                if tgt is not None:
                    self.comp.ocr_target = tgt or None
                elif NAMER is not None:
                    self.comp.ocr_target = NAMER.name
                if LOC is not None:
                    bdg = self._beacon_dungeon()
                    with self.comp.lock:
                        LOC.set_map(self.comp.resolve_zone(LOC.zone))
                        # the minimap label names the dungeon the moment you zone in; the log may
                        # not write its ZONE_CHANGE until your first kill, so start the run now
                        inst = self.comp.inst
                        rooms = self.comp.dungeon_rooms
                        if inst.cur is not None and LOC.zone and not inst.lookup.for_name(LOC.zone)                                 and not self.comp.is_outdoor(LOC.zone) and LOC.zone.lower() not in rooms \
                                and len(LOC.zone.strip()) >= 4 and re.fullmatch(r"[A-Za-z' ]+", LOC.zone.strip()):
                            rooms[LOC.zone.lower()] = inst.cur["name"]      # a room inside this dungeon
                            self.comp.save_rooms()
                        if inst.cur is None and bdg:
                            # the add-on says "in an instance" + which one: no log line or readable label needed
                            inst.me = self.comp.me
                            inst.on_zone(time.time(), bdg["map_id"], bdg.get("zone") or bdg.get("name"))
                        elif inst.cur is None and LOC.zone:
                            dg = inst.lookup.for_name(LOC.zone) or inst.lookup.for_name(self.comp.room_of(LOC.zone))
                            if dg and dg.get("map_id"):
                                inst.me = self.comp.me
                                inst.on_zone(time.time(), dg["map_id"], dg.get("zone") or LOC.zone)
                        elif inst.cur is not None and bdg and bdg.get("map_id") == inst.cur.get("map_id"):
                            pass                             # the add-on still says inside: a misread label can't end the run
                        elif inst.cur is not None and LOC.zone and self.comp.is_outdoor(LOC.zone):
                            # leaving: the label reads an outdoor zone/area (read twice in a row by LOC)
                            inst.on_zone(time.time(), 0, LOC.zone)
                    self.comp.portrait_level = bs["level"] if bs else LOC.level
                    self._group_snap()

                    if MAPPER is not None:
                        # dungeons: snapshots placed by log position; test zones: by the coords under the minimap
                        try:
                            from screenread import region
                            extra = region("minimap_zones")
                        except Exception:
                            extra = ()
                        if getattr(self.comp, "ingame_since", None):   # one trail per login (back from the
                            MAPPER.grids.session = round(self.comp.ingame_since)   # character screen = new trail)
                        MAPPER.label_now = LOC.zone           # (Mapping uses these even before a zone is settled)
                        MAPPER.label_steady_s = time.time() - (getattr(LOC, "zone_changed_t", 0) or 0)
                        MAPPER.log_map = self.comp.cur_map
                        MAPPER.label_zone = self.comp.resolve_zone(LOC.zone, fallback=False) if LOC.zone else None
                        if self.comp.inst.cur and LOC.zone and self.comp.is_outdoor(LOC.zone):
                            MAPPER.place_now = None          # outside (ghost run, just left): don't map the dungeon
                        elif self.comp.inst.cur:
                            MAPPER.place_now, MAPPER.unit_now = map_place(self.comp.inst.cur), "world"
                        elif self._map_place() and LOC.x is not None and time.time() - LOC.t < 10:
                            # outdoors / cities: the coords under the minimap place every shot. The label
                            # above the minimap must agree with the log's map (a label that names another
                            # zone = the log hasn't caught up yet: e.g. Stormwind coords filed as Elwynn)
                            mp = self._map_place()
                            MAPPER.place_now, MAPPER.unit_now = mp, "coords"
                            if mp:
                                MAPPER.last_coords_place = mp
                        else:
                            MAPPER.place_now = None
                            MAPPER.unit_now = "coords"        # outdoors, zone not settled: Mapping can still run
                    # party levels: yours from the portrait, theirs when you target them (2 matching reads)
                    with self.comp.lock:
                        inst = self.comp.inst
                        my_level = bs["level"] if bs else LOC.level
                        if my_level and self.comp.me_name:
                            inst.note_level(self.comp.me_name, my_level)
                        pair = (self.comp.ocr_target, LOC.target_level)
                        if pair[0] and pair[1] and pair == getattr(self, "_tl_prev", None):
                            inst.note_level(pair[0], pair[1])
                        self._tl_prev = pair
            except Exception as e:  # (screen / zone reading: its errors used to stop the log being read at all)
                print("screen/zone error:", repr(e), file=sys.stderr)
            try:
                self.poll_once()
            except Exception as e:  # keep tailing no matter what one bad line does
                print("tail error:", repr(e), file=sys.stderr)
            time.sleep(0.5)


# ---------------------------------------------------------------- web server

COMP = Companion()
SCREEN = None
QUESTS = None
ASKER = None
NAMER = None
QWINDOW = None
MOBS = None
BUFFS = None
PARTY_NAMES = None
LOC = None
MAPPER = None           # minimap stitcher (maps of dungeons)
MARKERS_FILE = os.path.join(HERE, "markers.json")


def load_markers():
    try:
        with open(MARKERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


LOCAL_ONLY = ("/api/restore", "/api/import", "/api/trail/clear", "/api/label/rename", "/api/ask", "/api/answer")


def lan_urls():
    """Where another device on the home network opens the HUD (empty when "lan" is off in settings.json)."""
    return [f"http://{ip}:{PORT}/hud" for ip in wowpath.lan_ips()] if wowpath.lan_on() else []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        page = {"/": "index.html", "/index.html": "index.html", "/hud": "hud.html", "/hud.html": "hud.html",
                "/group": "group.html", "/group.html": "group.html",
                "/instance": "instance.html", "/instance.html": "instance.html",
                "/dungeon": "dungeon.html", "/dungeon.html": "dungeon.html",
                "/map": "map.html", "/map.html": "map.html"}.get(self.path.split("?")[0])
        if page:
            with open(os.path.join(HERE, page), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path.startswith("/api/dungeon"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            data = COMP.dungeon_snapshot((q.get("guide") or [None])[0])
            self._send(200, json.dumps(data, default=list).encode(), "application/json")
        elif self.path.startswith("/api/qr.svg"):                  # the tablet address as a QR code (qr.py)
            urls = lan_urls()
            if not urls:
                self._send(404, b"no network address", "text/plain")
            else:
                import qr
                self._send(200, qr.svg(urls[0]).encode(), "image/svg+xml")
        elif self.path.startswith("/api/backups"):
            import mapshare
            self._send(200, json.dumps({"backups": mapshare.list_backups(),
                                        "maps": [{"id": g, "name": (((MAPPER.grids.grids.get(g) or {}).get("name") or g) if MAPPER else g)}
                                                 for g in mapshare._grid_ids()]}).encode(), "application/json")
        elif self.path.startswith("/api/export"):
            import mapshare
            from urllib.parse import urlparse, parse_qs
            gid = os.path.basename((parse_qs(urlparse(self.path).query).get("map") or [""])[0])
            try:
                data = mapshare.export(gid)
            except (OSError, ValueError) as e:
                self._send(404, str(e).encode(), "text/plain")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{gid}_map_{time.strftime("%Y-%m-%d")}.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/api/maps"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            data = COMP.maps_snapshot((q.get("place") or [None])[0])
            self._send(200, json.dumps(data, default=float).encode(), "application/json")
        elif self.path.startswith("/api/instance"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            data = COMP.instance_snapshot((q.get("name") or [None])[0])
            self._send(200, json.dumps(data, default=list).encode(), "application/json")
        elif self.path.startswith("/api/threads"):
            # where every thread is right now (for "the page stopped updating": which reader is stuck, and where)
            import traceback
            names = {t.ident: t.name for t in threading.enumerate()}
            out = []
            for ident, frame in sys._current_frames().items():
                out.append(f"--- {names.get(ident, ident)}\n" + "".join(traceback.format_stack(frame)[-6:]))
            self._send(200, "\n".join(out).encode(), "text/plain; charset=utf-8")
        elif self.path.startswith("/api/state"):
            snap = COMP.snapshot()
            snap["quests"] = QUESTS.quests if QUESTS else []
            snap["strip"] = strip_summary()
            snap["gdi"] = gdi_handles()
            snap["lan"] = lan_urls()
            try:
                from quests import claude_mode
                snap["claude"] = claude_mode()               # "on" / "off" / "missing": quest look-ups
            except Exception:
                snap["claude"] = "missing"
            snap["local"] = self.client_address[0] in ("127.0.0.1", "::1")
            if LOC:
                from location import map_file
                loc = LOC.snapshot()
                zone = loc["zone"] or snap.get("cur_map") or snap.get("zone")
                mf = map_file(zone)
                loc["map_url"] = (f"/maps/{os.path.basename(mf)}?v={int(os.path.getmtime(mf))}"
                                  if mf and os.path.exists(mf) else None)
                loc["zone"] = zone
                snap["location"] = loc
            snap["markers"] = load_markers()
            snap["quests_info"] = {"busy": QUESTS.busy, "t": QUESTS.t, "error": QUESTS.error,
                                   "qwindow": QUESTS.qwindow} if QUESTS else None
            snap["asks"] = [{k: a.get(k) for k in ("id", "quest", "status", "answer", "by", "t")}
                            for a in ASKER.list()] if ASKER else []
            self._send(200, json.dumps(snap).encode(), "application/json")
        elif self.path.startswith("/minimap/"):
            name = os.path.basename(self.path.split("?")[0])
            stem = re.sub(r"_(?:w|L\d+)$", "", name[:-4])          # floors: <place>_L1.png ...
            fp = os.path.join(HERE, "maps", "minimap", stem, name)
            if name.endswith(".png") and os.path.exists(fp):
                with open(fp, "rb") as f:
                    self._send(200, f.read(), "image/png")
            else:
                self._send(404, b"not found", "text/plain")
        elif self.path.startswith("/grid/"):
            parts = self.path.split("?")[0].split("/")              # /grid/<id>/<tile>.png
            fp = os.path.join(HERE, "maps", "grids", os.path.basename(parts[2]), "tiles", os.path.basename(parts[-1])) if len(parts) >= 4 else ""
            if fp.endswith(".png") and os.path.exists(fp):
                with open(fp, "rb") as f:
                    self._send(200, f.read(), "image/png")
            else:
                self._send(404, b"not found", "text/plain")
        elif self.path.startswith("/world/"):
            parts = self.path.split("?")[0].split("/")              # /world/<continent>/<tile>.png
            fp = os.path.join(HERE, "maps", "world", os.path.basename(parts[2]), "tiles", os.path.basename(parts[-1])) if len(parts) >= 4 else ""
            if fp.endswith(".png") and os.path.exists(fp):
                with open(fp, "rb") as f:
                    self._send(200, f.read(), "image/png")
            else:
                self._send(404, b"not found", "text/plain")
        elif self.path.startswith("/quad/"):
            parts = self.path.split("?")[0].split("/")              # /quad/<place>/<file>.png
            fp = os.path.join(HERE, "maps", "quad", os.path.basename(parts[2]), os.path.basename(parts[-1])) if len(parts) >= 4 else ""
            if fp.endswith(".png") and os.path.exists(fp):
                with open(fp, "rb") as f:
                    self._send(200, f.read(), "image/png")
            else:
                self._send(404, b"not found", "text/plain")
        elif self.path.startswith("/snaps/"):
            name = os.path.basename(self.path.split("?")[0])
            fp = os.path.join(HERE, "instances", "snaps", name)
            if name.endswith(".png") and os.path.exists(fp):
                with open(fp, "rb") as f:
                    self._send(200, f.read(), "image/png")
            else:
                self._send(404, b"not found", "text/plain")
        elif self.path.startswith("/maps/"):
            name = os.path.basename(self.path.split("?")[0])
            fp = os.path.join(HERE, "maps", name)
            if name.endswith(".png") and os.path.exists(fp):
                with open(fp, "rb") as f:
                    self._send(200, f.read(), "image/png")
            else:
                self._send(404, b"not found", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def _json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_POST(self):
        # only this companion's own pages may change anything: any other web page open in the browser could
        # otherwise post here (restore a backup, start lookups, clear the trail)
        o = self.headers.get("Origin")
        if o and o.split("://", 1)[-1] != self.headers.get("Host"):
            self._send(403, b"cross-site request refused", "text/plain")
            return
        # a tablet or phone on the home network may look, set the level and switch Mapping; what can lose or
        # overwrite something is for the PC the companion runs on
        if self.path in LOCAL_ONLY and self.client_address[0] not in ("127.0.0.1", "::1"):
            self._send(403, json.dumps({"ok": False, "error": "Only from the PC the companion runs on"}).encode(),
                       "application/json")
            return
        if self.path in ("/api/backup", "/api/restore", "/api/import"):
            import mapshare
            try:
                if self.path == "/api/backup":
                    out = {"ok": True, "name": mapshare.backup("manual")}
                elif self.path == "/api/restore":
                    if MAPPER is not None:
                        MAPPER.set_mapping(False)
                    with COMP.lock:
                        gids = mapshare.restore(self._json().get("name") or "")
                        if MAPPER is not None:
                            MAPPER.reload_maps()
                    out = {"ok": True, "maps": gids}
                else:
                    n = int(self.headers.get("Content-Length", 0))
                    data = self.rfile.read(n)
                    if MAPPER is not None:
                        MAPPER.set_mapping(False)
                    with COMP.lock:
                        gid, added, how = mapshare.import_package(data)
                        if MAPPER is not None:
                            MAPPER.reload_maps()
                    out = {"ok": True, "map": gid, "added": added, "how": how}
                self._send(200, json.dumps(out).encode(), "application/json")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": str(e)}).encode(), "application/json")
            return
        if self.path == "/api/label/rename":
            try:
                body = self._json()
                old, new = (body.get("from") or "").strip(), (body.get("to") or "").strip()
                import location as _loc
                ok = _loc.rename_label(old, new)
                if ok:
                    if MAPPER is not None:
                        MAPPER.grids.rename_area(old, new)
                    sz = COMP._subzones()                      # the zone it's in carries over to the new name
                    if old.lower() in sz and new.lower() not in sz:
                        sz[new.lower()] = sz[old.lower()]
                        json.dump(sz, open(COMP.SUBZONES_FILE, "w", encoding="utf-8"), indent=1, sort_keys=True)
                    if LOC is not None and (LOC.zone or "").strip() == old:
                        LOC.zone = new
                self._send(200, json.dumps({"ok": ok, "to": new}).encode(), "application/json")
            except (ValueError, TypeError) as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            return
        if self.path == "/api/trail/clear":
            with COMP.lock:
                COMP.trail = []                                # the zone map's path
            if MAPPER is not None:
                MAPPER.grids.clear_trail()                     # the world map's path
            self._send(200, json.dumps({"ok": True}).encode(), "application/json")
            return
        if self.path == "/api/mapping" and MAPPER is not None:
            try:
                body = self._json()
                MAPPER.set_mapping(bool(body.get("on")), new=bool(body.get("new")))
            except (ValueError, TypeError):
                pass
            self._send(200, json.dumps({"on": MAPPER.mapping_on}).encode(), "application/json")
            return
        if self.path == "/api/ask" and ASKER:
            try:
                body = self._json()
                from quests import claude_mode
                if claude_mode() != "on":            # no look-up unless this PC's owner switched it on
                    self._send(200, json.dumps({"status": claude_mode()}).encode(), "application/json")
                    return
                snap = COMP.snapshot()
                ctx = {"level": (snap.get("xp") or {}).get("level"), "zone": snap.get("zone")}
                rec = ASKER.ask(body["quest"], ctx)
                self._send(200, json.dumps({"id": rec["id"], "status": rec["status"],
                                            "answer": rec.get("answer")}).encode(), "application/json")
            except (ValueError, KeyError, TypeError) as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            return
        if self.path == "/api/questwindow" and QUESTS:
            global QWINDOW
            try:
                from quests import read_quest_window
                known = list(QUESTS.quests) + [a["quest"] for a in ASKER.list() if a.get("quest")] if ASKER else list(QUESTS.quests)
                QWINDOW = read_quest_window(known)
                try:
                    from location import capture_map
                    QWINDOW["map_saved"] = capture_map()
                except Exception:
                    pass
            except Exception as e:
                QWINDOW = {"mode": "error", "error": str(e), "t": time.time()}
            self._send(200, json.dumps(QWINDOW).encode(), "application/json")
            return
        if self.path == "/api/capturemap":
            try:
                from location import capture_map
                cz = (LOC.map if LOC else None) or COMP.cur_map
                res = {"ok": True, "zone": capture_map(replace=True, known_zones=list(COMP.map_bounds), current_zone=cz)}
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            self._send(200, json.dumps(res).encode(), "application/json")
            return
        if self.path == "/api/quests/refresh" and QUESTS:
            QUESTS.refresh()
            self._send(200, b'{"ok": true}', "application/json")
            return
        if self.path == "/api/answer" and ASKER:
            try:
                body = self._json()
                ok = ASKER.answer(body["id"], body["text"], body.get("by", "chat"))
            except (ValueError, KeyError, TypeError):
                ok = False
            self._send(200 if ok else 400, json.dumps({"ok": ok}).encode(), "application/json")
            return
        if self.path == "/api/baseline":
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                lvl, xp = int(body["level"]), int(body.get("xp") or 0)
                if body.get("frac") is not None:        # how full the XP bar is: the XP is worked out here, where
                    xp = round(min(1.0, max(0.0, float(body["frac"]))) * xp_needed(lvl))   # every level's size is known
                ok = 1 <= lvl <= MAX_LEVEL and COMP.set_baseline(lvl, xp)
            except (ValueError, KeyError, TypeError):
                ok = False
            self._send(200 if ok else 400, json.dumps({"ok": ok}).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")


def main():
    if "--dump" in sys.argv:            # offline check: parse logs once, print the snapshot
        Tailer(COMP).poll_once()
        snap = COMP.snapshot()
        for k in ("trail", "timeline", "kill_marks"):
            snap[k] = f"<{len(snap[k])} points>"
        print(json.dumps(snap, indent=1, default=str))
        return
    if not os.path.isdir(LOG_DIR):
        if not os.path.isdir(os.path.dirname(LOG_DIR)):
            print(f"Can't find the game. Looked for: {os.path.dirname(LOG_DIR)}")
            print("Run Setup.bat, or put the game's folder in settings.json next to companion.py, like this:")
            print('  {"wow_dir": "D:/Games/World of Warcraft"}')
            sys.exit(1)
        os.makedirs(LOG_DIR, exist_ok=True)      # a game that has never logged: the folder comes with its first log
    global SCREEN, QUESTS, ASKER, NAMER, LOC, MOBS, BUFFS, PARTY_NAMES, MAPPER
    if "--no-screen" not in sys.argv:
        try:
            from screenread import ScreenReader
            SCREEN = ScreenReader()
            SCREEN.start()
            print("screen reading on (live HP / mana / XP / target / cooldowns)")
            from quests import QuestReader, Asker, TargetNamer
            NAMER = TargetNamer(SCREEN)
            NAMER.start()
            QUESTS = QuestReader(SCREEN)
            QUESTS.on_refresh = COMP.quests_refreshed
            QUESTS.start()
            ASKER = Asker()
            from quests import PartyNamer
            PARTY_NAMES = PartyNamer(SCREEN)
            PARTY_NAMES.start()
            from buffread import BuffReader
            BUFFS = BuffReader(SCREEN)
            BUFFS.start()
            from location import LocationReader
            try:                                         # the add-on's strip, 10 looks a second (numbers + names)
                import beacon
                beacon.start()
            except Exception as e:
                print("beacon reader not started:", repr(e), file=sys.stderr)
            LOC = LocationReader(SCREEN)
            LOC.start()
            from minimapper import MinimapMapper

            def _mm_grab(box):
                from screenread import find_window
                _, rect = find_window("World of Warcraft")
                return LOC._grab(rect, box) if rect else None
            MAPPER = MinimapMapper(_mm_grab)
            MAPPER.use_beacon = True                     # the position add-on's strip, when it is on screen
            MAPPER.grids.resolve_label = lambda lab: COMP.resolve_zone(lab, fallback=False) if lab else None
            try:                                                   # a backup when the newest is 6 h+ old
                import mapshare
                threading.Thread(target=mapshare.auto_backup, daemon=True).start()
            except Exception:
                pass
            try:
                MAPPER.grids.session = round(wow_started() or 0)     # one trail per play session
            except Exception:
                pass
            MAPPER.start()
            COMP.inst.on_my_pos = lambda t, x, y, place: MAPPER.position(place, "world", t, (x, y))
            COMP.inst.on_kill_now = lambda tk, ml, place: MAPPER.kill_snap(place, tk, ml)
            COMP.inst.live_me = MAPPER.live_pos
            COMP.inst.on_me_died = lambda t, place: MAPPER.on_death(place, t)
            print("location reading on")
            print("quest tracker reading on")
        except Exception as e:
            print("screen reading unavailable:", e)
    from mobinfo import MobInfo
    MOBS = MobInfo(os.path.join(os.path.dirname(LOG_DIR), "Cache", "WDB", "enUS", "creaturecache.wdb"))
    MOBS.start()
    from logarchive import LogArchiver
    LogArchiver(LOG_DIR).start()                       # permanent compressed copies of every log
    Tailer(COMP).start()
    # this PC + the home network (the owner's choice); "lan": false in settings.json keeps it to this PC
    srv = ThreadingHTTPServer(("0.0.0.0" if wowpath.lan_on() else "127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/hud"
    print(f"WoW companion on {url}  (reading {LOG_DIR})")
    for u in lan_urls():
        print(f"On a tablet or phone on the same network: {u}")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
