"""Live dungeon view: pick a past run as your guide, see what's next (pull, boss, pulls and mobs
until the boss), the fight in progress, pace against the guide at every boss, and the group."""
import math

from instance import members, run_end
import time
from collections import defaultdict


def _far(p):
    return bool(p) and (abs(p[0]) > 3000 or abs(p[1]) > 3000)       # world yards, not an instance's own


def _bearing(me, pos):
    dx_n, dy_w = pos[0] - me[0], pos[1] - me[1]
    ang = (math.degrees(math.atan2(-dy_w, dx_n)) + 360) % 360
    return round(math.hypot(dx_n, dy_w)), round(ang), ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((ang + 22.5) // 45) % 8]


def _run_header(key, r, n_bosses):
    killed = sum(1 for b in r["bosses"].values() if b.get("killed"))
    wipes = sum(max(0, b.get("attempts", 0) - (1 if b.get("killed") else 0)) for b in r["bosses"].values())
    kills = r["kills"] if isinstance(r["kills"], dict) else {}
    return {"key": key, "start": r["start"], "dur": round(run_end(r, n_bosses) - r["start"]), "bosses": killed,
            "of": n_bosses, "full": bool(n_bosses) and killed >= n_bosses, "deaths": len(r["deaths"]),
            "wipes": wipes, "xp": r.get("xp", 0), "pulls": len(r.get("fights", [])),
            "party": r.get("party", []), "members": members(r), "kills": sum(kills.values()),
            "snaps": [s for s in r.get("snaps", []) if s]}


def live_view(tracker, guide_key=None, mob_moves=None, mob_heals=None, me_pos=None, roles=None, recaps=None):
    cur = tracker.cur
    finished = False
    if not cur:
        # just left? show the run you finished for 30 minutes
        le = getattr(tracker, "_last_exit", None)
        if le and le[2] and time.time() - le[1] < 1800:
            cur, finished = le[2], True
        else:
            return {"inside": False}
    name = cur["name"]
    rec = tracker.record(name)
    look = tracker.lookup.for_map(cur["map_id"]) or tracker.lookup.for_name(name)
    order = [b["name"] for b in (look or {}).get("bosses", [])]
    past = {k: r for k, r in rec["runs"].items() if int(r["start"]) != int(cur["start"]) and r.get("fights")}
    headers = sorted((_run_header(k, r, len(order)) for k, r in past.items()), key=lambda h: -h["start"])
    full = [h for h in headers if h["full"]] or headers
    best = min(full, key=lambda h: (h["dur"], h["deaths"]), default=None)
    safest = min(headers, key=lambda h: (h["deaths"], h["dur"]), default=None)
    for h in headers:
        h["best"] = best is not None and h["key"] == best["key"]
        h["safest"] = safest is not None and h["key"] == safest["key"] and len(headers) > 1
    gkey = guide_key if guide_key in past else (best["key"] if best else None)
    guide = past.get(gkey)

    kp = getattr(tracker, "kill_pos", None)                  # your spot at the end of the last fight
    me = me_pos or ([kp[1], kp[2]] if kp and kp[0] >= cur["start"] - 60 else None) or         (cur["trail"][-1][1:] if cur["trail"] else None)
    done_bosses = [b for b in order if cur["bosses"].get(b, {}).get("killed")]
    next_boss_name = next((b for b in order if b not in done_bosses), None)

    # guide pulls in order; one is "done" if we fought within 25 yd of it (or killed that boss)
    gf = [dict(f) for f in sorted(guide["fights"], key=lambda f: f["t"])] if guide else []
    ours = [f for f in cur.get("fights", []) if f.get("pos")]
    for i, f in enumerate(gf):
        f["n"] = i + 1
        if f.get("boss"):
            f["done"] = f["boss"] in done_bosses
        else:
            f["done"] = bool(f.get("pos")) and any(
                math.hypot(o["pos"][0] - f["pos"][0], o["pos"][1] - f["pos"][1]) < 25 for o in ours)
    last_done = max((i for i, f in enumerate(gf) if f["done"]), default=-1)
    upcoming = [f for f in gf[last_done + 1:] if not f["done"]]
    to_boss = []
    for f in upcoming:
        if f.get("boss"):
            break
        to_boss.append(f)

    taken_sorted = sorted(x["taken"] for x in gf if not x.get("boss")) or [0]
    median_taken = taken_sorted[len(taken_sorted) // 2] or 1

    def describe(f):
        if not f:
            return None
        d = dict(f)
        # danger: hit much harder than a typical pull last time, or someone died
        d["danger"] = "high" if f.get("deaths") or f["taken"] >= 1.6 * median_taken else (
            "low" if f["taken"] <= 0.6 * median_taken else "normal")
        if me and f.get("pos") and _far(me) == _far(f["pos"]):     # (not across the portal: "11,236 yd")
            d["yards"], d["bearing"], d["compass"] = _bearing(me, f["pos"])
        d["what"] = {m: [x for x in (mob_moves(m) if mob_moves else []) if x["kind"] != "melee"][:3] for m in f["mobs"]}
        d["heals"] = [m for m in f["mobs"] if mob_heals and mob_heals(m)]
        # kill / interrupt first: healers, and mobs with a big spell (>= 60 avg)
        d["priority"] = sorted({m for m in f["mobs"] if m in d["heals"] or any(
            x["kind"] == "damage" and (x.get("avg") or 0) >= 60 for x in (mob_moves(m) if mob_moves else []))})
        return d

    boss = None
    if next_boss_name:
        gb = (guide or {}).get("bosses", {}).get(next_boss_name, {})
        bfight = next((f for f in gf if f.get("boss") == next_boss_name), None)
        boss = {"name": next_boss_name, "moves": (mob_moves(next_boss_name) if mob_moves else [])[:6],
                "heals": mob_heals(next_boss_name) if mob_heals else None,
                "last_kill": gb.get("dur"), "last_attempts": gb.get("attempts"),
                "last_deaths": (bfight or {}).get("deaths", []),
                "adds": {m: c for m, c in (bfight or {}).get("mobs", {}).items() if m != next_boss_name},
                "pos": gb.get("pos") or (bfight or {}).get("pos")}
        if me and boss["pos"] and _far(me) == _far(boss["pos"]):
            boss["yards"], boss["bearing"], boss["compass"] = _bearing(me, boss["pos"])

    # pace: time into the run at each boss kill, this run vs the guide
    splits = []
    for b in order:
        mine = cur["bosses"].get(b, {})
        gb = (guide or {}).get("bosses", {}).get(b, {})
        t_m = (mine["start"] + (mine.get("dur") or 0) - cur["start"]) if mine.get("killed") and mine.get("start") else None
        t_g = (gb["start"] + (gb.get("dur") or 0) - guide["start"]) if guide and gb.get("killed") and gb.get("start") else None
        splits.append({"boss": b, "mine": round(t_m) if t_m else None, "guide": round(t_g) if t_g else None,
                       "delta": round(t_m - t_g) if t_m and t_g else None})

    f = getattr(tracker, "_fight", None)
    fight = None
    if f and time.time() - f["end"] < 15:
        names = defaultdict(int)
        for n in f["mobs"].values():
            names[n] += 1
        fight = {"dur": round(f["end"] - f["start"]), "mobs": dict(names), "size": len(f["mobs"]),
                 "taken": f["taken"], "dealt": f["dealt"], "healed": f["healed"], "deaths": f["deaths"],
                 "boss": f.get("boss")}
    last_end = max((x["t"] + x["dur"] for x in cur.get("fights", [])), default=cur["start"])

    my_kills = sum(cur["kills"].values()) if isinstance(cur["kills"], dict) else 0

    # now vs the guide at the same point: pair each guide pull we've done with our fight there
    pairs = []
    for gp in gf:
        if not gp["done"] or not gp.get("pos"):
            continue
        if gp.get("boss"):
            mine = next((o for o in cur.get("fights", []) if o.get("boss") == gp["boss"]), None)
        else:
            near = [o for o in ours if math.hypot(o["pos"][0] - gp["pos"][0], o["pos"][1] - gp["pos"][1]) < 25]
            mine = min(near, key=lambda o: math.hypot(o["pos"][0] - gp["pos"][0], o["pos"][1] - gp["pos"][1]), default=None)
        if mine:
            pairs.append({"n": gp["n"], "boss": gp.get("boss"), "mobs": gp["mobs"],
                          "dur": [round(mine["dur"]), round(gp["dur"])], "taken": [mine["taken"], gp["taken"]],
                          "healed": [mine["healed"], gp["healed"]],
                          "deaths": [len(mine["deaths"]), len(gp["deaths"])]})
    # (the clock stops when the run does: these kept climbing after the last boss / after you left)
    now = cur.get("cleared_t") or (cur["end"] if finished else time.time())
    # a two-part dungeon (Forever's Deadmines): draw only the half you're in
    from instance import two_halves, map_place, portal_of
    world = map_place(cur).endswith("(open world)")
    split = two_halves([cur, guide])
    same = (lambda p: bool(p) and _far(p) == world) if split else (lambda p: bool(p))
    portal = portal_of(look) if world else None
    compare = None
    if guide:
        t_guide_here = (gf[last_done]["t"] + gf[last_done]["dur"] - guide["start"]) if last_done >= 0 else 0
        g_done = [x for x in gf if x["done"]]
        g_deaths_here = sum(1 for d in guide["deaths"] if d[0] <= guide["start"] + t_guide_here)
        tot = lambda k, i: sum(p[k][i] for p in pairs)
        compare = {
            "time": [round(now - cur["start"]), round(t_guide_here)],
            "pulls": [len(cur.get("fights", [])), len(g_done)],
            "kills": [my_kills, sum(x["kills"] for x in g_done)],
            "deaths": [len(cur["deaths"]), g_deaths_here],
            "taken": [tot("taken", 0), tot("taken", 1)],
            "healed": [tot("healed", 0), tot("healed", 1)],
            "avg_pull": [round(tot("dur", 0) / len(pairs)) if pairs else None,
                         round(tot("dur", 1) / len(pairs)) if pairs else None],
            "pairs": pairs[-6:][::-1],
        }
    # ETA: where the guide was in its run at the pull we've reached, plus what it still needed
    eta = None
    if guide and gf:
        t_g = (gf[last_done]["t"] + gf[last_done]["dur"] - guide["start"]) if last_done >= 0 else 0
        remaining = (run_end(guide, len(order)) - guide["start"]) - t_g
        eta = {"finish_in": round(remaining), "total": round(now - cur["start"] + remaining),
               "vs_guide": round((now - cur["start"]) - t_g)}
    guide_kills = sum(f["kills"] for f in gf)
    return {
        "inside": True, "finished": finished, "name": name, "lookup": look,
        "pending": bool(cur.get("pending")), "run_start": cur["start"],
        # this run's roster: level (screen) and gear = average item level (combat log), per member
        "roster": {mm["name"]: mm for mm in members(cur)},
        "cleared": bool(cur.get("cleared_t")),
        "elapsed": 0 if cur.get("pending") else round((cur.get("cleared_t") or (cur["end"] if finished else time.time())) - cur["start"]),
        "runs": headers, "guide_key": gkey,
        "guide": {"trail": [p for p in guide["trail"] if same(p[1:])],
                  "fights": [f if not f.get("pos") or same(f["pos"]) else dict(f, pos=None) for f in gf],
                  "dur": round(run_end(guide, len(order)) - guide["start"]),
                  "start": next((p[1:] for p in guide["trail"] if same(p[1:])), None),
                  "deaths": [d for d in tracker._deaths(guide, roles or {}, recaps or []) if same(d["pos"])]} if guide else None,
        "trail": [p for p in cur["trail"] if same(p[1:])], "me": me if not me or same(me) else None,
        "deaths": len(cur["deaths"]),
        "open_world": world, "portal": portal if portal and _far(portal) else None,
        "pulls_done": len(cur.get("fights", [])), "bosses": order, "bosses_done": done_bosses,
        "next_pull": describe(upcoming[0] if upcoming else None), "next_boss": boss,
        "to_boss": [describe(x) for x in to_boss], "pulls_left": len(upcoming),
        # mobs left: from the guide's remaining pulls (until the boss, and in total)
        "mobs_to_boss": sum(x["size"] for x in to_boss),
        "mobs_left": sum(x["size"] for x in upcoming),
        "kills": my_kills, "guide_kills": guide_kills,
        # % complete: share of the guide run's mobs whose pulls you've done (no guide: bosses killed)
        "pct": round(100 * sum(x["size"] for x in gf if x["done"]) / max(1, sum(x["size"] for x in gf)))
               if gf else round(100 * len(done_bosses) / max(1, len(order))),
        "loot": tracker.loot_rows(cur.get("loot", [])), "coin": cur.get("copper", 0),
        "splits": splits, "fight": fight, "eta": eta, "compare": compare, "since_pull": 0 if fight else round(max(0, min(now - last_end, now - cur["start"]))),
    }
