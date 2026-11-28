"""Places you have used, remembered for the maps: vendors, repairers, trainers, flight masters, mailboxes, banks,
inns, and the herbs / veins / fish you gathered.

The add-on says WHAT happened (its text 24: "vendor <name>", "gather Herb Gathering: Peacebloom"); the companion
knows where you stood at that moment (the strip's world position). Kept in places.json beside the program
(never shared: it is from your play).
"""
import json
import math
import os
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(HERE, "places.json")
KINDS = ("vendor", "repair", "trainer", "flight", "mail", "bank", "inn", "gather", "exit", "entrance")
SAME = {"gather": 12.0, "exit": 150.0, "entrance": 60.0}   # yd: the same kind (and name) this close is the same place (others: 25)
MOST = 4000


class Places:
    def __init__(self, path=FILE):
        self.path, self.lock = path, threading.Lock()
        try:
            rows = json.load(open(path, encoding="utf-8"))
            self.rows = [r for r in rows if isinstance(r, dict) and r.get("kind") in KINDS]
        except (OSError, ValueError):
            self.rows = []

    def add(self, kind, name, continent, ns, we, zone=None, now=None):
        """-> "new", "seen" (already known: its count and time go up) or None (not a place / no position)."""
        if kind not in KINDS or continent is None or ns is None or we is None:
            return None
        now = time.time() if now is None else now
        name = (name or "").strip()[:80]
        with self.lock:
            for r in self.rows:
                if r["kind"] == kind and r["cont"] == continent and r["name"] == name \
                        and math.hypot(r["ns"] - ns, r["we"] - we) <= SAME.get(kind, 25.0):
                    r["n"], r["t"] = r.get("n", 1) + 1, round(now)
                    self._save()
                    return "seen"
            self.rows.append({"kind": kind, "name": name, "cont": continent, "ns": round(ns, 1), "we": round(we, 1),
                              "zone": zone or None, "n": 1, "t": round(now)})
            if len(self.rows) > MOST:                    # the oldest gathering spots go first
                self.rows.sort(key=lambda r: (r["kind"] != "gather", r["t"]))
                del self.rows[:len(self.rows) - MOST]
            self._save()
            return "new"

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.rows, f)
        os.replace(tmp, self.path)

    def on_map(self, continent, bounds):
        """The places of a continent in a zone map's coords: bounds = that zone's (north, south, west, east) in
        world yards -> [{"kind", "label", "fx", "fy", "n"}] (also the ones outside the zone: fx / fy run past
        0-100, as the continent map is drawn in one zone's terms)."""
        if not bounds:
            return []
        N, S, W, E = bounds
        if N == S or W == E:
            return []
        with self.lock:
            return [{"kind": r["kind"], "label": r["name"], "n": r.get("n", 1),
                     "fx": round((W - r["we"]) / (W - E) * 100, 2), "fy": round((N - r["ns"]) / (N - S) * 100, 2)}
                    for r in self.rows if r["cont"] == continent]
