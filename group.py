"""Group/party tracking from the combat log: per-member damage, healing, damage taken, deaths,
roles, class guesses, positions, loose-mob (aggro) alerts and death recaps.

Fed by Companion.feed_combat. Party membership comes from the unit flags (PARTY/RAID affiliation
relative to you). Set WOW_GROUP_TEST=1 to treat every nearby friendly player as a party member
(for trying the page while solo).
"""
import math
import re
import os
from collections import defaultdict, deque

MINE, PARTY, RAID = 0x1, 0x2, 0x4
FRIENDLY = 0x10
T_PLAYER, T_NPC, T_PET, T_GUARDIAN = 0x400, 0x800, 0x1000, 0x2000
TEST = os.environ.get("WOW_GROUP_TEST") == "1"
FIGHT_GAP = 8.0
HEAL_RANGE = 40.0       # yards

CLASS_SPELLS = {
    "Warrior": ["Heroic Strike", "Rend", "Battle Shout", "Thunder Clap", "Sunder Armor", "Charge", "Taunt",
                "Revenge", "Shield Block", "Overpower", "Hamstring", "Defensive Stance", "Bloodrage"],
    "Paladin": ["Seal of Righteousness", "Holy Light", "Blessing of Might", "Devotion Aura", "Judgement",
                "Seal of the Crusader", "Retribution Aura", "Flash of Light", "Hammer of Justice"],
    "Hunter": ["Serpent Sting", "Arcane Shot", "Raptor Strike", "Aspect of the Monkey", "Auto Shot",
               "Hunter's Mark", "Concussive Shot", "Aspect of the Hawk", "Track Beasts"],
    "Rogue": ["Sinister Strike", "Eviscerate", "Backstab", "Gouge", "Slice and Dice", "Stealth", "Kick"],
    "Priest": ["Smite", "Power Word: Fortitude", "Lesser Heal", "Renew", "Power Word: Shield",
               "Shadow Word: Pain", "Mind Blast", "Inner Fire", "Heal", "Flash Heal"],
    "Mage": ["Fireball", "Frostbolt", "Arcane Intellect", "Frost Armor", "Arcane Missiles", "Fire Blast",
             "Frost Nova", "Conjure Water", "Conjure Food", "Polymorph"],
    "Warlock": ["Shadow Bolt", "Corruption", "Immolate", "Demon Skin", "Summon Imp", "Curse of Agony",
                "Life Tap", "Fear", "Drain Life", "Summon Voidwalker"],
    "Druid": ["Wrath", "Moonfire", "Healing Touch", "Rejuvenation", "Mark of the Wild", "Thorns",
              "Entangling Roots", "Bear Form", "Cat Form", "Regrowth", "Maul"],
    "Shaman": ["Lightning Bolt", "Earth Shock", "Healing Wave", "Rockbiter Weapon", "Lightning Shield",
               "Stoneskin Totem", "Searing Totem", "Flame Shock", "Strength of Earth Totem"],
}
SPELL_CLASS = {sp: cls for cls, sps in CLASS_SPELLS.items() for sp in sps}
HEAL_SPELL_HINT = ("heal", "renew", "rejuv", "regrowth", "light", "mend", "wave", "shield")


def _on_map(p):
    return p if p and 0 <= p[0] <= 100 and 0 <= p[1] <= 100 else None


def short(n):
    return n.split("-")[0] if n else n


def new_member():
    return {"name": None, "guid": None, "cls": None, "me": False,
            "dmg": 0, "heal": 0, "over": 0, "taken": 0, "deaths": 0, "melee_taken": 0,
            "hp": None, "maxhp": None, "power": None, "maxpower": None, "ptype": 0,
            "x": None, "y": None, "seen": 0, "taken_by": defaultdict(int),
            "my_heal": 0, "my_over": 0, "dispels": 0, "interrupts": 0, "auras": {},
            "rips": 0, "loose_mobs": 0,             # pulled threat off the tank / (tank) mobs never picked up
            "faults": 0,                            # aggro problems judged to be this player's fault
            "melee_t": deque(maxlen=400),           # when they took melee hits (tank = most, recently)
            "recent": deque(maxlen=60),        # (t, amount) damage taken, for incoming-damage triage
            "last_hits": deque(maxlen=8)}      # (t, source, spell, amount, hp_after) for death recaps


class GroupTracker:
    def __init__(self):
        self.members = {}                       # guid -> member
        self.pet_owner = {}                     # pet guid -> owner guid
        self.fight = None
        self.fights = deque(maxlen=30)
        self.deaths = deque(maxlen=60)
        self.loose = deque(maxlen=20)           # mobs hitting a non-tank
        self.mob_on = {}                        # mob guid -> {"tanked": bool, "on": guid, "t": t}
        self.aggro_log = deque(maxlen=30)       # threat rips and mobs the tank never picked up
        self.mob_dmg = {}                       # mob guid -> {member guid: [damage, first hit t]}
        self.me = None

    # -------------------------------------------------- membership
    def _is_member(self, guid, flags):
        if not guid or not guid.startswith("Player-"):
            return False
        if guid == self.me:
            return True
        if flags & T_PLAYER and flags & (PARTY | RAID):
            return True
        return TEST and bool(flags & T_PLAYER) and bool(flags & FRIENDLY)

    def _member(self, guid, name):
        m = self.members.get(guid)
        if m is None:
            m = self.members[guid] = new_member()
            m["guid"], m["name"], m["me"] = guid, short(name), guid == self.me
        return m

    def _owner(self, guid, flags):
        """Who gets credit: the player, or the owner of a pet/guardian."""
        if flags & (T_PET | T_GUARDIAN):
            return self.pet_owner.get(guid)
        return guid

    # -------------------------------------------------- fights
    def _touch(self, t):
        f = self.fight
        if f is None or t - f["last"] > FIGHT_GAP:
            self._close()
            f = self.fight = {"start": t, "last": t, "dmg": defaultdict(int), "heal": defaultdict(int),
                              "taken": defaultdict(int), "deaths": [], "kills": 0, "mobs": set()}
        f["last"] = t
        return f

    def _close(self):
        if self.fight:
            f = self.fight
            f["dur"] = max(1.0, f["last"] - f["start"])
            self.fights.append(f)
            self.fight = None

    # -------------------------------------------------- events
    def on_event(self, t, ev, c, sg, sn, sf, dg, dn, df, spell, a, s, adv):
        if self.me is None:
            return
        # positions / hp from the advanced block, plus pet ownership
        if adv:
            ig, owner = c[a], c[a + 1]
            if owner and owner != "0000000000000000" and ig != owner:
                self.pet_owner[ig] = owner
            m = self.members.get(ig)
            if m is not None:
                try:
                    m["hp"], m["maxhp"] = int(float(c[a + 2])), int(float(c[a + 3]))
                    m["ptype"] = int(float(c[a + 10].split("|")[0]))
                    m["power"] = int(float(c[a + 11].split("|")[0]))
                    m["maxpower"] = int(float(c[a + 12].split("|")[0]))
                    x, y = float(c[a + 14]), float(c[a + 15])
                    if (x, y) != (0.0, 0.0):
                        m["x"], m["y"] = x, y
                    m["seen"] = t
                except (ValueError, IndexError):
                    pass

        if sf & (T_PET | T_GUARDIAN):
            # a pet only counts if it's in your group (or yours) and its owner is a known member
            owner = self.pet_owner.get(sg)
            src_m = self.members.get(owner) if owner and (sf & (MINE | PARTY | RAID) or TEST) else None
        else:
            src_m = self.members.get(sg)
            if src_m is None and self._is_member(sg, sf):
                src_m = self._member(sg, sn)
        dst_m = None
        if self._is_member(dg, df):
            dst_m = self._member(dg, dn)
        for mm in (src_m, dst_m):
            if mm is not None:
                mm["seen"] = max(mm["seen"], t)        # any log line with them counts as "still here"

        if src_m and spell in SPELL_CLASS and not src_m["cls"]:
            src_m["cls"] = SPELL_CLASS[spell]

        if ev == "UNIT_DIED":
            if dst_m:
                dst_m["deaths"] += 1
                rec = {"t": t, "name": dst_m["name"], "hits": list(dst_m["last_hits"])[-6:]}
                self.deaths.append(rec)
                if self.fight:
                    self.fight["deaths"].append(dst_m["name"])
            elif df & T_NPC and self.fight and dg in self.fight["mobs"]:
                self.fight["kills"] += 1
            return

        is_dmg = ev.endswith("_DAMAGE") and not ev.endswith("_LANDED")
        is_heal = ev in ("SPELL_HEAL", "SPELL_PERIODIC_HEAL")
        if is_dmg and len(c) > s + 6:
            amt = int(float(c[s])) if c[s] not in ("nil", "") else 0
            absorbed = int(float(c[s + 6])) if c[s + 6] not in ("nil", "") else 0
            if src_m and df & T_NPC:
                md = self.mob_dmg.setdefault(dg, {})
                rec = md.setdefault(src_m["guid"], [0, t])          # [damage on this mob, first hit time]
                rec[0] += amt
                f = self._touch(t)
                f["mobs"].add(dg)
                src_m["dmg"] += amt
                f["dmg"][src_m["name"]] += amt
            if dst_m and sf & T_NPC:
                f = self._touch(t)
                f["mobs"].add(sg)
                dst_m["taken"] += amt
                dst_m["taken_by"][short(sn) or spell] += amt
                dst_m["recent"].append((t, amt + absorbed))
                hp_after = None
                if adv and c[a] == dg:
                    try:
                        hp_after = int(float(c[a + 2]))
                    except ValueError:
                        pass
                dst_m["last_hits"].append((t, short(sn), spell, amt + absorbed, hp_after))
                f["taken"][dst_m["name"]] += amt
                if spell == "Melee":
                    dst_m["melee_taken"] += 1
                    dst_m["melee_t"].append(t)
                    self.now = t
                    self._aggro_check(t, sg, short(sn), dst_m)
                    # aggro check: a mob meleeing someone who isn't the tank
                    tank = self.tank()
                    if tank and tank != dst_m["guid"] and len(self.members) > 1:
                        if not self.loose or self.loose[-1]["mob"] != short(sn) or t - self.loose[-1]["t"] > 5:
                            self.loose.append({"t": t, "mob": short(sn), "on": dst_m["name"],
                                               "role": self.role(dst_m)})
            return
        if is_heal and len(c) > s + 2:
            amt, over = int(float(c[s])), int(float(c[s + 2]))
            if src_m:
                src_m["heal"] += amt - over
                src_m["over"] += over
                if self.fight and t - self.fight["last"] <= FIGHT_GAP:
                    self.fight["heal"][src_m["name"]] += amt - over
                if src_m["guid"] == self.me and dst_m:
                    dst_m["my_heal"] += amt - over
                    dst_m["my_over"] += over
            return
        if ev == "SPELL_ABSORBED" and len(c) >= 19:
            absorber = c[-10]
            m = self.members.get(absorber)
            amt = int(float(c[-3])) if c[-3] not in ("nil", "") else 0
            if m:
                m["heal"] += amt
                if self.fight:
                    self.fight["heal"][m["name"]] += amt
                if absorber == self.me and dst_m:
                    dst_m["my_heal"] += amt
            return
        if dst_m and ev in ("SPELL_AURA_APPLIED", "SPELL_AURA_REFRESH"):
            dst_m["auras"][spell] = t
        elif dst_m and ev == "SPELL_AURA_REMOVED":
            dst_m["auras"].pop(spell, None)
        if src_m and ev == "SPELL_DISPEL":
            src_m["dispels"] += 1
        if src_m and ev == "SPELL_INTERRUPT":
            src_m["interrupts"] += 1

    def _aggro_check(self, t, mob_g, mob_name, victim):
        """Who a mob is meleeing tells who has its aggro. On the tank, then on someone else = that
        person ripped threat (over-DPS, or healing threat). Never on the tank and straight onto
        someone else = the tank didn't pick it up (sloppy tank)."""
        tank_g = self.tank()
        st = self.mob_on.get(mob_g)
        if st is None or t - st["t"] > 60:
            st = self.mob_on[mob_g] = {"tanked": False, "on": None, "t": t, "flagged": False}
        prev_on, st["t"] = st["on"], t
        st["on"] = victim["guid"]
        if not tank_g or len(self.members) < 3:
            return
        if victim["guid"] == tank_g:
            st["tanked"] = True
            return
        if prev_on == victim["guid"]:
            return                                       # still on the same person: already counted
        tank = self.members.get(tank_g)
        md = self.mob_dmg.get(mob_g, {})
        tank_dmg, tank_first = md.get(tank_g, [0, None])
        vic_dmg, vic_first = md.get(victim["guid"], [0, None])
        role = self.role(victim)
        if st["tanked"]:
            victim["rips"] += 1
            # whose fault? damage on this mob is the threat stand-in (tanks get extra threat, hence 1.3x)
            if role != "healer" and tank_dmg and vic_dmg > 1.3 * tank_dmg:
                fault, why = victim, f"out-damaged the tank on it ({vic_dmg:,} vs {tank_dmg:,})"
            elif role != "healer" and tank_first and vic_first and vic_first - tank_first < 3:
                fault, why = victim, f"opened {max(0, vic_first - tank_first):.1f}s after the tank: no threat built yet"
            elif not tank_dmg:
                fault, why = tank, "the tank never hit it"
            else:
                fault, why = tank, (f"tank threat too low ({tank_dmg:,} dmg on it)" if role != "healer"
                                    else f"healing threat beat the tank ({tank_dmg:,} dmg on it)")
            kind = "rip"
        else:
            if st["flagged"]:
                return
            st["flagged"] = True
            if tank:
                tank["loose_mobs"] += 1
            if vic_first and (not tank_first or vic_first < tank_first):
                fault, why = victim, f"{victim['name']} hit it first and pulled it"
            else:
                fault, why = tank, "the tank never picked it up"
            kind = "loose"
        if fault:
            fault["faults"] += 1
        self.aggro_log.append({"t": t, "kind": kind, "who": victim["name"], "role": role, "mob": mob_name,
                               "tank": tank["name"] if tank else None,
                               "fault": fault["name"] if fault else None, "why": why})

    # -------------------------------------------------- roles & views
    def tank(self):
        """Whoever in the current party took the most melee in the last 10 minutes. Only classes that
        clearly can't tank are ruled out (Forever lets Shamans tank). Cached per second."""
        now = getattr(self, "now", 0)
        if getattr(self, "_tank_t", None) == int(now):
            return self._tank
        recent = lambda m: sum(1 for x in m["melee_t"] if now - x <= 600)
        cands = [(recent(m), m) for m in self.members.values()
                 if m["cls"] not in ("Priest", "Mage", "Warlock", "Hunter") and not m["me"] and now - m["seen"] < 300]
        cands.sort(key=lambda c: -c[0])
        tank = None
        if cands and cands[0][0] >= 10 and (len(cands) == 1 or cands[0][0] >= 1.5 * cands[1][0]):
            tank = cands[0][1]["guid"]
        self._tank_t, self._tank = int(now), tank
        return tank

    def role(self, m):
        if m["guid"] == self.tank():
            return "tank"
        if m["heal"] > m["dmg"] * 0.8 and m["heal"] > 200:
            return "healer"
        if m["me"] and m["cls"] in ("Priest", "Druid", "Shaman", "Paladin") and m["heal"] > 200:
            return "healer"                          # you: the group's healer
        return "dps"

    def snapshot(self, now_t, me_xy, to_map_pct=None, cur_map=None, screen_party=None):
        f = self.fight or (self.fights[-1] if self.fights else None)
        fdur = max(1.0, f["last"] - f["start"]) if f else 1.0
        members = []
        for m in self.members.values():
            if not m["me"] and now_t - m["seen"] > 600 and not TEST:
                continue                         # left the group / gone
            if TEST and not m["me"] and now_t - m["seen"] > 120:
                continue
            dist = None
            if me_xy and me_xy[0] is not None and m["x"] is not None and not m["me"]:
                dist = round(math.hypot(m["x"] - me_xy[0], m["y"] - me_xy[1]), 1)
            inc10 = sum(a for tt, a in m["recent"] if now_t - tt <= 10)
            raw = m["heal"] + m["over"]
            members.append({
                "name": m["name"], "me": m["me"], "cls": m["cls"], "role": self.role(m),
                "hp": m["hp"], "maxhp": m["maxhp"], "power": m["power"], "maxpower": m["maxpower"], "ptype": m["ptype"],
                "dist": dist, "in_range": dist is None or dist <= HEAL_RANGE,
                "map": _on_map(to_map_pct(cur_map, m["x"], m["y"])) if to_map_pct and m["x"] is not None else None,
                "dmg": m["dmg"], "heal": m["heal"], "overheal": round(m["over"] / raw * 100) if raw else 0,
                "taken": m["taken"], "deaths": m["deaths"], "inc10": inc10,
                "my_heal": m["my_heal"], "my_over_pct": round(m["my_over"] / (m["my_heal"] + m["my_over"]) * 100)
                if m["my_heal"] + m["my_over"] else 0,
                "f_dmg": f["dmg"].get(m["name"], 0) if f else 0, "f_heal": f["heal"].get(m["name"], 0) if f else 0,
                "f_taken": f["taken"].get(m["name"], 0) if f else 0,
                "dps": round(f["dmg"].get(m["name"], 0) / fdur, 1) if f else 0,
                "hps": round(f["heal"].get(m["name"], 0) / fdur, 1) if f else 0,
                "top_hitter": max(m["taken_by"], key=m["taken_by"].get) if m["taken_by"] else None,
                "dispels": m["dispels"], "interrupts": m["interrupts"], "seen": m["seen"],
                "rips": m["rips"], "loose_mobs": m["loose_mobs"], "faults": m["faults"],
            })
        # live HP/mana from the party frames on screen (names there are cut short: match by prefix)
        key = lambda n: re.sub(r"[^a-z]", "", (n or "").lower())       # "ßrynstone" -> "rynstone"
        for f in screen_party or []:
            nm = key(f.get("name"))
            row = next((r for r in members if not r["me"] and nm and key(r["name"]).startswith(nm[:6])), None)
            if row is None and nm:
                row = {"name": f["name"], "me": False, "cls": None, "role": "dps", "hp": None, "maxhp": None,
                       "power": None, "maxpower": None, "ptype": 0, "dist": None, "in_range": True, "map": None,
                       "dmg": 0, "heal": 0, "overheal": 0, "taken": 0, "deaths": 0, "inc10": 0, "my_heal": 0,
                       "my_over_pct": 0, "f_dmg": 0, "f_heal": 0, "f_taken": 0, "dps": 0, "hps": 0,
                       "top_hitter": None, "dispels": 0, "interrupts": 0, "seen": now_t}
                members.append(row)
            if row is None:
                continue
            row["live"] = True
            row["hp_frac"] = f["hp"]
            row["mana_frac"] = f["mana"]
            if row.get("maxhp"):
                row["hp"] = round(f["hp"] * row["maxhp"])
            if row.get("maxpower") and f["mana"] is not None:
                row["power"] = round(f["mana"] * row["maxpower"])
        # when the party frames are readable, they decide who's in the group
        if screen_party and all(f.get("name") for f in screen_party):
            from difflib import SequenceMatcher
            keep = [key(f["name"])[:6] for f in screen_party]
            close = lambda r: any(SequenceMatcher(None, key(r["name"])[:6], k).ratio() >= 0.7 for k in keep)
            # never drop someone the log saw in the last minute (an OCR misread mustn't hide the tank)
            members = [r for r in members if r["me"] or close(r) or now_t - r["seen"] < 60]
        order = {"tank": 0, "healer": 1, "dps": 2}
        members.sort(key=lambda r: (order[r["role"]], not r["me"], r["name"] or ""))
        fights = []
        for g in list(self.fights)[-10:][::-1]:
            fights.append({"start": g["start"], "dur": round(g["dur"], 1), "dmg": sum(g["dmg"].values()),
                           "heal": sum(g["heal"].values()), "taken": sum(g["taken"].values()),
                           "deaths": g["deaths"], "kills": g["kills"],
                           "dps": round(sum(g["dmg"].values()) / g["dur"], 1)})
        cur = None
        import time as _time
        # (by the LOG's clock: the log arrives 30 s - 5 min late, so by the real clock every fight looked over
        #  already, and one long fight was cut into several)
        if self.fight and now_t - self.fight["last"] > 20:
            self._close()
        if self.fight:
            cur = {"start": self.fight["start"], "dur": round(fdur, 1), "dmg": sum(self.fight["dmg"].values()),
                   "heal": sum(self.fight["heal"].values()), "taken": sum(self.fight["taken"].values()),
                   "dps": round(sum(self.fight["dmg"].values()) / fdur, 1),
                   "hps": round(sum(self.fight["heal"].values()) / fdur, 1)}
        return {"members": members, "in_group": len(members) > 1, "test": TEST, "fight": cur,
                "fights": fights, "deaths": list(self.deaths)[-8:][::-1],
                "loose": [x for x in list(self.loose)[-8:][::-1] if now_t - x["t"] < 600],
                "aggro_log": list(self.aggro_log)[-10:][::-1]}
