"""Re-stitch a minimap map from its kept pieces, without the combat log.

Every pair of pieces that should overlap is matched directly (phase correlation on the overlap,
ignoring black and the ring), then all piece positions are solved together (least squares), so one
bad match or a drifted track can't push a room into the wrong place. Black (unexplored) is see-through,
and pieces with a UI menu over the minimap are dropped.

    python restitch.py "The Hall of Thanes"
"""
import json
import math
import os
import sys

import numpy as np
from PIL import Image

from minimapper import ROOT, safe, phase_shift, shot_k, to_scale, center_pad


_CACHE = {}


def k_indoor():
    """Minimap px per yard in WoW's indoor view (dungeons, cities), fully zoomed out: screen_layout's
    minimap px_per_yd_in (1.24 on the 2026-10-09 layout). The user keeps the minimap fully zoomed out."""
    try:
        from screenread import region
        return float(region("minimap").get("px_per_yd_in") or 1.24)
    except Exception:
        return 1.24


def load(place):
    d = os.path.join(ROOT, safe(place))
    with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
        m = json.load(f)
    world = m.get("unit") == "world"
    kmap = k_indoor() if world else None
    pieces = []
    for key, c in m["cells"].items():
        try:
            im = Image.open(os.path.join(d, c["file"])).convert("RGBA")
        except OSError:
            continue
        if world:
            # dungeons: every shot at today's scale. Shots kept from the old UI were taken at another zoom
            # (1.26 px/yd against 1.40): unscaled, the rooms in them came out 11% small next to new ones.
            im = to_scale(im, shot_k(c, im.width, kmap), kmap)
        pieces.append({"key": key, "file": c["file"], "img": im, "x": c["px"], "y": c["py"], "off": c.get("off", 0),
                       "t": c.get("t", 0)})
    # one size for all (matching needs it): the whole round shot in the middle of a see-through square. (This
    # used to trim every dungeon shot to its middle 208 px - a leftover of the oldest minimap - which threw
    # away more than half of each picture.)
    size = max([p["img"].width for p in pieces] or [0]) if world else 2 * (m.get("r") or 104)
    for q in pieces:
        if q["img"].width != size or q["img"].height != size:
            q["img"] = center_pad(q["img"], size)
    return d, m, pieces


def has_menu(im):
    """A dropdown/tooltip over the minimap: a big flat dark-grey box with light text rows."""
    a = np.asarray(im.convert("RGB"), dtype=float)
    al = np.asarray(im)[..., 3] > 0
    g = a.mean(axis=2)
    flat = (np.abs(a[..., 0] - a[..., 2]) < 6) & (g > 20) & (g < 70)          # neutral dark grey
    white = (g > 225) & (np.abs(a[..., 0] - a[..., 2]) < 20)
    return (flat & al).mean() > 0.12 and (white & al).mean() > 0.01


def gray_masked(im):
    a = np.asarray(im, dtype=float)
    g = a[..., :3].mean(axis=2)
    ok = (a[..., 3] > 0) & (g > 12)                    # not outside the circle, not black
    return g, ok


def match(pa, pb):
    """Offset of piece b relative to piece a (b.pos - a.pos) from the pictures, or None."""
    ga, oka = gray_masked(pa["img"])
    gb, okb = gray_masked(pb["img"])
    f = 1
    if ga.shape[0] > 256:                               # big shots: match at half size (4x faster, +-1 px)
        f = 2
        ga, oka, gb, okb = ga[::2, ::2], oka[::2, ::2], gb[::2, ::2], okb[::2, ::2]
    n = ga.shape[0]
    S = 2 * n
    A = np.zeros((S, S)); B = np.zeros((S, S))
    ma = np.where(oka, ga - ga[oka].mean(), 0) if oka.any() else None
    mb = np.where(okb, gb - gb[okb].mean(), 0) if okb.any() else None
    if ma is None or mb is None:
        return None
    A[:n, :n] = ma
    B[:n, :n] = mb
    fa, fb = np.fft.fft2(A), np.fft.fft2(B)
    r = fa * np.conj(fb)
    r /= np.abs(r) + 1e-9
    c = np.fft.ifft2(r).real
    iy, ix = np.unravel_index(np.argmax(c), c.shape)
    peak = c[iy, ix] / (c.std() + 1e-9)
    dy = iy - S if iy > S // 2 else iy
    dx = ix - S if ix > S // 2 else ix
    return dx * f, dy * f, peak


def solve(pieces, pairs):
    """Least squares positions from pairwise offsets (piece 0 fixed), robust to bad pairs."""
    n = len(pieces)
    use = list(pairs)
    for _ in range(5):
        rows, bx, by, w = [], [], [], []
        for i, j, dx, dy, wt in use:
            row = np.zeros(n); row[j] = 1; row[i] = -1
            rows.append(row); bx.append(dx); by.append(dy); w.append(wt)
        row = np.zeros(n); row[0] = 1                   # anchor
        rows.append(row); bx.append(pieces[0]["x"]); by.append(pieces[0]["y"]); w.append(100)
        Am = np.array(rows) * np.array(w)[:, None]
        X = np.linalg.lstsq(Am, np.array(bx) * w, rcond=None)[0]
        Y = np.linalg.lstsq(Am, np.array(by) * w, rcond=None)[0]
        res = [math.hypot(X[j] - X[i] - dx, Y[j] - Y[i] - dy) for i, j, dx, dy, _ in use]
        bad = [k for k, e in enumerate(res) if e > 8]
        if not bad:
            break
        worst = max(bad, key=lambda k: res[k])
        use.pop(worst)                                   # drop the worst disagreeing match, re-solve
    return X, Y, use


def compose(pieces, X, Y, out, bounds=None):
    r = pieces[0]["img"].width / 2
    if bounds:
        x0, y0, W, H = bounds
    else:
        x0, y0 = min(X) - r, min(Y) - r
        W, H = int(max(X) + r - x0) + 1, int(max(Y) + r - y0) + 1
    # Every spot is drawn from the shot whose MIDDLE is nearest to it: the middle of a shot is where you stood
    # (sharpest, nothing drawn over it but your arrow), its rim is where edge markers sit. Walking pieces
    # (estimated spots) only show where no kill shot has anything. Black = unexplored: see-through.
    n = pieces[0]["img"].width
    yy, xx = np.mgrid[:n, :n]
    dist = np.hypot(xx - r + 0.5, yy - r + 0.5)
    rgb = np.zeros((H, W, 3), dtype=np.uint8)
    alpha = np.zeros((H, W), dtype=np.uint8)
    best = np.full((H, W), np.inf)
    who = np.full((H, W), -1, dtype=np.int32)          # which shot each spot was drawn from
    placed = []                                        # kill shots as drawn: (number, pixels, usable, ox, oy)
    holes = []
    for num, (p, x, y) in enumerate(zip(pieces, X, Y)):
        a = np.asarray(p["img"])
        if a.shape[0] != n or a.shape[1] != n:
            continue
        g = a[..., :3].astype(int).mean(axis=2)
        ok = (a[..., 3] > 128) & (g >= 14)
        fill = p.get("off", 0) >= 1000
        d = dist + (1e6 if fill else 0.0)
        ox, oy = int(x - x0 - r), int(y - y0 - r)
        sx0, sy0, sx1, sy1 = max(0, -ox), max(0, -oy), min(n, W - ox), min(n, H - oy)
        if sx1 <= sx0 or sy1 <= sy0:
            continue
        cs = (slice(oy + sy0, oy + sy1), slice(ox + sx0, ox + sx1))
        ps = (slice(sy0, sy1), slice(sx0, sx1))
        take = ok[ps] & (d[ps] < best[cs])
        rgb[cs][take] = a[..., :3][ps][take]
        alpha[cs][take] = 255
        best[cs][take] = d[ps][take]
        who[cs][take] = num
        if not fill:
            placed.append((num, a[..., :3], ok, ox, oy))
            # your arrow is cut out of every shot: remember the floor colour around it
            ring = (dist > 17) & (dist < 27) & ok
            if ring.sum() > 60:
                holes.append((ox + r, oy + r, np.median(a[..., :3][ring], axis=0)))
    _undot(rgb, who, placed, n)
    # the arrow's hole, where no other shot covers it: the floor colour from around it (it was a white dot)
    HR = 19
    for hx, hy, col in holes:
        x_a, x_b, y_a, y_b = int(max(0, hx - HR)), int(min(W, hx + HR + 1)), int(max(0, hy - HR)), int(min(H, hy + HR + 1))
        if x_b <= x_a or y_b <= y_a:
            continue
        gy, gx = np.mgrid[y_a:y_b, x_a:x_b]
        m_ = (np.hypot(gx - hx + 0.5, gy - hy + 0.5) <= HR) & (alpha[y_a:y_b, x_a:x_b] == 0)
        rgb[y_a:y_b, x_a:x_b][m_] = col.astype(np.uint8)
        alpha[y_a:y_b, x_a:x_b][m_] = 255
    canvas = Image.fromarray(np.dstack([rgb, alpha]), "RGBA")
    tmp = out + ".tmp.png"                              # (a page may be loading the old picture this instant)
    canvas.save(tmp, "PNG")
    for i in range(6):
        try:
            os.replace(tmp, out)
            break
        except PermissionError:
            if i == 5:
                raise
            import time as _tm
            _tm.sleep(0.05)
    return [round(x0, 1), round(y0, 1)], [W, H]


DOT_MIN, DOT_DIFF, DOT_RIM = 3, 40, 14   # shots that must cover a spot / how far off (0-255, any channel) is a dot / its rim


def _grow(mask, n=2):
    """The mask and everything within n pixels of it (straight or diagonal)."""
    for _ in range(n):
        g = mask.copy()
        g[1:] |= mask[:-1]; g[:-1] |= mask[1:]
        g[:, 1:] |= mask[:, :-1]; g[:, :-1] |= mask[:, 1:]
        g[1:, 1:] |= mask[:-1, :-1]; g[:-1, :-1] |= mask[1:, 1:]
        g[1:, :-1] |= mask[:-1, 1:]; g[:-1, 1:] |= mask[1:, :-1]
        mask = g
    return mask


def _undot(rgb, who, placed, n):
    """Party and mob dots are part of the shot they were in, and the nearest shot wins every spot, so they got
    baked into the map. A dot is somewhere else (or gone) in the other shots: where DOT_MIN or more kill shots
    cover a spot and the one drawn is far (DOT_DIFF) from their middle colour, the middle colour is drawn
    instead, and so is the dot's dark rim (within 2 px, DOT_RIM off). Spots that agree are left as they were
    (taking the middle everywhere would blur a map whose shots are a pixel or two apart). Returns how many
    pixels were changed."""
    H, W = who.shape
    changed = 0
    for num, a, ok, ox, oy in placed:
        y0, y1, x0, x1 = max(0, oy), min(H, oy + n), max(0, ox), min(W, ox + n)
        if y1 <= y0 or x1 <= x0:
            continue
        mine = who[y0:y1, x0:x1] == num
        if not mine.any():
            continue
        # only the patch this shot was drawn on (on a well-covered map: a small part of it)
        ys, xs = np.nonzero(mine.any(axis=1))[0], np.nonzero(mine.any(axis=0))[0]
        y0, y1, x0, x1 = y0 + int(ys[0]), y0 + int(ys[-1]) + 1, x0 + int(xs[0]), x0 + int(xs[-1]) + 1
        mine = who[y0:y1, x0:x1] == num
        near = [q for q in placed if q[3] < x1 and q[3] + n > x0 and q[4] < y1 and q[4] + n > y0]
        if len(near) < DOT_MIN:
            continue
        h, w = y1 - y0, x1 - x0
        stack = np.full((len(near), h, w, 3), np.nan, dtype=np.float32)
        for k, (_, b, okb, bx, by) in enumerate(near):
            # the part of shot b that lies on this shot's patch of the canvas
            cy0, cy1, cx0, cx1 = max(y0, by), min(y1, by + n), max(x0, bx), min(x1, bx + n)
            if cy1 <= cy0 or cx1 <= cx0:
                continue
            part = b[cy0 - by:cy1 - by, cx0 - bx:cx1 - bx].astype(np.float32)
            part[~okb[cy0 - by:cy1 - by, cx0 - bx:cx1 - bx]] = np.nan
            stack[k, cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0] = part
        have = ~np.isnan(stack[..., 0])
        with np.errstate(all="ignore"):
            # the middle colour costs a sort: only where the shots disagree at all (most of the map: they agree)
            lo = np.where(have[..., None], stack, np.inf).min(axis=0)
            hi = np.where(have[..., None], stack, -np.inf).max(axis=0)
            look = mine & (have.sum(axis=0) >= DOT_MIN) & ((hi - lo).max(axis=2) > DOT_RIM)
            if not look.any():
                continue
            mid = np.nanmedian(stack[:, look], axis=0)
        diff = np.zeros((h, w), dtype=np.float32)
        diff[look] = np.abs(rgb[y0:y1, x0:x1][look].astype(np.float32) - mid).max(axis=1)
        dot = diff > DOT_DIFF
        if not dot.any():
            continue
        dot = _grow(dot) & (diff > DOT_RIM)
        full = np.zeros((h, w, 3), dtype=np.float32)
        full[look] = mid
        rgb[y0:y1, x0:x1][dot] = full[dot].round().astype(np.uint8)
        changed += int(dot.sum())
    return changed


def solve_joint(pieces, pairs, anchors, square=True, k_fixed=None):
    """Unknowns: X, Y of every piece, plus k, cx, cy (world -> canvas: k * (u, v) + (cx, cy)).
    Rows: pair matches (Xj - Xi = dx ...), anchors (X_p + dx - k u - cx = 0 ...), piece 0 fixed.
    Bad pairs/anchors are dropped one at a time and re-solved."""
    n = len(pieces)
    idx = {p["key"]: i for i, p in enumerate(pieces)}
    anc = [a for a in anchors if a["piece"] in idx]
    use_p, use_a = list(pairs), list(anc)
    K, CX, CY, KY = 2 * n, 2 * n + 1, 2 * n + 2, 2 * n + 3     # KY: separate north-south scale (map %)
    for _ in range(40):
        rows, rhs, w = [], [], []

        def add(coefs, val, wt):
            r = np.zeros(2 * n + 4)
            for c, v in coefs:
                r[c] += v
            rows.append(r); rhs.append(val); w.append(wt)
        for i, j, dx, dy, wt in use_p:
            add([(j, 1), (i, -1)], dx, wt)
            add([(n + j, 1), (n + i, -1)], dy, wt)
        for a in use_a:
            pi = idx[a["piece"]]
            wa = 3.0 if a.get("shot") else 0.6          # a shot taken at the logged moment is the best anchor
            add([(pi, 1), (K, -a["u"]), (CX, -1)], -a["dx"], wa)
            add([(n + pi, 1), (KY, -a["v"]), (CY, -1)], -a["dy"], wa)
        if k_fixed:                                  # scale known (measured): hold it
            add([(K, 1)], k_fixed[0], 1000)
            add([(KY, 1)], k_fixed[1], 1000)
        elif square:                                 # yards: same scale both ways
            add([(K, 1), (KY, -1)], 0.0, 1000)
        add([(0, 1)], pieces[0]["x"], 100)
        add([(n, 1)], pieces[0]["y"], 100)
        A = np.array(rows) * np.array(w)[:, None]
        sol = np.linalg.lstsq(A, np.array(rhs) * np.array(w), rcond=None)[0]
        X, Y, k, cx, cy, ky = sol[:n], sol[n:2 * n], sol[K], sol[CX], sol[CY], sol[KY]
        pres = [math.hypot(X[j] - X[i] - dx, Y[j] - Y[i] - dy) for i, j, dx, dy, _ in use_p]
        ares = [math.hypot(X[idx[a["piece"]]] + a["dx"] - k * a["u"] - cx,
                           Y[idx[a["piece"]]] + a["dy"] - ky * a["v"] - cy) for a in use_a]
        worst_p = max(range(len(pres)), key=lambda q: pres[q], default=None)
        worst_a = max(range(len(ares)), key=lambda q: ares[q], default=None)
        wp = pres[worst_p] if worst_p is not None else 0
        wa = ares[worst_a] if worst_a is not None else 0
        if wp <= 8 and wa <= 10:
            break
        if not use_p and wa <= 10:
            break
        if wp / 8 >= wa / 10:
            use_p.pop(worst_p)
        else:
            use_a.pop(worst_a)
    return X, Y, float(k), float(cx), float(cy), use_p, use_a, float(ky)


def solve_place(place, m=None):
    """For the live mapper: re-solve all pieces, write the map to <place>.png and the solved
    positions back into meta. Returns (x0, y0, W, H) or None."""
    d, meta, pieces = load(place)
    if m is not None:
        meta = m
    keep = [p for p in pieces if p["key"] in meta["cells"] and not has_menu(p["img"])
            and not meta["cells"][p["key"]].get("fill")]
    fills = [p for p in pieces if p["key"] in meta["cells"] and meta["cells"][p["key"]].get("fill")]
    if len(keep) < 2:
        return None
    r = keep[0]["img"].width / 2
    pairs = []
    kw = k_indoor() if meta.get("unit") == "world" else None
    for i in range(len(keep)):
        for j in range(i + 1, len(keep)):
            if meta.get("align") and len(keep) > 12 and \
                    math.hypot(keep[j]["x"] - keep[i]["x"], keep[j]["y"] - keep[i]["y"]) > 2.6 * r:
                continue                             # scale known: only neighbours can overlap
            ci, cj = meta["cells"][keep[i]["key"]], meta["cells"][keep[j]["key"]]
            if kw and "u" in ci and "u" in cj and math.hypot(cj["u"] - ci["u"], cj["v"] - ci["v"]) * kw > 1.8 * r:
                continue                             # dungeons: the logged spots say these two can't share much
            if meta["cells"][keep[i]["key"]].get("layer", 0) != meta["cells"][keep[j]["key"]].get("layer", 0):
                continue                             # different floors never overlap
            ck = (place, keep[i]["file"], keep[i]["t"], keep[j]["file"], keep[j]["t"])
            if ck not in _CACHE:                     # a piece is re-taken (new t) only when a better one comes
                _CACHE[ck] = match(keep[i], keep[j])
            mt = _CACHE[ck]
            if mt and mt[2] > 9:
                pairs.append((i, j, mt[0], mt[1], min(mt[2], 40) / 10))
    anchors = (meta.get("anchors") or [])
    if meta.get("unit") in ("coords", "world"):
        # outdoors the coordinates are the truth; picture matches only fine-tune. Repeating architecture
        # (city rings, rows of houses) makes the picture lock onto the wrong copy, so a match must agree
        # with what the coordinates predict.
        cells = meta["cells"]
        uv = {i: (cells[p["key"]].get("u"), cells[p["key"]].get("v")) for i, p in enumerate(keep)}
        # map % is % of width and % of height: east-west and north-south scales differ
        kxs, kys = [], []
        for i, j, dx, dy, wt in pairs:
            a, b = uv[i], uv[j]
            if a[0] is None or b[0] is None or wt < 1.5:
                continue
            du, dv = b[0] - a[0], b[1] - a[1]
            if abs(du) > 2 and abs(du) > 2 * abs(dv) and dx * du > 0:
                kxs.append(dx / du)
            if abs(dv) > 2 and abs(dv) > 2 * abs(du) and dy * dv > 0:
                kys.append(dy / dv)
        al0 = meta.get("align") or {}
        kx0, ky0 = al0.get("k"), al0.get("ky") or al0.get("k")
        def robust(xs):
            """The value most matches agree on (densest cluster within 4%), not the middle of all of them:
            wrong matches from repeating tiles can be nearly half of them."""
            best = max(xs, key=lambda x: sum(1 for y in xs if abs(y - x) < 0.04 * x))
            close = [y for y in xs if abs(y - best) < 0.04 * best]
            return float(np.median(close))
        if meta.get("unit") == "world":            # yards: same scale both ways
            kxs = []
            for i, j, dx, dy, wt in pairs:
                if uv[i][0] is None or uv[j][0] is None or wt < 1.2:
                    continue
                du, dv = uv[j][0] - uv[i][0], uv[j][1] - uv[i][1]
                dd, dm = math.hypot(du, dv), math.hypot(dx, dy)
                # a real match points the same way you walked; repeating tiles point anywhere
                if dd > 4 and dm > 3 and (dx * du + dy * dv) / (dd * dm) > 0.97:
                    kxs.append(dm / dd)
            kys = kxs
            # the minimap zoom is fixed (fully zoomed out, indoor view: k_indoor, 1.24 px/yd) and that wins.
            # Clean matches may only nudge it within 15%, and only when there are plenty of them (repeating
            # tiles fool matches, and fool them consistently, so a cluster of wrong ones can look convincing).
            # (It was 2.30 here from the old zoomed-in minimap: every dungeon drew rooms twice.)
            prior = k_indoor()
            near = [x for x in kxs if abs(x - prior) < 0.15 * prior]
            kx0 = ky0 = float(np.median(near)) if len(near) >= 8 else prior
            kxs = near
        elif len(kxs) >= 3:
            kx0 = robust(kxs)
        if meta.get("unit") != "world" and len(kys) >= 3:
            ky0 = robust(kys)
        if kx0 and ky0:
            pairs = [(i, j, dx, dy, wt) for i, j, dx, dy, wt in pairs
                     if uv[i][0] is not None and uv[j][0] is not None and
                     math.hypot(dx - kx0 * (uv[j][0] - uv[i][0]), dy - ky0 * (uv[j][1] - uv[i][1])) < 14]
        # every shot is an anchor at its own coordinates (strong), matches nudge them
        anchors = [{"piece": p["key"], "u": uv[i][0], "v": uv[i][1], "dx": 0.0, "dy": 0.0, "shot": True}
                   for i, p in enumerate(keep) if uv[i][0] is not None]
    al = None
    used_a = []
    coords = meta.get("unit") == "coords"              # map-% units outdoors, yards in dungeons
    if meta.get("unit") == "world" and anchors:
        # dungeons: scale held at the measured value (or what clean matches agree on); the logged spots
        # place every shot; picture matches only fine-tune
        X, Y, k, cx, cy, used, used_a, ky = solve_joint(keep, pairs, anchors, square=True, k_fixed=(kx0, ky0))
        al = {"k": k, "ky": ky, "cx": cx, "cy": cy, "source": "log", "anchors": len(used_a), "of": len(anchors),
              "k_from": len(kxs) if len(kxs) >= 3 else "measured"}
    elif len(anchors) >= 6 and len({a["piece"] for a in anchors}) >= 1:
        X, Y, k, cx, cy, used, used_a, ky = solve_joint(keep, pairs, anchors, square=not coords,
                                                         k_fixed=(kx0, ky0) if coords and kx0 and ky0 else None)
        spread = max((math.hypot(a["u"] - b["u"], a["v"] - b["v"]) for a in used_a[:60] for b in used_a[:60]), default=0)
        if (1 < k < 200 if coords else 0.3 < k < 3) and len(used_a) >= 6 and spread > (2 if coords else 20):
            al = {"k": k, "ky": ky, "cx": cx, "cy": cy, "source": "log", "anchors": len(used_a), "of": len(anchors)}
    else:
        X, Y, used = solve(keep, pairs)
    if al:
        meta["align"] = al
    # pieces nothing agrees with stay where tracking put them
    # (a shot placed by its own logged spot is placed: left at its old spot it stayed behind whenever the
    #  scale or offset of the map moved)
    by_anchor = {a["piece"] for a in used_a}
    linked = {0} | {i for i, j, *_ in used} | {j for i, j, *_ in used} | \
        {q for q, p in enumerate(keep) if p["key"] in by_anchor}
    for k, p in enumerate(keep):
        if k in linked:
            meta["cells"][p["key"]]["px"], meta["cells"][p["key"]]["py"] = round(float(X[k]), 1), round(float(Y[k]), 1)
        else:
            X[k], Y[k] = p["x"], p["y"]
    # bottom layer (walking): re-placed from its live-estimated world spot with the current scale/offset
    al2 = meta.get("align") or {}
    FX, FY = [], []
    for p in fills:
        c = meta["cells"][p["key"]]
        if al2.get("k"):
            c["px"] = round(al2["k"] * c["u"] + al2.get("cx", 0.0), 1)
            c["py"] = round((al2.get("ky") or al2["k"]) * c["v"] + al2.get("cy", 0.0), 1)
        p["off"] = 1000.0                            # compose draws high 'off' first = underneath
        FX.append(c["px"]); FY.append(c["py"])
    # every floor on the same canvas (same origin/size), so switching floors keeps everything lined up
    r2 = keep[0]["img"].width / 2
    AX, AY = list(X) + FX, list(Y) + FY
    x0, y0 = float(min(AX)) - r2, float(min(AY)) - r2
    W, H = int(max(AX) + r2 - x0) + 1, int(max(AY) + r2 - y0) + 1
    allp = keep + fills
    floors = sorted({meta["cells"][p["key"]].get("layer", 0) for p in allp})
    for L in floors:
        idx = [q for q, p in enumerate(allp) if meta["cells"][p["key"]].get("layer", 0) == L]
        compose([allp[q] for q in idx], [AX[q] for q in idx], [AY[q] for q in idx],
                os.path.join(d, safe(place) + (f"_L{L}" if L else "") + ".png"), bounds=(x0, y0, W, H))
    return round(x0, 1), round(y0, 1), W, H


def main(place):
    d, m, pieces = load(place)
    pieces = [p for p in pieces if not has_menu(p["img"])]
    r = pieces[0]["img"].width / 2
    pairs = []
    for i in range(len(pieces)):
        for j in range(i + 1, len(pieces)):
            est = (pieces[j]["x"] - pieces[i]["x"], pieces[j]["y"] - pieces[i]["y"])
            if math.hypot(*est) > 2.6 * r:
                continue                                 # too far apart to overlap (even with drift)
            mt = match(pieces[i], pieces[j])
            if mt and mt[2] > 9:
                pairs.append((i, j, mt[0], mt[1], min(mt[2], 40) / 10))
    X, Y, used = solve(pieces, pairs)
    out = os.path.join(d, safe(place) + "_restitched.png")
    origin, size = compose(pieces, X, Y, out)
    print(f"{len(pieces)} pieces, {len(pairs)} matches, {len(used)} agree -> {out} {size}")
    return out


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "The Hall of Thanes")
