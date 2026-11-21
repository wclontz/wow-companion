"""Looks up mobs you haven't fought yet.

name -> NPC id   from WoW's own creature cache (Cache/WDB/enUS/creaturecache.wdb: every NPC your
                 client has seen) or from combat-log GUIDs (Creature-0-x-x-x-<id>-x)
id   -> info     Wowhead's public tooltip service (title, level, type, elite/rare, spawn points)
                 + wow-forever.gg's NPC page when it has one (level range, hostility, drops)
Results are cached forever in mobdb.json, so each mob is fetched once.
"""
import html
import json
import os
import re
import struct
import threading
import time
import urllib.request
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(HERE, "mobdb.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WoWCompanion/1.0 (personal second-screen tool)"
PRINTABLE = re.compile(rb"[\x20-\x7e]{2,}")


def parse_creature_cache(path):
    """{name_lower: (id, name)} from creaturecache.wdb ([id u32][size u32][data] records after a 24-byte header)."""
    out = {}
    try:
        with open(path, "rb") as _f:
            b = _f.read()
    except OSError:
        return out
    pos = 24
    while pos + 8 <= len(b):
        cid, size = struct.unpack_from("<II", b, pos)
        if size == 0 or pos + 8 + size > len(b):
            break
        data = b[pos + 8:pos + 8 + size]
        m = PRINTABLE.search(data, 8)
        if m and cid:
            name = m.group().decode("ascii", "replace").strip()
            if len(name) >= 3:
                out.setdefault(name.lower(), (cid, name))
        pos += 8 + size
    return out


def _get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _text(h):
    h = re.sub(r"<script.*?</script>|<style.*?</style>", "", h, flags=re.S)
    return [ln.strip() for ln in html.unescape(re.sub(r"<[^>]+>", "\n", h)).split("\n") if ln.strip()]


def fetch_wowhead(cid):
    j = json.loads(_get(f"https://nether.wowhead.com/forever/tooltip/npc/{cid}"))
    lines = _text(j.get("tooltip", ""))
    info = {"id": cid, "name": j.get("name"), "title": None, "level": None, "type": None, "rank": None}
    for ln in lines[1:]:
        m = re.match(r"Level\s+([\d?\-– ]+)\s*(.*?)\s*(\((Elite|Rare|Rare Elite|Boss)\))?$", ln)
        if m:
            info["level"] = m.group(1).strip()
            typ = (m.group(2) or "").replace("(Normal)", "").strip()
            info["type"] = typ or None
            info["rank"] = m.group(4)
        elif ln != info["name"] and not info["title"]:
            info["title"] = ln
    coords = (j.get("map") or {}).get("coords") or {}
    pts = [p for v in coords.values() for p in v] if isinstance(coords, dict) else []
    info["spawns"] = pts[:40]
    return info


def fetch_forevergg(cid, name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    try:
        lines = _text(_get(f"https://wow-forever.gg/db/npcs/{cid}-{slug}/"))
    except Exception:
        return {}
    out = {}
    for i, ln in enumerate(lines):
        m = re.match(r"Level\s+([\d\-–]+)\s+·\s+(\w+)", ln)
        if m and "level_range" not in out:
            out["level_range"], out["reaction"] = m.group(1), m.group(2)
        if ln == "Drops" and i + 1 < len(lines) and lines[i + 1].isdigit() and "drops" not in out:
            drops, j = [], i + 1
            while j < len(lines) and len(drops) < 6:
                if lines[j] == "Drop" and j >= 1 and j + 3 < len(lines):
                    rate = lines[j + 3]
                    drops.append({"item": lines[j - 1], "rate": rate if "%" in rate else None})
                j += 1
                if lines[j - 1].startswith("Checked in game"):
                    break
            out["drops"] = drops
        m = re.match(r"Coins:", ln)
        if m and i + 3 < len(lines):
            out["coins"] = f"{lines[i + 1]}–{lines[i + 3]}c"
    return out


class MobInfo(threading.Thread):
    def __init__(self, cache_path):
        super().__init__(daemon=True)
        self.cache_path = cache_path
        self.lock = threading.Lock()
        self.db = self._load()
        self.index, self.index_mtime = {}, 0
        self.log_ids = {}                         # name_lower -> id, from combat-log GUIDs
        self.want = []                            # names waiting to be fetched
        self.wake = threading.Event()

    def _load(self):
        try:
            with open(DB_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            # (misses are not kept: 822 of 1,166 saved entries were names the screen misread)
            keep = {k: v for k, v in list(self.db.items()) if "error" not in v}
            with open(DB_FILE + ".tmp", "w", encoding="utf-8") as f:
                json.dump(keep, f, indent=1)
            os.replace(DB_FILE + ".tmp", DB_FILE)
        except OSError:
            pass

    def note_guid(self, guid, name):
        """Feed creature GUIDs from the combat log: the 6th field is the NPC id."""
        if guid and guid.startswith("Creature-") and name:
            parts = guid.split("-")
            if len(parts) > 5 and parts[5].isdigit():
                self.log_ids.setdefault(name.lower(), int(parts[5]))

    def _refresh_index(self):
        try:
            mt = os.path.getmtime(self.cache_path)
        except OSError:
            return
        if mt != self.index_mtime:
            self.index, self.index_mtime = parse_creature_cache(self.cache_path), mt

    def id_for(self, name):
        key = name.lower().strip()
        if key in self.log_ids:
            return self.log_ids[key], name
        self._refresh_index()
        if key in self.index:
            return self.index[key]
        best = max(self.index, key=lambda k: SequenceMatcher(None, k, key).ratio(), default=None)
        if best and SequenceMatcher(None, best, key).ratio() >= 0.85:
            return self.index[best]                # OCR misspelling -> the real name
        return None, None

    def get(self, name):
        """Cached info for a mob name, or None (and queue a lookup)."""
        if not name:
            return None
        key = name.lower().strip()
        with self.lock:
            rec = self.db.get(key)
            if rec is None and key not in self.want:
                self.want.append(key)
                self.wake.set()
        return rec

    def run(self):
        while True:
            self.wake.wait(5)
            self.wake.clear()
            while True:
                with self.lock:
                    if not self.want:
                        break
                    key = self.want.pop(0)
                    if key in self.db:
                        continue
                cid, real = self.id_for(key)
                rec = {"name": real or key, "id": cid, "t": time.time()}
                if cid:
                    try:
                        rec.update(fetch_wowhead(cid))
                    except Exception as e:
                        rec["error"] = f"wowhead: {e}"
                    rec.update(fetch_forevergg(cid, rec.get("name") or key))
                else:
                    rec["error"] = "not in your game's creature cache yet"
                with self.lock:
                    # don't cache failures forever: allow a retry after 10 minutes
                    if cid and "error" not in rec:
                        self.db[key] = rec
                        if (real or "").lower() != key:
                            self.db[(real or key).lower()] = rec
                        self._save()
                    else:
                        self.db[key] = {**rec, "retry_after": time.time() + 600}
                time.sleep(1.0)                      # be gentle with the sites

    def lookup(self, name):
        rec = self.get(name)
        if rec and rec.get("retry_after") and time.time() > rec["retry_after"]:
            with self.lock:
                self.db.pop(name.lower().strip(), None)
            return self.get(name)
        return rec
