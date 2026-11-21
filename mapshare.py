"""Map backups and sharing.

Backups (maps/backups/*.zip): a Mapping grid - shots, tiles, zones, remembered spots, areas, trail - plus its
raw clips (so replay_grid.py can rebuild it), the label pictures and the name lists. Made on request, on
companion start when the newest is over 6 h old, and before every restore / import. The newest 8 automatic ones are kept; ones you named are never removed.

Sharing: export = a map package (shots, tiles, zones, areas; no trail, no raw clips) another companion can
import. Import merges into your map of the same continent when both are lined up with the game's world
yards (the shift between the two 0,0s is exact); otherwise it comes in as its own map. Your own Mapping
shots keep winning over imported ones where both cover a spot.
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


def import_package(data):
    """Merge a shared map into yours (same continent, both lined up with the game) or add it as its own map.
    -> (map id, shots added, how). The caller reloads the maps afterwards."""
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
