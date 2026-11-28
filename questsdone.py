"""The quests you have finished, kept for good in quests_done.jsonl (one line a quest; the file stays on this PC).

Two sources:
  * the chat log's own line "<quest> completed." (needs /chatlog; the add-on switches it on at login). A chat
    line names no character, so each is given to whoever the character log (character_log.jsonl) shows was
    played at that time, or to a hand-given period (a line {"from", "to", "name"} in the file), or to nobody.
  * the game's own list of finished quests, which the add-on writes into its saved file when you log out or
    /reload (`game_list`). It has no dates, but it also knows the quests done while the chat log was off.

  {"t", "quest", "who" (name or null)}            a quest finished
  {"from", "to", "name"}                          lines in that time without a name belong to this character
"""
import bisect
import glob
import gzip
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(HERE, "quests_done.jsonl")
CHARLOG = os.path.join(HERE, "character_log.jsonl")
NEAR = 1800.0           # s: a character-log reading this close to a chat line says who was played

_LINE = re.compile(r"^(\d+)/(\d+)(?:/(\d+))? (\d+):(\d+):(\d+)\.(\d+)\s+(.{1,120}?) completed\.\s*$")
# somebody's chat that happens to end the same way: a link, a channel tag, "Name says: ...", "To Name: ..."
# (a quest's own name may hold ": " - "WANTED: Murkdeep!" - so that alone says nothing)
_NOT_QUEST = re.compile(r"\|c|\|H|^\[|^\S+ (?:says|yells|whispers):(?: |$)|^To \S+:(?: |$)")


def parse(line, now=None):
    """A chat log line -> (time, quest name) when it is the game's "<quest> completed.", else None."""
    m = _LINE.match(line or "")
    if not m:
        return None
    mo, d, y, hh, mm, ss, ms, quest = m.groups()
    if _NOT_QUEST.search(quest):
        return None
    now = now or datetime.now()
    try:
        dt = datetime(int(y) if y else now.year, int(mo), int(d), int(hh), int(mm), int(ss), int(ms[:3]) * 1000)
        if not y and dt > now + timedelta(days=2):
            dt = dt.replace(year=dt.year - 1)            # (no year in a chat line: December is not the future)
    except ValueError:
        return None
    return dt.timestamp(), quest.strip()


class Who:
    """Who was played when, from the character log."""

    def __init__(self, path=None):
        self.path, self.sig, self.ts, self.names = path or CHARLOG, None, [], []

    def _load(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return
        if (st.st_mtime, st.st_size) == self.sig:
            return
        rows = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        rows.append((float(r["t"]), str(r["name"])))
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            return
        rows.sort()
        self.ts, self.names, self.sig = [r[0] for r in rows], [r[1] for r in rows], (st.st_mtime, st.st_size)

    def at(self, t):
        self._load()
        i = bisect.bisect(self.ts, t)
        near = [j for j in (i - 1, i) if 0 <= j < len(self.ts) and abs(self.ts[j] - t) <= NEAR]
        return self.names[min(near, key=lambda j: abs(self.ts[j] - t))] if near else None


class Done:
    def __init__(self, path=None, who=None):
        self.path, self.who = path or FILE, who or Who()
        self.lock = threading.Lock()
        self.rows, self.seen, self.periods = [], set(), []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and "from" in r:
                        self.periods.append(r)
                    elif isinstance(r, dict) and "quest" in r and "t" in r:
                        self._keep(r)
        except OSError:
            pass

    def _keep(self, r):
        key = (round(r["t"]), r["quest"])
        if key in self.seen:
            return False
        self.seen.add(key)
        self.rows.append(r)
        return True

    def _append(self, r):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def add(self, t, quest, who=None):
        """A quest finished at `t`. -> True when it is new (the log is read again at every start)."""
        with self.lock:
            if (round(t), quest) in self.seen:
                return False
            r = {"t": round(t, 1), "quest": quest[:120], "who": who or self.who.at(t)}
            self._keep(r)
            self._append(r)
            return True

    def feed(self, line):
        """A chat log line (any line). -> True when it was a new finished quest."""
        hit = parse(line)
        return bool(hit) and self.add(*hit)

    def name_period(self, t0, t1, name):
        """Lines from t0 to t1 that have no name belong to `name` (for the time before the character log)."""
        with self.lock:
            p = {"from": t0, "to": t1, "name": name}
            self.periods.append(p)
            self._append(p)

    def _name(self, r):
        if r.get("who"):
            return r["who"]
        for p in self.periods:
            if p["from"] <= r["t"] <= p["to"]:
                return p["name"]
        return self.who.at(r["t"])                       # (the character log may have caught up since)

    def backfill(self, files):
        """Every chat log there is (plain or .gz), read whole. -> how many quests were new."""
        n = 0
        for path in files:
            try:
                f = gzip.open(path, "rt", encoding="utf-8", errors="replace") if path.endswith(".gz") \
                    else open(path, encoding="utf-8", errors="replace")
                with f:
                    for line in f:
                        n += self.feed(line)
            except (OSError, EOFError):
                continue
        return n

    def snapshot(self, game=None):
        """What the page shows: [{"name" (None: not known), "n", "names", "days": [{"day", "quests": [{"t", "quest",
        "times"}]}], "game": {...} or None}], the character with the newest quest first. `game` = game_list()."""
        with self.lock:
            rows = sorted(self.rows, key=lambda r: r["t"])
            by = {}
            for r in rows:
                by.setdefault(self._name(r), []).append(r)
        chars = []
        for name, rs in by.items():
            days, count = [], {}
            for r in rs:
                count[r["quest"]] = count.get(r["quest"], 0) + 1
                day = time.strftime("%Y-%m-%d", time.localtime(r["t"]))
                if not days or days[-1]["day"] != day:
                    days.append({"day": day, "quests": []})
                days[-1]["quests"].append({"t": round(r["t"]), "quest": r["quest"]})
            days.reverse()
            for d in days:
                d["quests"].reverse()
            c = {"name": name, "n": len(rs), "names": len(count), "first": round(rs[0]["t"]), "last": round(rs[-1]["t"]),
                 "days": days, "game": None}
            g = (game or {}).get(name) if name else None
            if g:                                        # the game's list: what the chat log never showed
                had = {q.lower() for q in count}
                c["game"] = {"n": g["n"], "t": g.get("t"),
                             "only": sorted(q for q in g["titles"] if q.lower() not in had),
                             "unnamed": g["n"] - len(g["titles"])}
            chars.append(c)
        for name, g in (game or {}).items():             # a character only the game's list knows
            if name not in by:
                chars.append({"name": name, "n": 0, "names": 0, "first": None, "last": 0, "days": [],
                              "game": {"n": g["n"], "t": g.get("t"), "only": sorted(g["titles"]),
                                       "unnamed": g["n"] - len(g["titles"])}})
        chars.sort(key=lambda c: (c["name"] is None, -c["last"]))
        return {"chars": chars, "total": len(rows)}


def chat_logs(log_dir):
    """Every chat log we have: the archive's copies first, then the game's own (newest lines last)."""
    return sorted(glob.glob(os.path.join(HERE, "combat_logs", "WoWChatLog*.gz"))) + \
        (sorted(glob.glob(os.path.join(log_dir, "WoWChatLog*.txt"))) if log_dir else [])


# ---- the game's own list, from the add-on's saved file (read only; it is rewritten at logout and /reload)
_BLOCK = re.compile(r'\["quests_done"\]\s*=\s*\{')
_CHAR = re.compile(r'^\s*\["((?:[^"\\]|\\.){1,40})"\]\s*=\s*\{\s*$')
_ENTRY = re.compile(r'^\s*\["?(\d{1,7})"?\]\s*=\s*(?:"((?:[^"\\]|\\.)*)"|true),?\s*$')


def saved_files(wow_dir):
    """The add-on's saved files under the game's folder (one an account)."""
    return glob.glob(os.path.join(wow_dir, "WTF", "Account", "*", "SavedVariables", "CompanionBeacon.lua")) if wow_dir else []


def game_list(paths):
    """{character: {"n": quests the game says are done, "titles": [those it could name], "t": the file's time}}.
    The file is the add-on's (text another program wrote): only the plain shapes we write are read."""
    out = {}
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read(4_000_000)
            mt = os.path.getmtime(path)
        except OSError:
            continue
        m = _BLOCK.search(text)
        if not m:
            continue
        name = None
        for line in text[m.end():].split("\n"):
            c = _CHAR.match(line)
            if c:
                name = c.group(1)[:40]
                out[name] = {"n": 0, "titles": [], "t": round(mt)}
                continue
            e = _ENTRY.match(line) if name else None
            if e:
                out[name]["n"] += 1
                if e.group(2):
                    out[name]["titles"].append(e.group(2).replace('\\"', '"').replace("\\\\", "\\")[:120])
            elif line.strip().startswith("}"):
                if name is None:
                    break                                # the end of quests_done
                name = None
    return out
