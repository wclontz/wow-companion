"""questsdone.py: the chat log's "<quest> completed." lines, who they belong to, and the add-on's saved list."""
import json
import os
import tempfile
from datetime import datetime

import questsdone

now = datetime(2026, 11, 26, 12, 0, 0)
t, q = questsdone.parse("11/4 23:10:44.772  A Threat Within completed.", now)
assert q == "A Threat Within" and datetime.fromtimestamp(t) == datetime(2026, 11, 4, 23, 10, 44, 772000), (t, q)
assert questsdone.parse('11/8 10:00:00.000  WANTED: "Hogger" completed.', now)[1] == 'WANTED: "Hogger"'   # (": " in a name)
assert questsdone.parse("11/8 10:00:00.000  Camping 101: Cooking completed.", now)[1] == "Camping 101: Cooking"
for chat in ("11/8 10:00:00.000  Bob says: my quest completed.", "11/8 10:00:00.000  [2. Trade] Bob: run completed.",
             "11/8 10:00:00.000  To Bob: completed.", "11/8 10:00:00.000  |cff00ff00|Hquest:1|h[X]|h|r completed.",
             "11/8 10:00:00.000  Experience gained: 40.", "nonsense", ""):
    assert questsdone.parse(chat, now) is None, chat
assert datetime.fromtimestamp(questsdone.parse("12/30 10:00:00.000  Old One completed.", datetime(2027, 1, 2))[0]).year == 2026

d = tempfile.mkdtemp()
cl = os.path.join(d, "character_log.jsonl")
with open(cl, "w", encoding="utf-8") as f:
    f.write(json.dumps({"t": 1000.0, "name": "Aaa"}) + "\n" + json.dumps({"t": 9000.0, "name": "Bbb"}) + "\nbroken\n")
who = questsdone.Who(cl)
assert who.at(1200) == "Aaa" and who.at(8000) == "Bbb" and who.at(5000) is None and who.at(1000 - questsdone.NEAR - 1) is None

path = os.path.join(d, "quests_done.jsonl")
done = questsdone.Done(path, who)
assert done.add(1100.0, "One") and not done.add(1100.2, "One")              # the log is read again at every start
assert done.add(5000.0, "Two") and done.add(5100.0, "Two") and done.add(8500.0, "Three")
snap = done.snapshot()
names = {c["name"]: c for c in snap["chars"]}
assert snap["total"] == 4 and names["Aaa"]["n"] == 1 and names["Bbb"]["n"] == 1 and names[None]["n"] == 2, snap
assert names[None]["names"] == 1 and snap["chars"][-1]["name"] is None       # (the unknown one comes last)
done.name_period(4000, 6000, "Aaa")                                          # the time before the character log
done = questsdone.Done(path, who)                                            # ...and it is all there after a restart
names = {c["name"]: c for c in done.snapshot()["chars"]}
assert None not in names and names["Aaa"]["n"] == 3 and names["Aaa"]["names"] == 2, names
assert [x["quest"] for x in names["Aaa"]["days"][0]["quests"]][0] == "Two"   # newest first

log = os.path.join(d, "WoWChatLog.txt")
with open(log, "w", encoding="utf-8") as f:
    f.write("11/4 23:10:44.772  A Threat Within completed.\n11/4 23:10:44.772  Experience gained: 40.\n"
            "11/4 23:12:38.342  Eagan Peltskinner completed.\n")
assert done.backfill([log, os.path.join(d, "missing.txt")]) == 2 and done.backfill([log]) == 0

sv = os.path.join(d, "CompanionBeacon.lua")
with open(sv, "w", encoding="utf-8") as f:
    f.write('\nCompanionBeaconDB = {\n["hide_carry"] = false,\n["quests_done"] = {\n["Aaa"] = {\n["783"] = "A Threat Within",\n'
            '["7"] = true,\n["54"] = "Say \\"hi\\"",\n["99"] = "One",\n},\n["Ccc"] = {\n["5"] = "Solo",\n},\n},\n["other"] = {\n["1"] = "no",\n},\n}\n')
g = questsdone.game_list([sv, os.path.join(d, "none.lua")])
assert g["Aaa"]["n"] == 4 and g["Aaa"]["titles"] == ["A Threat Within", 'Say "hi"', "One"] and g["Ccc"]["n"] == 1 and "other" not in g, g
names = {c["name"]: c for c in done.snapshot(g)["chars"]}
assert names["Aaa"]["game"]["only"] == ["A Threat Within", 'Say "hi"'] and names["Aaa"]["game"]["unnamed"] == 1, names["Aaa"]["game"]
assert names["Ccc"]["n"] == 0 and names["Ccc"]["game"]["only"] == ["Solo"]
assert questsdone.game_list([]) == {} and questsdone.saved_files(None) == []
print("ALL PASS")
