"""Item quality + vendor value by name, looked up on Wowhead (Forever) and cached forever in itemcache.json.

The chat log has plain item names (no links/colours), so the name is searched once, then the tooltip
gives the sell price. Random-suffix items ("Nomad Tunic of the Eagle") fall back to the base name.
"""
import html as htmlmod
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "itemcache.json")
UA = {"User-Agent": "Mozilla/5.0 WoWCompanion"}
QUALITY = ["poor", "common", "uncommon", "rare", "epic", "legendary", "artifact", "heirloom"]


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _sell(html):
    m = re.search(r"Sell Price:(.*?)</div>", html or "")
    if not m:
        return 0
    return sum(int(amt) * {"gold": 10000, "silver": 100, "copper": 1}[unit]
               for unit, amt in re.findall(r'money(gold|silver|copper)">(\d+)', m.group(1)))


# random-suffix enchant names -> which stats they roll (amounts vary per drop)
SUFFIX = {"the Eagle": "Stamina, Intellect", "the Monkey": "Agility, Stamina", "the Bear": "Strength, Stamina",
          "the Tiger": "Strength, Agility", "the Owl": "Intellect, Spirit", "the Whale": "Stamina, Spirit",
          "the Boar": "Strength, Spirit", "the Falcon": "Agility, Intellect", "the Wolf": "Agility, Spirit",
          "the Gorilla": "Strength, Intellect", "Power": "Attack Power", "Healing": "Healing power",
          "Spirit": "Spirit", "Intellect": "Intellect", "Stamina": "Stamina", "Strength": "Strength",
          "Agility": "Agility", "Defense": "Defense", "Blocking": "Block value", "Marksmanship": "Ranged AP",
          "Fiery Wrath": "Fire spell damage", "Frozen Wrath": "Frost spell damage",
          "Shadow Wrath": "Shadow spell damage", "Arcane Wrath": "Arcane spell damage",
          "Nature's Wrath": "Nature spell damage", "Sorcery": "Spell damage", "Regeneration": "Health per 5 sec",
          "Concentration": "Mana per 5 sec", "Shadow Protection": "Shadow resistance",
          "Fire Resistance": "Fire resistance", "Frost Resistance": "Frost resistance",
          "Nature Resistance": "Nature resistance", "Arcane Resistance": "Arcane resistance"}


def tooltip_lines(tt, name):
    """Wowhead tooltip HTML -> [[kind, text], ...] for our own hover box."""
    s = re.sub(r"<!--.*?-->", "", tt or "")
    s = re.sub(r'<div class="whtt-sellprice">.*?</div>', "", s)
    s = re.sub(r"</td>\s*<th>", "\t", s)
    s = re.sub(r"<br\s*/?>|</tr>|</div>|</table>|<table[^>]*>", "\n", s)
    s = htmlmod.unescape(re.sub(r"<[^>]+>", "", s))
    out = []
    for ln in s.split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        kind = "use" if re.match(r"(Equip|Use|Chance on hit):", ln) else "stat" if re.match(r"[+-]\d", ln) else ""
        out.append([kind, ln])
    if out:
        out[0] = ["name", name]                      # show the full name (with its suffix)
    m = re.search(r" of ((?:the )?[A-Z][\w']*(?: [A-Z][\w']*)?)$", name)
    if m and m.group(1) in SUFFIX and len(out) > 1:
        at = next((i for i, l in enumerate(out) if l[1].startswith(("Durability", "Requires"))), len(out))
        out.insert(at, ["stat","+ " + SUFFIX[m.group(1)] + " (random amount)"])
    return out


class ItemDB:
    def __init__(self):
        try:
            self.items = json.load(open(CACHE, encoding="utf-8"))
        except (OSError, ValueError):
            self.items = {}
        self.queue = []
        self.lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def get(self, name):
        """Cached info or None (and queues a lookup)."""
        it = self.items.get(name)
        if it is None or (it.get("id") and "tt" not in it):     # new, or cached before tooltips were kept
            with self.lock:
                if name not in self.queue:
                    self.queue.append(name)
        return it

    def _find(self, name):
        tries = [name]
        m = re.match(r"(.+?) of ((?:the )?[A-Z][\w']*(?: [A-Z][\w']*)?)$", name)   # random suffix
        if m and m.group(2) in SUFFIX:
            tries.append(m.group(1))
        for q in tries:
            res = _get("https://www.wowhead.com/forever/search/suggestions-template?q=" + urllib.parse.quote(q))
            hits = [r for r in res.get("results", []) if r.get("type") == 3]
            hit = next((r for r in hits if r["name"].lower() == q.lower()), None)
            if hit:
                return hit
        return None

    def _run(self):
        while True:
            with self.lock:
                name = self.queue.pop(0) if self.queue else None
            if not name:
                time.sleep(2)
                continue
            try:
                hit = self._find(name)
                if hit:
                    tt = _get(f"https://nether.wowhead.com/forever/tooltip/item/{hit['id']}")
                    info = {"id": hit["id"], "q": hit.get("quality", 1), "icon": hit.get("icon"),
                            "sell": _sell(tt.get("tooltip")), "slot": hit.get("pinFooterText"),
                            "tt": tooltip_lines(tt.get("tooltip"), name)}
                else:
                    info = {"id": None, "q": None, "sell": 0}
                self.items[name] = info
                json.dump(self.items, open(CACHE, "w", encoding="utf-8"), indent=0)
            except Exception:
                time.sleep(10)                                  # offline: try again later
                with self.lock:
                    self.queue.append(name)
            time.sleep(0.5)
