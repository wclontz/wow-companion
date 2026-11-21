"""Each character's progress over time, kept for good in character_log.jsonl (one line a reading):

  {"t", "name", "level", "xp", "xpmax", "money", "gear" (our gear score), "ilvl", "zone",
   "sess" (the play session it belongs to), "kills", "deaths", "played" (seconds; these three count up inside a session)}

A line is written when a character is first seen, when its level changes, and otherwise at most every 10 minutes
and only if something changed. The level, XP, money and gear are the add-on's own numbers for the character on
screen, so a line is only written while the add-on's strip is being read. The file stays on this PC.
"""
import json
import os
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(HERE, "character_log.jsonl")
EVERY = 600


def load(path=None):
    """Every reading in the file, oldest first: {name: [reading, ...]}."""
    out = {}
    try:
        with open(path or FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    out.setdefault(r["name"], []).append(r)
                except (ValueError, KeyError, TypeError):
                    continue                             # one bad line (cut short) must not lose the rest
    except OSError:
        pass
    for rows in out.values():
        rows.sort(key=lambda r: r["t"])
    return out


def worth_writing(last, rec, every=EVERY):
    """Is this reading worth a line after `last` (the character's newest line, or None)?"""
    if last is None or rec["level"] != last.get("level"):
        return True
    if any(last.get(k) is None and rec.get(k) is not None for k in ("gear", "sess", "zone")):
        return True                                      # the last line was written before this was known
    if rec["t"] - last["t"] < every:
        return False
    return any(rec.get(k) != last.get(k) for k in ("xp", "money", "gear", "kills", "deaths", "zone"))


def summary(rows, points=300):
    """One character's readings -> what the page shows: where it stands, lifetime totals (the per-session counts
    added up), each level's first sighting, and the readings thinned to `points` for the charts."""
    if not rows:
        return None
    tot = {"kills": 0, "deaths": 0, "played": 0}
    best, series, levels = {}, [], []
    for r in rows:
        if r.get("sess") != best.get("sess"):            # a new session: the last one's counts are final
            for k in tot:
                tot[k] += best.get(k) or 0
            best = {"sess": r.get("sess")}
        for k in tot:
            if r.get(k) is not None:
                best[k] = max(best.get(k) or 0, r[k])
        run = {k: tot[k] + (best.get(k) or 0) for k in tot}
        if not levels or r["level"] != levels[-1]["level"]:
            levels.append({"level": r["level"], "t": r["t"], "played": run["played"]})
        frac = r["xp"] / r["xpmax"] if r.get("xp") is not None and r.get("xpmax") else 0
        series.append([round(r["t"]), round(r["level"] + min(frac, 0.999), 3), r.get("money"), r.get("gear"),
                       run["kills"], run["deaths"], round(run["played"])])
    step = max(1, -(-len(series) // points))             # (rounded up: never more than `points`, plus the last)
    thin = series[::step]
    if thin[-1] is not series[-1]:
        thin.append(series[-1])
    last = rows[-1]
    return {"name": last["name"], "now": last, "first_t": rows[0]["t"], "readings": len(rows),
            "kills": run["kills"], "deaths": run["deaths"], "played": round(run["played"]),
            "levels": levels, "series": thin}


class CharLog(threading.Thread):
    """Asks `take()` for the current reading once a minute and writes the lines worth keeping.
    take() -> a reading without "t", or None when there is nothing trustworthy to write right now."""

    def __init__(self, take, path=None, period=60):
        super().__init__(daemon=True)
        self.take, self.path, self.period = take, path or FILE, period
        self.lock = threading.Lock()
        self.rows = load(self.path)
        self.error = None

    def note(self, rec, now=None):
        """Write `rec` if it is worth a line. -> True when written."""
        if not rec or not rec.get("name") or not rec.get("level"):
            return False
        rec = dict(rec, t=round(now or time.time(), 1))
        with self.lock:
            mine = self.rows.setdefault(rec["name"], [])
            if not worth_writing(mine[-1] if mine else None, rec):
                return False
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            mine.append(rec)
        return True

    def run(self):
        while True:
            try:
                self.note(self.take())
                self.error = None
            except Exception as e:                       # never let a bad reading stop the log
                self.error = repr(e)
            time.sleep(self.period)

    def snapshot(self):
        with self.lock:
            chars = [summary(rows) for rows in self.rows.values()]
        chars = [c for c in chars if c]
        chars.sort(key=lambda c: -c["now"]["t"])
        return {"chars": chars, "error": self.error}
