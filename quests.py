"""Reads the on-screen quest tracker with Windows OCR and parses it into quests.

Also handles "look this quest up" requests: tries the bundled `claude` CLI (works once it
has been logged in), otherwise queues the request in asks.jsonl for a Claude chat session
watching that file to answer via POST /api/answer.
"""
import glob
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from difflib import SequenceMatcher

import numpy as np
from PIL import Image

import fastgrab

HERE = os.path.dirname(os.path.abspath(__file__))
OCR_PS1 = os.path.join(HERE, "ocr.ps1")
ASKS_FILE = os.path.join(HERE, "asks.jsonl")
QUEST_REGION = (1640, 285, 1920, 1000)       # quest tracker on a 1920x1080 window
TITLE_RE = re.compile(r"(?:[\[|]|^1(?=\d{2,3}\s)|^I\s(?=[0-9IO]))\s*([0-9IOolS](?:\s?[0-9IOolS])?)\+?\s*(?:[\]\)|J1]\s*|\s+(?=[A-Z]))(.+)$")
SKIP = {"all objectives", "quests", "campaign", "scenario"}
NO_WINDOW = 0x08000000


def _fix_level(s):
    return int(s.replace(" ", "").translate(str.maketrans({"I": "1", "l": "1", "O": "0", "o": "0", "S": "5"})))


def _clean(s):
    s = re.sub(r"\bO/ ?I\b", "0/1", s)
    s = re.sub(r"(\d)/ I\b", r"\1/1", s)
    return s.strip(" -•·")


def _strip_icon(t):
    """Drop the quest-icon junk OCR reads in front of a title: '@ ', '(2) ', 'Q) '."""
    m = re.match(r"^(\S{1,3})\s+(.+)$", t)
    if m and not re.fullmatch(r"[A-Za-z]{2,3}", m.group(1)):
        return _clean(m.group(2))
    return _clean(t)


def tag_colours(img, lines):
    """Mark each OCR line as a title (gold/red/green quest-coloured text) or not (white detail text)."""
    a = np.asarray(img.convert("RGB")).astype(int)
    for ln in lines:
        x, y, h = ln["x"] // 2, ln["y"] // 2, max(4, ln["h"] // 2)
        seg = a[y:y + h, x:x + 160].reshape(-1, 3)
        txt = seg[seg.max(axis=1) > 150]
        if len(txt) < 5:
            continue
        r, g, b = txt.mean(axis=0)
        ln["is_title"] = not (b > 150 and min(r, g, b) > 130)
    return lines


def _wrapped_title(q, ln, text, gold_ok=False):
    """A line right under a title, with no objective count or leading dash, starting at (or left of)
    the title text instead of the deeper detail indent, continues that title."""
    if re.match(r"^[-•·]|\d+\s*/\s*\d+", text) or len(text) > 40:
        return False
    if ln.get("is_title") is False and gold_ok:
        return False
    if ln.get("is_title") is False and not gold_ok and q.get("_gold_only"):
        return False
    close = ln["y"] - q["_y"] <= q["_h"] * 1.9
    return close and ln.get("x", 0) <= q["_x"] + 12


def parse(lines, stop_at_gap=True):
    """lines: [{"text","y","h"}] top to bottom -> [{"level","title","details":[...]}]."""
    quests = []
    last_y = None
    typical = np.median([ln["h"] for ln in lines if ln["h"] > 10]) if lines else 20
    for ln in lines:
        text = ln["text"].strip()
        if not text or text.lower() in SKIP:
            continue
        if stop_at_gap and last_y is not None and ln["y"] - last_y > typical * 4:
            break                                   # big gap: end of tracker (tooltips etc. below)
        last_y = ln["y"]
        m = TITLE_RE.search(text)
        if m and m.start() > 6:
            m = None
        if text.startswith("<") or re.match(r"^(level \w|right click)", text, re.I):
            continue                                 # a tooltip drawn over the tracker
        if not m and quests and not quests[-1]["details"] and _wrapped_title(quests[-1], ln, text, gold_ok=True):
            quests[-1]["title"] += " " + _strip_icon(text)    # gold 2nd line of a wrapped title
            quests[-1]["_y"] = ln["y"]
            continue
        if not m and ln.get("is_title") and len(text) >= 4:
            quests.append({"level": None, "title": _strip_icon(text), "details": [], "_x": ln.get("x", 0),
                           "_y": ln["y"], "_h": ln["h"]})
            continue
        if m and ln.get("is_title") is False:
            m = None                                 # white text can't be a title
        if m:
            try:
                lvl = _fix_level(m.group(1))
                if lvl > 60:                         # "[61 Book" is "[6] Book" with the ] read as 1
                    lvl = int(str(lvl)[0])
            except ValueError:
                lvl = None
            quests.append({"level": lvl, "title": _clean(m.group(2)), "details": [], "_x": ln.get("x", 0) + m.start(2) * 0,
                           "_y": ln["y"], "_h": ln["h"]})
        elif quests and not quests[-1]["details"] and _wrapped_title(quests[-1], ln, text):
            quests[-1]["title"] += " " + _clean(text)        # long title wrapped onto a 2nd line
            quests[-1]["_y"] = ln["y"]
        elif quests:
            quests[-1]["details"].append(_clean(text))
    for q in quests:
        for k in ("_x", "_y", "_h"):
            q.pop(k, None)
    for q in quests:                                # "done" = every x/y objective complete
        objs = [re.match(r"(\d+)\s*/\s*(\d+)", d) for d in q["details"]]
        objs = [o for o in objs if o]
        q["done"] = bool(objs) and all(int(a) >= int(b) for a, b in (o.groups() for o in objs))
    return quests


class _OcrWorker:
    """One long-lived PowerShell running ocr.ps1 -Server; images go in by path, lines come back."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None

    def _start(self):
        self.proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", OCR_PS1, "-Server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=NO_WINDOW)

    def run(self, img):
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            img.save(path)
            with self.lock:
                for attempt in range(2):
                    try:
                        if self.proc is None or self.proc.poll() is not None:
                            self._start()
                        self.proc.stdin.write(path + chr(10))
                        self.proc.stdin.flush()
                        lines = []
                        while True:
                            raw = self.proc.stdout.readline()
                            if not raw:
                                raise OSError("ocr worker exited")
                            raw = raw.strip()
                            if raw == "##END##":
                                break
                            try:
                                lines.append(json.loads(raw, strict=False))
                            except ValueError:
                                pass
                        return sorted(lines, key=lambda ln: ln["y"])
                    except OSError:
                        self.proc = None            # restart it once, then give up for this image
            return []
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


_OCR = _OcrWorker()


def ocr(img, invert=True):
    """img: PIL image -> list of OCR lines. invert=True suits red/yellow titles, False the rest."""
    if invert:
        a = np.asarray(img.convert("RGB")).astype(float)
        g = np.clip((a.max(axis=2) - 60) * 255 / 140, 0, 255)       # coloured text -> bright
        pre = Image.fromarray((255 - g).astype("uint8")).convert("RGB")  # dark text on light
    else:
        pre = img.convert("RGB")
    pre = pre.resize((pre.width * 2, pre.height * 2), Image.LANCZOS)
    return _OCR.run(pre)


class QuestReader(threading.Thread):
    """Reads the tracker only when asked (refresh button): a short burst of reads, merged."""

    def __init__(self, screen, reads=5):
        super().__init__(daemon=True)
        self.screen, self.reads = screen, reads
        self.quests, self.t, self.error = [], 0, None
        self.busy = False
        self.wake = threading.Event()
        self.qwindow = None                          # what the quest log window showed at the last refresh
        self.on_refresh = None                       # callback(old_titles, new_titles)
        self.known_zones = None                      # zone names from the combat log

    def refresh(self):
        self.busy = True
        self.wake.set()
        self.seen = []                               # merged across reads: {key, quest, n, last, pos}

    @staticmethod
    def _sim(a, b):
        if min(len(a), len(b)) >= 6 and (a in b or b in a or a[:8] == b[:8]):
            return 1.0                               # OCR cut the title short
        return SequenceMatcher(None, a, b).ratio()

    @staticmethod
    def _key(title):
        return re.sub(r"[^a-z]", "", title.lower())

    def _merge(self, found):
        now = time.time()
        for pos, q in enumerate(found):
            k = self._key(q["title"])
            if len(k) < 4:
                continue
            best = max(self.seen, key=lambda e: self._sim(e["key"], k), default=None)
            if best and self._sim(best["key"], k) > 0.7:
                if len(k) > len(best["key"]):
                    best["key"] = k                  # keep the fullest version as the key
                best["n"] += 1
                best["last"], best["pos"] = now, pos
                best["variants"][q["title"]] = best["variants"].get(q["title"], 0) + 1
                if len(" ".join(q["details"])) >= len(" ".join(best["quest"]["details"])) * 0.7:
                    best["quest"]["details"], best["quest"]["done"] = q["details"], q["done"]
                if q["level"]:
                    best["quest"]["level"] = q["level"]
            else:
                self.seen.append({"key": k, "quest": dict(q), "n": 1, "last": now, "pos": pos,
                                  "variants": {q["title"]: 1}})
        self.seen = [e for e in self.seen if now - e["last"] < 120]      # turned in / abandoned
        # fold together entries that turned out to be the same quest
        merged = []
        for e in sorted(self.seen, key=lambda e: -e["n"]):
            twin = next((m for m in merged if self._sim(m["key"], e["key"]) > 0.7), None)
            if twin:
                twin["n"] += e["n"]
                for v, c in e["variants"].items():
                    twin["variants"][v] = twin["variants"].get(v, 0) + c
            else:
                merged.append(e)
        self.seen = merged
        out = []
        for e in sorted(self.seen, key=lambda e: e["pos"]):
            if e["n"] >= 2:
                q = dict(e["quest"])
                # most common spelling, favouring complete-looking titles
                q["title"] = max(e["variants"], key=lambda v: (e["variants"][v] + len(v) / 10, len(v)))
                out.append(q)
        return out

    def _read_once(self):
        from screenread import find_window
        _, rect = find_window("World of Warcraft")
        if not rect:
            raise RuntimeError("WoW window not found")
        wx, wy, wr, wb = rect
        sx, sy = (wr - wx) / 1920, (wb - wy) / 1080
        from screenread import region
        x0, y0, x1, y1 = region("quest_tracker")
        img = fastgrab.screen((wx + int(x0 * sx), wy + int(y0 * sy),
                                   wx + int(x1 * sx), wy + int(y1 * sy)))
        return parse(tag_colours(img, ocr(img, True))) + parse(tag_colours(img, ocr(img, False)))

    def run(self):
        while True:
            self.wake.wait()
            self.wake.clear()
            self.busy = True
            try:
                st = self.screen.snapshot() if self.screen else {"ok": True}
                if not st.get("ok"):
                    raise RuntimeError(st.get("reason") or "game not visible")
                self.seen = []                       # fresh read: forget the old list
                result = []
                for i in range(self.reads):
                    result = self._merge(self._read_once())
                    time.sleep(0.4)
                old = [q["title"] for q in self.quests]
                self.quests, self.t, self.error = result, time.time(), None
                try:                                 # quest log window open? read it in the same pass
                    self.qwindow = read_quest_window(list(result))
                    try:
                        from location import capture_map
                        capture_map(known_zones=list(self.known_zones or ()))
                    except Exception:
                        pass
                except Exception:
                    self.qwindow = None
                if self.on_refresh:
                    try:
                        self.on_refresh(old, [q["title"] for q in result])
                    except Exception:
                        pass
            except Exception as e:
                self.error = str(e)
            self.busy = False


# ---------------------------------------------------------------- lookups

def find_claude_cli():
    """Claude's command-line program on this PC, however it was installed: settings.json "claude_path", `claude`
    on the PATH (Claude Code), its usual folder, or the copy that comes inside the Claude desktop app."""
    import shutil
    import wowpath
    given = wowpath.settings().get("claude_path")
    if given and os.path.isfile(str(given)):
        return str(given)
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    home = os.path.expanduser("~")
    patterns = [
        os.path.join(home, ".local", "bin", "claude.exe"),
        os.path.join(home, ".claude", "local", "claude.exe"),
        os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude-code", "*", "*", "claude.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Claude-Data", "*", "claude-code", "*", "*", "claude.exe"),
    ]
    paths = [p for pat in patterns for p in glob.glob(pat)]
    return max(paths, key=os.path.getmtime) if paths else None


_MODE = [0.0, "missing"]


def claude_mode():
    """Quest look-ups by Claude on this PC: "on" (settings.json "claude": true, and Claude is here), "off" (it is
    here, not switched on) or "missing". Each look-up runs the owner's own Claude, on their plan: so it is asked for."""
    if time.time() - _MODE[0] > (3 if time.time() < _SIGNED["watch"] else 60):
        import wowpath
        try:
            here = bool(find_claude_cli())
        except Exception:
            here = False
        mode = "missing" if not here else "on" if wowpath.settings().get("claude") is True else "off"
        if mode == "on" and signed_in() is False:
            mode = "signed_out"                      # switched on, but this PC's Claude has nobody signed in
        _MODE[:] = [time.time(), mode]
    return _MODE[1]


_SIGNED = {"t": 0.0, "ok": None, "busy": False, "watch": 0.0}


def _check_signed_in():
    try:
        exe = find_claude_cli()
        out = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=40, creationflags=NO_WINDOW, cwd=tempfile.gettempdir())
        _SIGNED["ok"] = bool(json.loads(out.stdout).get("loggedIn"))
    except Exception:
        _SIGNED["ok"] = None                         # an older Claude without `auth status`: not known
    _SIGNED["t"], _SIGNED["busy"] = time.time(), False
    _MODE[0] = 0.0


def signed_in():
    """Is somebody signed in to this PC's Claude? True / False / None (not known yet, or it can't say). Asked with
    `claude auth status`, which costs nothing; in the background, at most every 2 minutes, so no page waits on it."""
    every = 4 if time.time() < _SIGNED["watch"] else 120          # right after "sign in": look often
    if time.time() - _SIGNED["t"] > every and not _SIGNED["busy"]:
        _SIGNED["busy"] = True
        threading.Thread(target=_check_signed_in, daemon=True).start()
    return _SIGNED["ok"]


def start_login():
    """The HUD's "Sign in to Claude": start Claude's own sign-in (`claude auth login`) in a window of its own. It
    opens the person's browser, and they sign in there themselves: the companion never sees a password or a key.
    -> True when it was started."""
    exe = find_claude_cli()
    if not exe:
        return False
    subprocess.Popen([exe, "auth", "login"], creationflags=0x00000010,          # CREATE_NEW_CONSOLE
                     cwd=tempfile.gettempdir())
    _SIGNED["watch"] = time.time() + 600                 # see it at once when they are through
    return True


def login_command():
    """What to type to sign this PC's Claude in (the full path: it is often not on the PATH)."""
    exe = find_claude_cli()
    return f'"{exe}" auth login' if exe else None


def set_claude(on):
    """The HUD's switch: remember it in settings.json. -> the mode now ("missing" when Claude is not on this PC)."""
    import wowpath
    wowpath.save_setting("claude", bool(on))
    _MODE[0] = _SIGNED["t"] = 0.0
    return claude_mode()


SPOT_KINDS = ("start", "objective", "turnin")
SPOTS_ASK = ('After the answer, add ONE last line that starts with SPOTS: followed by a JSON list (on that one line) of up '
             'to 6 places to go for this quest, most useful first, each '
             '{"zone": "<zone name>", "x": <zone map x, 0-100>, "y": <zone map y, 0-100>, "label": "<4 words at most>", '
             '"kind": "start" or "objective" or "turnin"}. Zone map coordinates as Wowhead shows them. For mobs spread '
             'over an area give the middle of the area. Only places you have coordinates for; SPOTS: [] if none.')


def parse_spots(text):
    """An answer that ends with a `SPOTS: [...]` line -> (the answer without it, the places as a clean list).
    Anything not shaped right is left out: this is text from a look-up, never trusted."""
    i = (text or "").rfind("SPOTS:")
    if i < 0:
        return text, None
    head, tail = text[:i].rstrip().rstrip("`").rstrip(), text[i + 6:].strip().strip("`").strip()
    try:
        raw = json.loads(tail[tail.index("["):tail.rindex("]") + 1])
    except ValueError:
        return head or text, None
    spots = []
    for s in raw if isinstance(raw, list) else []:
        try:
            x, y = float(s["x"]), float(s["y"])
            zone = str(s["zone"]).strip()[:40]
        except (KeyError, TypeError, ValueError):
            continue
        if not zone or not (0 <= x <= 100 and 0 <= y <= 100):
            continue
        kind = s.get("kind") if s.get("kind") in SPOT_KINDS else "objective"
        spots.append({"zone": zone, "x": round(x, 1), "y": round(y, 1), "kind": kind,
                      "label": re.sub(r"[^\w '\-:,.()]", "", str(s.get("label") or ""))[:40].strip()})
        if len(spots) == 8:
            break
    return head, spots


class Asker:
    def __init__(self):
        self.lock = threading.Lock()
        self.asks = {}                               # id -> {id, quest, status, answer, t}
        self._load()

    def _load(self):
        try:
            with open(ASKS_FILE, encoding="utf-8") as f:
                for line in f:
                    try:
                        a = json.loads(line)
                        self.asks.setdefault(a["id"], {}).update(a)
                    except (ValueError, KeyError, TypeError, AttributeError):
                        pass
        except OSError:
            pass

    def _append(self, rec):
        with open(ASKS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def ask(self, quest, context, again=False):
        # reuse an answer we already have for the same quest (again: look it up afresh, e.g. for its map spots)
        with self.lock:
            for a in ([] if again else self.asks.values()):
                if a.get("closed") and a.get("status") == "answered" and a.get("quest", {}).get("title") == quest.get("title"):
                    a["closed"] = False                  # the quest is in the log again: its answer comes back out
                    self._append({"id": a["id"], "closed": False})
                    return a
                if a.get("closed"):
                    continue
                if a.get("quest", {}).get("title") == quest.get("title") and a.get("status") in ("answered", "pending") \
                        and (a.get("status") == "answered" or time.time() - a.get("t", 0) < 300):   # (asked again while it's still being looked up: not another lookup)
                    return a
            rec = {"id": uuid.uuid4().hex[:8], "quest": quest, "context": context, "status": "pending",
                   "t": time.time()}
            self.asks[rec["id"]] = rec
            self._append(rec)
        threading.Thread(target=self._try_cli, args=(rec,), daemon=True).start()
        return rec

    def answer(self, ask_id, text, by="chat"):
        with self.lock:
            a = self.asks.get(ask_id)
            if not a:
                return False
            text, spots = parse_spots(text)
            a.update(status="answered", answer=text, by=by, answered_t=time.time(), spots=spots)
            self._append({"id": ask_id, "status": "answered", "answer": text, "by": by,
                          "answered_t": a["answered_t"], "spots": spots})
            return True

    def close(self, titles):
        """These quests left the log (finished or abandoned): put their look-ups away. They stay in asks.jsonl,
        off the pages and the maps, and come back without a new look-up if the quest is taken again."""
        with self.lock:
            for a in self.asks.values():
                if not a.get("closed") and (a.get("quest") or {}).get("title") in titles:
                    a["closed"] = True
                    self._append({"id": a["id"], "closed": True})

    def spots(self, titles=None):
        """The places of the newest answer for each quest (only the quests named, when a list is given):
        [{quest, zone, x, y, label, kind}]."""
        with self.lock:
            newest = {}
            for a in self.asks.values():
                t = (a.get("quest") or {}).get("title")
                if t and a.get("status") == "answered" and not a.get("closed") and (titles is None or t in titles):
                    if t not in newest or a.get("answered_t", 0) > newest[t].get("answered_t", 0):
                        newest[t] = a
            return [dict(s, quest=t) for t, a in newest.items() for s in (a.get("spots") or [])]

    def list(self):
        with self.lock:
            return sorted((a for a in self.asks.values() if not a.get("closed")), key=lambda a: -a.get("t", 0))[:40]

    def _try_cli(self, rec):
        exe = find_claude_cli()
        if not exe:
            self._mark(rec, "queued")
            return
        q = rec["quest"]
        prompt = (f"World of Warcraft Classic-era quest (WoW Forever): [{q.get('level')}] {q.get('title')}. "
                  f"Tracker text: {' | '.join(q.get('details', []))}. Player context: {rec.get('context')}. "
                  "Look it up on wowhead.com/forever (Forever adds quests that are not in Classic). Answer in under 120 words, plain text: where to go "
                  "(zone + map coords), what to do/kill/loot, drop rates or tips, and the reward if notable. " + SPOTS_ASK)
        try:
            out = subprocess.run([exe, "-p", prompt, "--allowedTools", "WebSearch,WebFetch"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, creationflags=NO_WINDOW,
                                 cwd=tempfile.gettempdir())
            text = (out.stdout or "").strip()
            if out.returncode == 0 and text and "not logged in" not in text.lower():
                self.answer(rec["id"], text, by="claude-cli")
                return
            if "not logged in" in text.lower():
                _SIGNED.update(ok=False, t=time.time())
                _MODE[0] = 0.0
        except (OSError, subprocess.TimeoutExpired):
            pass
        self._mark(rec, "queued")

    def _mark(self, rec, status):
        with self.lock:
            if rec.get("status") == "pending":
                rec["status"] = status
                self._append({"id": rec["id"], "status": status})


# ---------------------------------------------------------------- target name

TARGET_NAME_REGION = (1280, 753, 1402, 770)   # text above the target health bar


def name_mask(img):
    """Unit names are yellow/white text on a green (friendly), red (hostile) or dark band:
    keep only the text pixels, as black on white, enlarged."""
    a = np.asarray(img.convert("RGB")).astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    text = (r > 110) & (g > 90) & (r > b + 40)          # yellow-ish glyphs incl. their darker lower half
    out = np.where(text, 0, 255).astype("uint8")
    im = Image.fromarray(out).convert("RGB")
    im = im.resize((im.width * 3, im.height * 3), Image.LANCZOS)
    pad = Image.new("RGB", (im.width + 60, im.height + 40), "white")   # OCR wants a margin
    pad.paste(im, (30, 20))
    return pad


class PartyNamer(threading.Thread):
    """Reads the names on the party frames (they change only when the group does)."""

    def __init__(self, screen, every=4.0):
        super().__init__(daemon=True)
        self.screen, self.every = screen, every
        self.names = {}                              # slot -> name

    def run(self):
        while True:
            try:
                st = self.screen.snapshot()
                if st.get("ok") and st.get("party"):
                    from screenread import find_window, region
                    _, rect = find_window("World of Warcraft")
                    P = region("party")
                    if rect and P:
                        wx, wy, wr, wb = rect
                        sx, sy = (wr - wx) / 1920, (wb - wy) / 1080
                        seen = {}
                        for f in st["party"]:
                            x0, y0, x1, y1 = P["name"][f["slot"]]
                            img = fastgrab.screen((wx + int(x0 * sx), wy + int(y0 * sy),
                                                       wx + int(x1 * sx), wy + int(y1 * sy)))
                            txt = " ".join(ln["text"] for ln in ocr(name_mask(img), False))
                            txt = re.sub(r"[^A-Za-z' ]", "", txt).strip()
                            if len(txt) >= 3:
                                seen[f["slot"]] = txt.split()[0]
                        self.names = seen
            except Exception:
                pass
            time.sleep(self.every)


class TargetNamer(threading.Thread):
    """OCRs the target frame's name about once a second while something is targeted."""

    def __init__(self, screen, every=0.8):
        super().__init__(daemon=True)
        self.screen, self.every = screen, every
        self.name, self.t = None, 0

    def run(self):
        while True:
            try:
                st = self.screen.snapshot()
                if st.get("ok") and st.get("target") is not None:
                    from screenread import find_window
                    _, rect = find_window("World of Warcraft")
                    if rect:
                        wx, wy, wr, wb = rect
                        sx, sy = (wr - wx) / 1920, (wb - wy) / 1080
                        from screenread import region
                        x0, y0, x1, y1 = region("target_name")
                        img = fastgrab.screen((wx + int(x0 * sx), wy + int(y0 * sy),
                                                   wx + int(x1 * sx), wy + int(y1 * sy)))
                        lines = ocr(name_mask(img), False)
                        text = " ".join(ln["text"] for ln in lines).strip(" .,'`-")
                        text = re.sub(r"[^A-Za-z' -]", "", text).strip()
                        if len(text) >= 3:
                            self.name, self.t = text, time.time()
                else:
                    self.name = None
            except Exception:
                pass
            time.sleep(self.every)


# ---------------------------------------------------------------- open quest window

HEADINGS = {"description": "Description:", "rewards": "Rewards:", "objectives": "Objectives:",
            "progress": "Progress:", "completion": "Completion:"}


def _letters(t):
    return re.sub(r"[^a-z]", "", t.lower().replace("0", "o").replace("1", "l").replace("8", "b"))


def _fix_detail_lines(lines):
    out = []
    for t in lines:
        low = _letters(t)
        head = next((v for k, v in HEADINGS.items() if 5 <= len(low) <= 14 and SequenceMatcher(None, low, k).ratio() >= 0.55), None)
        if head:
            out.append(head)
            continue
        t = re.sub(r"^[YX][PF]\s+(?=\d)", "XP ", t)          # "YP 675" -> "XP 675"
        out.append(t)
    return out


def read_quest_window(known=()):
    """known: [{"title","level"}] quests we already know; used to repair the fancy-font title."""
    """Find the 'Map & Quest Log' window by its title and OCR its quest panel.
    Returns {"title", "level", "lines"} or raises RuntimeError."""
    from screenread import find_window
    _, rect = find_window("World of Warcraft")
    if not rect:
        raise RuntimeError("WoW window not found")
    wx, wy, wr, wb = rect
    sx, sy = (wr - wx) / 1920, (wb - wy) / 1080
    full = fastgrab.screen(rect)
    # 1) find the window title (OCR the top 2/3 of the screen at native size)
    probe = full.crop((0, 0, int(1500 * sx), int(760 * sy)))
    title = next((ln for ln in ocr(probe, False) if "quest log" in ln["text"].lower()), None)
    if not title:
        raise RuntimeError("Quest window isn't open (press L)")
    tx, ty = title["x"] / 2 / sx, title["y"] / 2 / sy          # ocr() upscales 2x
    # 2) the quest panel sits right of the map, at a fixed offset from the title text
    x0, x1, y0, y1 = tx + 228, tx + 540, ty + 22, ty + 522
    panel = full.crop((int(x0 * sx), int(y0 * sy), int(x1 * sx), int(y1 * sy)))
    raw = [ln for ln in ocr(panel, False) if ln["text"].strip()]
    if len(" ".join(ln["text"] for ln in raw)) < 20:
        raw = [ln for ln in ocr(panel, True) if ln["text"].strip()]
    junk = re.compile(r"^(q?\s*search quest log|quests:?|\d+\s*/\s*40|abandon|share|track|untrack|back|show map|"
                      r"stormwind city|camping|campaign)$", re.I)
    raw = [ln for ln in raw if not junk.match(ln["text"].strip())]
    titles = [ln for ln in raw if (m := TITLE_RE.search(ln["text"])) and m.start() <= 6]
    if len(titles) >= 3:
        # list view: every quest in the log, tracked or not
        return {"mode": "list", "quests": parse(raw, stop_at_gap=False), "t": time.time()}
    lines = _fix_detail_lines([_clean(ln["text"]) for ln in raw])
    qtitle, level = None, None
    if titles:
        m = TITLE_RE.search(titles[0]["text"])
        try:
            level = _fix_level(m.group(1))
        except ValueError:
            level = None
        qtitle = _clean(m.group(2))
    elif lines:
        qtitle = lines[0]
    # the detail title uses a decorative font OCR mangles: snap it to a quest we already know
    if qtitle and known:
        best = max(known, key=lambda q: SequenceMatcher(None, _letters(q["title"]), _letters(qtitle)).ratio())
        if SequenceMatcher(None, _letters(best["title"]), _letters(qtitle)).ratio() >= 0.5:
            if lines and lines[0] == qtitle:
                lines = lines[1:]
            qtitle, level = best["title"], level or best.get("level")
    return {"mode": "detail", "title": qtitle, "level": level, "lines": lines, "t": time.time()}
