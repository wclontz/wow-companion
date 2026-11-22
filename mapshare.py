"""Map backups and sharing.

Backups (maps/backups/*.zip): a Mapping grid - shots, tiles, zones, remembered spots, areas, trail - plus its
raw clips (so replay_grid.py can rebuild it), the label pictures and the name lists. Made on request, on
companion start when the newest is over 6 h old, and before every restore / import. The newest 8 automatic ones are kept; ones you named are never removed.

Sharing: export = a map package (shots, tiles, zones, areas; no trail, no raw clips) another companion can
import. Import merges into your map of the same continent when both are lined up with the game's world
yards (the shift between the two 0,0s is exact); otherwise it comes in as its own map. Your own Mapping
shots keep winning over imported ones where both cover a spot.

Send my map (2026-11-21): the full export of a well-explored continent is hundreds of MB. `send` packs only the
drawn map - its tiles as WebP, a few MB - with the scale and the world position of its 0,0. A companion that
is given one (`receive`, through the same Import button) keeps it in maps/friends/<continent>/ and draws it
UNDER its own map (`Under`, used by WorldMap.compose): a friend's ground shows wherever you have none yet,
and your own shots cover it as you explore. Nothing of yours is changed by it; `forget` removes it.
"""
import io
import json
import os
import shutil
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
GRIDS = os.path.join(HERE, "maps", "grids")
RAW = os.path.join(HERE, "maps", "minimap_raw")
BACKUPS = os.path.join(HERE, "maps", "backups")
KEEP = 8
EXTRA = ["subzones.json", "label_fixes.json", "label_images"]      # what the label reader has learned
EXTRA_ROOT = HERE


def _grid_ids():
    try:
        return [d for d in os.listdir(GRIDS) if os.path.isfile(os.path.join(GRIDS, d, "grid.json"))]
    except OSError:
        return []


def _add_dir(z, src, arc, skip=("tiles",)):
    """Pictures go in as JPEG (~5x smaller; turned back into PNG + the minimap mask when read back). The drawn
    tiles are left out: they're rebuilt from the shots."""
    from PIL import Image
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            p = os.path.join(root, f)
            if f.endswith(".tmp"):
                continue
            name = os.path.join(arc, os.path.relpath(p, src)).replace(os.sep, "/")
            try:                                        # (one unreadable or just-removed file must not stop
                if f.endswith(".png") and f.startswith(("c", "clip")):   # the backup - and with it every restore)
                    b = io.BytesIO()
                    Image.open(p).convert("RGB").save(b, "JPEG", quality=90)
                    z.writestr(name + ".jpg", b.getvalue())
                else:
                    z.write(p, name)
            except OSError:
                continue


def _unpack(z, m, dst):
    """One zip entry to disk; JPEG'd pictures back to PNG (map clips get the minimap mask again)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if m.endswith(".png.jpg"):
        from PIL import Image
        im = Image.open(io.BytesIO(z.read(m))).convert("RGB")
        dst = dst[:-4]
        if "clips" in m.replace("\\", "/").split("/"):
            from minimapper import disc_mask
            im = im.convert("RGBA")
            im.putalpha(disc_mask(im.width // 2))
        im.save(dst)
    else:
        with z.open(m) as src, open(dst, "wb") as out:
            shutil.copyfileobj(src, out)


def backup(reason="manual", keep=None):
    """Zip every map (+ raw clips + what the label reader learned). -> backup file name."""
    os.makedirs(BACKUPS, exist_ok=True)
    for f in os.listdir(BACKUPS):                       # half-written ones (the companion closed mid-backup):
        if f.endswith(".zip.tmp"):                      # 405 MB of them had piled up
            try:
                os.remove(os.path.join(BACKUPS, f))
            except OSError:
                pass
    gids = _grid_ids()
    if not gids:
        return None
    name = time.strftime("%Y-%m-%d_%H%M%S") + f"_{reason}.zip"
    tmp = os.path.join(BACKUPS, name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for gid in gids:
            _add_dir(z, os.path.join(GRIDS, gid), os.path.join("grids", gid))
            if os.path.isdir(os.path.join(RAW, gid)):
                _add_dir(z, os.path.join(RAW, gid), os.path.join("minimap_raw", gid))
        for e in EXTRA:
            p = os.path.join(EXTRA_ROOT, e)
            if os.path.isdir(p):
                _add_dir(z, p, e)
            elif os.path.isfile(p):
                z.write(p, e)
        z.writestr("backup.json", json.dumps({"made": time.time(), "reason": reason, "maps": gids}))
    os.replace(tmp, os.path.join(BACKUPS, name))
    # only automatic ones are thinned out - never one you named, never the one about to be restored
    for old in [b for b in list_backups() if b.get("reason") in ("auto", "before-restore", "before-import")][KEEP:]:
        if old["name"] == keep:
            continue
        try:
            os.remove(os.path.join(BACKUPS, old["name"]))
        except OSError:
            pass
    return name


def list_backups():
    out = []
    try:
        names = [f for f in os.listdir(BACKUPS) if f.endswith(".zip")]
    except OSError:
        names = []
    for f in names:
        p = os.path.join(BACKUPS, f)
        try:
            with zipfile.ZipFile(p) as z:
                info = json.loads(z.read("backup.json"))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            info = {}
        out.append({"name": f, "made": info.get("made") or os.path.getmtime(p), "reason": info.get("reason"),
                    "maps": info.get("maps") or [], "mb": round(os.path.getsize(p) / 1e6, 1)})
    return sorted(out, key=lambda b: -b["made"])


def auto_backup(hours=6):
    """On companion start: a backup when the newest is older than `hours` (and there is a map)."""
    b = list_backups()
    if _grid_ids() and (not b or time.time() - b[0]["made"] > hours * 3600):
        return backup("auto")
    return None


def restore(name):
    """Put a backup back (what's there now is backed up first). The caller reloads the maps afterwards."""
    p = os.path.join(BACKUPS, os.path.basename(name))
    if not os.path.isfile(p):
        raise FileNotFoundError(name)
    backup("before-restore", keep=os.path.basename(name))
    with zipfile.ZipFile(p) as z:
        gids = json.loads(z.read("backup.json")).get("maps") or []
        # (names only: "" or ".." here would have removed the whole maps folder)
        gids = [g for g in gids if isinstance(g, str) and g not in ("", ".", "..") and g == os.path.basename(g)]
        for gid in gids:                               # replace those maps (and their raw clips) wholesale
            for base in (GRIDS, RAW):
                d = os.path.join(base, gid)
                if os.path.isdir(d):
                    shutil.rmtree(d)
        for m in z.namelist():
            if m == "backup.json" or m.endswith("/"):
                continue
            parts = m.replace("\\", "/").split("/")
            base = {"grids": GRIDS, "minimap_raw": RAW}.get(parts[0])
            dst = os.path.normpath(os.path.join(base, *parts[1:]) if base else os.path.join(EXTRA_ROOT, *parts))
            roots = [GRIDS, RAW] + [os.path.join(EXTRA_ROOT, e) for e in EXTRA]
            if not any(dst == os.path.normpath(r) or dst.startswith(os.path.normpath(r) + os.sep) for r in roots):
                continue                               # (never write anywhere else - not even other files of the companion)
            _unpack(z, m, dst)
    for gid in gids:                                   # tiles weren't saved: draw them again
        p = os.path.join(GRIDS, gid, "meta.json")
        if os.path.exists(p):
            mm = json.load(open(p, encoding="utf-8"))
            mm["tiles"] = {}
            json.dump(mm, open(p, "w", encoding="utf-8"))
    return gids


# ------------------------------------------------------------------ sharing
def export(gid):
    """A map package (zip bytes) for another player: shots, tiles, zones, areas. No trail, no raw clips."""
    d = os.path.join(GRIDS, gid)
    gr = json.load(open(os.path.join(d, "grid.json"), encoding="utf-8"))
    gr.pop("trail", None)
    gr.pop("trail_session", None)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("grid.json", json.dumps(gr))
        z.write(os.path.join(d, "meta.json"), "meta.json")
        if os.path.isdir(os.path.join(d, "clips")):
            _add_dir(z, os.path.join(d, "clips"), "clips")
        z.writestr("package.json", json.dumps({"kind": "wow-companion-map", "made": time.time(), "map": gr.get("name"),
                                               "continent": gr.get("continent")}))
    return buf.getvalue()


FRIENDS = os.path.join(HERE, "maps", "friends")
PICTURE = "wow-companion-map-picture"
_TILE = __import__("re").compile(r"^tiles/L([01])_(-?\d{1,4})_(-?\d{1,4})\.webp$")
MAX_TILES, MAX_TILE_BYTES = 3000, 3_000_000


def _safe(name):
    return "".join(ch for ch in str(name or "") if ch.isalnum() or ch in "_-")[:40]


def zones(gid):
    """The zones of a map that can be sent on their own (the ones whose rectangle is known), by name."""
    try:
        gr = json.load(open(os.path.join(GRIDS, gid, "grid.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sorted({f["zone"] for f in (gr.get("frames") or {}).values()
                   if f.get("bounds") and f.get("zone") and not f.get("pending")})


def send(gid, zone=None):
    """Your drawn map of one continent as a small package (zip bytes) for a friend: the tiles of the outdoor and
    the indoor view as WebP, the scale they are drawn at and where the map's 0,0 is in world yards.
    zone: only that zone's rectangle of it (everything outside is left see-through)."""
    from PIL import Image
    d = os.path.join(GRIDS, gid)
    gr = json.load(open(os.path.join(d, "grid.json"), encoding="utf-8"))
    m = json.load(open(os.path.join(d, "meta.json"), encoding="utf-8"))
    if not (gr.get("aligned") and gr.get("continent") and gr.get("w0") and m.get("draw_k")):
        raise ValueError("this map is not lined up with the game's world yet (the add-on does that): nothing to send")
    box = None                                        # the zone's rectangle in the map's px
    if zone:
        fr = [f for f in (gr.get("frames") or {}).values() if f.get("zone") == zone and f.get("bounds") and not f.get("pending")]
        if not fr:
            raise ValueError("that zone's edges are not known on this map")
        f, k = fr[0], m["draw_k"]
        xs, ys = sorted((k * f["ox"], k * (f["ox"] + 100 * f["sx"]))), sorted((k * f["oy"], k * (f["oy"] + 100 * f["sy"])))
        box = (int(xs[0]), int(ys[0]), int(xs[1]) + 1, int(ys[1]) + 1)
    buf, n = io.BytesIO(), 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for L in ("0", "1"):
            for key in (m.get("tiles") or {}).get(L) or {}:
                tx, ty = key.split(",")
                if box and (int(tx) * 1000 >= box[2] or int(tx) * 1000 + 1000 <= box[0]
                            or int(ty) * 1000 >= box[3] or int(ty) * 1000 + 1000 <= box[1]):
                    continue
                try:
                    im = Image.open(os.path.join(d, "tiles", f"L{L}_{tx}_{ty}.png")).convert("RGBA")
                except OSError:
                    continue
                if box:
                    cut = Image.new("RGBA", im.size, (0, 0, 0, 0))
                    part = (max(0, box[0] - int(tx) * 1000), max(0, box[1] - int(ty) * 1000),
                            min(1000, box[2] - int(tx) * 1000), min(1000, box[3] - int(ty) * 1000))
                    cut.paste(im.crop(part), part[:2])
                    im = cut
                    if im.getchannel("A").getextrema()[1] == 0:
                        continue
                b = io.BytesIO()
                im.save(b, "WEBP", quality=80, method=4)
                z.writestr(f"tiles/L{L}_{tx}_{ty}.webp", b.getvalue())
                n += 1
        if not n:
            raise ValueError("this map has nothing drawn yet")
        z.writestr("package.json", json.dumps({"kind": PICTURE, "made": time.time(), "continent": gr["continent"],
                                               "k": m["draw_k"], "w0": gr["w0"], "tile": 1000, "tiles": n}))
    return buf.getvalue()


def receive(data):
    """A friend's `send` package -> kept in maps/friends/<continent>/. A package drawn at the same scale from the
    same 0,0 as the one already kept (the same friend, another zone or a newer map) is laid over it; any other
    replaces it. Every entry is checked: it is a file from somewhere else. -> (continent, tiles kept)"""
    from PIL import Image
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        pk = json.loads(z.read("package.json"))
        if pk.get("kind") != PICTURE:
            raise ValueError("not a map picture package")
        cont, k, w0 = pk.get("continent"), pk.get("k"), pk.get("w0")
        if not (isinstance(cont, str) and _safe(cont) and isinstance(k, (int, float)) and 0.2 <= k <= 5
                and isinstance(w0, list) and len(w0) == 2 and all(isinstance(v, (int, float)) and abs(v) < 1e6 for v in w0)
                and pk.get("tile") == 1000):
            raise ValueError("the package's description is not usable")
        names = [i for i in z.infolist() if _TILE.match(i.filename)]
        if not names or len(names) > MAX_TILES:
            raise ValueError("no map tiles in the package (or far too many)")
        tmp = os.path.join(FRIENDS, _safe(cont) + ".new")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, "tiles"))
        kept = 0
        for i in names:
            if i.file_size > MAX_TILE_BYTES:
                continue
            try:
                im = Image.open(io.BytesIO(z.read(i)))
                if im.format != "WEBP" or im.size != (1000, 1000):
                    continue
                im.convert("RGBA").save(os.path.join(tmp, "tiles", os.path.basename(i.filename)), "WEBP", quality=80, method=4)
                kept += 1
            except Exception:
                continue
        if not kept:
            shutil.rmtree(tmp, ignore_errors=True)
            raise ValueError("none of the package's tiles could be read")
        json.dump({"kind": PICTURE, "made": float(pk.get("made") or 0), "got": time.time(), "continent": cont,
                   "k": float(k), "w0": [float(w0[0]), float(w0[1])], "tiles": kept},
                  open(os.path.join(tmp, "package.json"), "w", encoding="utf-8"))
    dst = os.path.join(FRIENDS, _safe(cont))
    try:
        old = json.load(open(os.path.join(dst, "package.json"), encoding="utf-8"))
    except (OSError, ValueError):
        old = None
    if old and abs(old.get("k", 0) - k) < 1e-6 and all(abs(a - b) < 1e-6 for a, b in zip(old.get("w0") or [0, 0], w0)):
        for f in os.listdir(os.path.join(dst, "tiles")):        # the same map's frame: the new tiles go over the old
            new = os.path.join(tmp, "tiles", f)
            try:
                under = Image.open(os.path.join(dst, "tiles", f)).convert("RGBA")
                if os.path.exists(new):
                    under.alpha_composite(Image.open(new).convert("RGBA"))
                under.save(new, "WEBP", quality=80, method=4)
            except Exception:
                continue
    shutil.rmtree(dst, ignore_errors=True)
    os.replace(tmp, dst)
    return cont, kept


def friends():
    """The friends' maps kept: [{"continent", "tiles", "got"}]"""
    out = []
    try:
        for d in sorted(os.listdir(FRIENDS)):
            try:
                pk = json.load(open(os.path.join(FRIENDS, d, "package.json"), encoding="utf-8"))
                out.append({"continent": pk["continent"], "tiles": pk.get("tiles"), "got": pk.get("got")})
            except (OSError, ValueError, KeyError):
                continue
    except OSError:
        pass
    return out


def forget(continent=None):
    """Removes the friends' maps (of one continent, or all). -> how many"""
    n = 0
    for f in friends():
        if continent in (None, f["continent"]):
            shutil.rmtree(os.path.join(FRIENDS, _safe(f["continent"])), ignore_errors=True)
            n += 1
    return n


def kind(data):
    """What a package is ("wow-companion-map", the picture kind, or None)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return json.loads(z.read("package.json")).get("kind")
    except Exception:
        return None


class Under:
    """A friend's map of a continent, as seen from one of your maps: its tiles cut and scaled to yours.
    yours: canvas px = ref * (world - w0); theirs: px = k * (world - their w0)."""

    def __init__(self, continent, w0, ref):
        self.ok = False
        try:
            self.dir = os.path.join(FRIENDS, _safe(continent))
            pk = json.load(open(os.path.join(self.dir, "package.json"), encoding="utf-8"))
            if pk.get("continent") != continent:
                return
            self.k, self.ref = pk["k"], ref or pk["k"]
            self.dx, self.dy = pk["w0"][0] - w0[0], pk["w0"][1] - w0[1]      # their 0,0 in your yards
            self.stamp = repr(pk.get("got"))
            self.have = {}
            for f in os.listdir(os.path.join(self.dir, "tiles")):
                mt = _TILE.match("tiles/" + f)
                if mt:
                    self.have.setdefault(int(mt.group(1)), set()).add((int(mt.group(2)), int(mt.group(3))))
            self.ok = bool(self.have)
        except (OSError, ValueError, KeyError, TypeError):
            self.ok = False

    def _theirs(self, X, Y):
        """Your canvas px -> their canvas px."""
        return self.k * (X / self.ref - self.dx), self.k * (Y / self.ref - self.dy)

    def tiles(self, L, T=1000):
        """Your tiles (tx, ty) of level L that their map has something under."""
        out = set()
        s = self.ref / self.k
        for ax, ay in self.have.get(L, ()):
            x0, y0 = (ax * 1000 / self.k + self.dx) * self.ref, (ay * 1000 / self.k + self.dy) * self.ref
            for tx in range(int(x0 // T), int((x0 + 1000 * s - 1) // T) + 1):
                for ty in range(int(y0 // T), int((y0 + 1000 * s - 1) // T) + 1):
                    out.add((tx, ty))
        return out

    def tile(self, L, tx, ty, T=1000):
        """Their map under your tile (an RGBA T x T picture), or None."""
        from PIL import Image
        (ax0, ay0), (ax1, ay1) = self._theirs(tx * T, ty * T), self._theirs((tx + 1) * T, (ty + 1) * T)
        w, h = max(1, int(round(ax1 - ax0))), max(1, int(round(ay1 - ay0)))
        part, any_ = Image.new("RGBA", (w, h), (0, 0, 0, 0)), False
        for ax in range(int(ax0 // 1000), int((ax1 - 1) // 1000) + 1):
            for ay in range(int(ay0 // 1000), int((ay1 - 1) // 1000) + 1):
                if (ax, ay) not in self.have.get(L, ()):
                    continue
                try:
                    im = Image.open(os.path.join(self.dir, "tiles", f"L{L}_{ax}_{ay}.webp")).convert("RGBA")
                except Exception:
                    continue
                part.paste(im, (int(round(ax * 1000 - ax0)), int(round(ay * 1000 - ay0))))
                any_ = True
        if not any_:
            return None
        if (w, h) != (T, T):
            part = part.resize((T, T), Image.BILINEAR)
        return part if part.getchannel("A").getextrema()[1] > 0 else None


def import_package(data):
    """Merge a shared map into yours (same continent, both lined up with the game) or add it as its own map.
    -> (map id, shots added, how). The caller reloads the maps afterwards."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z0:
            kind = json.loads(z0.read("package.json")).get("kind")
    except Exception:
        raise ValueError("not a WoW companion map package")
    if kind == PICTURE:                               # a friend's "send my map": kept beside your maps, drawn under
        cont, kept = receive(data)
        return cont, kept, "kept as a friend's map: it shows wherever you have not been yet"
    backup("before-import")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        pk = json.loads(z.read("package.json"))
        if pk.get("kind") != "wow-companion-map":
            raise ValueError("not a WoW companion map package")
        theirs = json.loads(z.read("grid.json"))
        tm = json.loads(z.read("meta.json"))
        target = None
        for gid in _grid_ids():
            g = json.load(open(os.path.join(GRIDS, gid, "grid.json"), encoding="utf-8"))
            if g.get("aligned") and theirs.get("aligned") and g.get("continent") and g.get("continent") == theirs.get("continent"):
                target = gid
                break
        if target is None:                            # its own map
            gid = ("".join(ch for ch in str(theirs.get("id") or "") if ch.isalnum() or ch in "_-") or "Shared") + "_shared"
            while os.path.exists(os.path.join(GRIDS, gid)):
                gid += "x"
            d = os.path.join(GRIDS, gid)
            os.makedirs(d)
            theirs["id"], theirs["name"] = gid, (theirs.get("name") or "Shared") + " (shared)"
            json.dump(theirs, open(os.path.join(d, "grid.json"), "w", encoding="utf-8"))
            tm["place"], tm["tiles"] = gid, {}
            json.dump(tm, open(os.path.join(d, "meta.json"), "w", encoding="utf-8"))
            for m in z.namelist():
                if m.startswith("clips/") and not m.endswith("/"):
                    _unpack(z, m, os.path.join(d, "clips", os.path.basename(m)))
            return gid, len(tm.get("clips") or []), "as its own map"
        # merge: their grid -> ours by the difference of the two 0,0s (both are world yards)
        d = os.path.join(GRIDS, target)
        ours = json.load(open(os.path.join(d, "grid.json"), encoding="utf-8"))
        om = json.load(open(os.path.join(d, "meta.json"), encoding="utf-8"))
        dx, dy = theirs["w0"][0] - ours["w0"][0], theirs["w0"][1] - ours["w0"][1]
        nid = max([c["id"] for c in om["clips"]] or [0])
        tag = "imp" + time.strftime("%m%d%H%M")
        added = 0
        for c in tm.get("clips") or []:
            if "file" not in c:
                continue
            nid += 1
            nc = {k: v for k, v in c.items() if k not in ("vote", "sim", "vote_n", "bad")}
            nc.update(id=nid, x=round(c["x"] + dx, 2), y=round(c["y"] + dy, 2), run=f"{tag}-{c['run']}",
                      file=f"c{nid:05d}.png", imported=True, mapping=False)      # yours win where both cover a spot
            try:
                _unpack(z, "clips/" + c["file"] + ".jpg", os.path.join(d, "clips", nc["file"] + ".jpg"))
            except KeyError:
                continue
            om["clips"].append(nc)
            added += 1
        for r, R in (tm.get("runs") or {}).items():
            R = dict(R)
            R["p"] = [[a, b, round(x + dx, 2), round(y + dy, 2)] for a, b, x, y in R.get("p") or []]
            om["runs"][f"{tag}-{r}"] = R
        om["tiles"] = {}
        json.dump(om, open(os.path.join(d, "meta.json"), "w", encoding="utf-8"))
        for f_id, f in (theirs.get("frames") or {}).items():
            ours["frames"].setdefault(f_id, f) if f.get("bounds") else None
        for k, c in (theirs.get("cells") or {}).items():
            oc = ours["cells"].setdefault(k, {"x": c["x"], "y": c["y"], "labels": {}})
            for l, n in c["labels"].items():
                oc["labels"][l] = oc["labels"].get(l, 0) + n
        A = 12.0
        for lab, e in (theirs.get("areas") or {}).items():
            t = ours.setdefault("areas", {}).setdefault(lab, {"name": e["name"], "cells": {}, "n": 0})
            t["n"] = t.get("n", 0) + e.get("n", 0)
            for key, n in e["cells"].items():
                i, j = map(int, key.split(","))
                k2 = f"{int(((i + .5) * A + dx) // A)},{int(((j + .5) * A + dy) // A)}"
                t["cells"][k2] = t["cells"].get(k2, 0) + n
        json.dump(ours, open(os.path.join(d, "grid.json"), "w", encoding="utf-8"))
        return target, added, "merged into your map"
