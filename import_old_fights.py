"""One-off: file every fight in every combat log (current Logs folder + the gzipped archive) into
mobfights.jsonl. Your level isn't in old logs (Forever doesn't log level-ups), so these are filed with
no level and show up as 'below lvl N'. Safe to re-run: fights already filed are skipped.
Run with the companion stopped (it writes the same file)."""
import glob
import gzip
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import companion as C  # noqa: E402

logs = {}
for p in glob.glob(os.path.join(HERE, "combat_logs", "WoWCombatLog*.txt.gz")):
    logs[os.path.basename(p)[:-3]] = p
for p in glob.glob(os.path.join(C.LOG_DIR, "WoWCombatLog*.txt")):
    logs[os.path.basename(p)] = p                   # the live copy is the most complete one

c = C.Companion()
c.inst.save = lambda *a, **k: None          # only fights get filed: never touch dungeon run records
before = sum(len(v) for v in c.mob_fights.values())
for name in sorted(logs):                           # names carry the date/time: oldest first
    p = logs[name]
    opener = gzip.open if p.endswith(".gz") else open
    n = 0
    with opener(p, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                c.feed_combat(line)
            except Exception:
                pass
            n += 1
    print(f"{name}: {n:,} lines")
after = sum(len(v) for v in c.mob_fights.values())
print(f"\nfiled {after - before} new fights ({after} in total) into {c.MOBFIGHTS_FILE}")
solo = Counter(r["name"] for rs in c.mob_fights.values() for r in rs if r.get("solo"))
print("most solo fights:", ", ".join(f"{n} {k}" for k, n in solo.most_common(10)))
