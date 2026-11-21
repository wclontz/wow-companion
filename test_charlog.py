"""The character progress log: which readings get a line, and the lifetime totals over several sessions."""
import os
import tempfile

import charlog

base = {"name": "Testa", "level": 10, "xp": 100, "xpmax": 1000, "money": 500, "gear": 80, "zone": "Elwynn Forest",
        "sess": 1, "kills": 5, "deaths": 0, "played": 600}
W = charlog.worth_writing
assert W(None, dict(base, t=0)), "the first reading of a character"
assert not W(dict(base, t=0), dict(base, t=30, xp=200)), "nothing new within 10 minutes"
assert W(dict(base, t=0), dict(base, t=30, level=11)), "a level-up at once"
assert W(dict(base, t=0), dict(base, t=700, xp=200)), "after 10 minutes, something changed"
assert not W(dict(base, t=0), dict(base, t=7000)), "an hour standing still: no line"
assert W(dict(base, t=0, gear=None), dict(base, t=30)), "the gear arrived after the last line: a line now"

path = os.path.join(tempfile.mkdtemp(), "character_log.jsonl")
log = charlog.CharLog(lambda: None, path)
t = 1000.0
wrote = 0
# session 1: 20 kills, 1 death, 40 minutes, level 10 -> 11; session 2: 12 kills, 30 minutes; and another character
for i, (sess, kills, deaths, played, level, xp) in enumerate([
        (1, 5, 0, 600, 10, 100), (1, 12, 1, 1500, 10, 700), (1, 20, 1, 2400, 11, 50),
        (2, 4, 0, 500, 11, 300), (2, 12, 0, 1800, 11, 900)]):
    wrote += log.note(dict(base, sess=sess, kills=kills, deaths=deaths, played=played, level=level, xp=xp), now=t + i * 900)
assert wrote == 5, wrote
assert not log.note(dict(base, sess=2, kills=12, deaths=0, played=1800, level=11, xp=900), now=t + 4 * 900 + 60)
assert log.note({"name": "Other", "level": 3, "xp": 1, "xpmax": 100, "money": 0}, now=t)       # no session numbers: still a line
assert not log.note({"name": "", "level": 3}) and not log.note(None)

again = charlog.CharLog(lambda: None, path)                    # a restart reads the same file
snap = again.snapshot()
me = next(c for c in snap["chars"] if c["name"] == "Testa")
assert (me["kills"], me["deaths"], me["played"]) == (32, 1, 4200), (me["kills"], me["deaths"], me["played"])
assert [l["level"] for l in me["levels"]] == [10, 11] and me["levels"][1]["played"] == 2400, me["levels"]
assert me["now"]["level"] == 11 and len(me["series"]) == 5 and me["series"][-1][4] == 32
assert abs(me["series"][1][1] - 10.7) < 1e-6, me["series"][1]
other = next(c for c in snap["chars"] if c["name"] == "Other")
assert other["kills"] == 0 and other["now"]["level"] == 3
with open(path, "a", encoding="utf-8") as f:
    f.write("{not json\n")                                      # a line cut short must not lose the rest
assert len(charlog.load(path)["Testa"]) == 5
many = [dict(base, t=i * 700, xp=i % 1000, sess=1, kills=i, played=i * 700) for i in range(2000)]
assert len(charlog.summary(many)["series"]) <= 302 and charlog.summary(many)["series"][-1][4] == 1999
print("ALL PASS")
