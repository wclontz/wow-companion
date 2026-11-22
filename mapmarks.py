"""The game's own marks on a minimap shot: quest ! and ? icons, and the pale-blue rings round quest areas.

They are baked into the pictures the world map is drawn from, and they come and go with your quest log, so two
shots of one spot often disagree: one has the mark, one is plain ground. The map is drawn from the plain one.

Colour alone can't say what a ring is (outdoors it is a thin see-through line; water, snow and Ironforge's pale
stone are as blue). So nothing is decided from one picture: every shot is boiled down to a coarse grid, 8 px a
block, holding
  bl   how blue the bluest bit of the block is (blue - red, smoothed), -127 where the shot has no picture
  ye   how many pure-yellow pixels (the icons' yellow) it has
and for one square of the map the shots that reach it are compared (`order`): a shot clearly bluer or yellower
there than the plainest one has a mark on it, and goes under the others. Where every shot agrees - water, or a
ring that was on all of them - nothing changes; the ring goes the day a shot without it is taken.

The grids are kept in `marks.bin` beside the map's meta.json (append-only; made again from the clips if lost).
"""
import os
import struct

import numpy as np
from PIL import Image, ImageFilter

B = 8                    # px a block
NONE = -127              # bl of a block with no picture in it
BLUE_TOL = 16            # this much bluer than the plainest shot of the square = a mark
YELLOW_TOL = 3           # this many more icon-yellow pixels = a mark
LOOK = 24                # nearest shots compared for one square
KEEP_BLUE, KEEP_YELLOW = 20, 6    # a new shot this much plainer than every kept one of its square is kept too
_HEAD = struct.Struct("<IdH")     # clip id, its time, picture width


def _yellow(a):
    """The icons' yellow, on an int16 RGBA array."""
    return (a[..., 0] > 215) & (a[..., 1] > 160) & (a[..., 2] < 50) & (a[..., 3] > 0)


def coarse(img):
    """(bl int8 n x n, ye uint8 n x n) of a shot."""
    a = np.asarray(img.convert("RGBA")).astype(np.int16)
    n = min(a.shape[0], a.shape[1]) // B
    a = a[:n * B, :n * B]
    bl = np.clip(a[..., 2] - a[..., 0], -126, 127)
    bl[a[..., 3] == 0] = NONE
    p = np.pad(bl, 1, mode="edge")
    sm = sum(p[dy:dy + n * B, dx:dx + n * B] for dy in range(3) for dx in range(3)) // 9
    sm[a[..., 3] == 0] = NONE
    blm = sm.reshape(n, B, n, B).max(axis=(1, 3)).astype(np.int8)
    ye = np.minimum(_yellow(a).reshape(n, B, n, B).sum(axis=(1, 3)), 255).astype(np.uint8)
    return blm, ye


def square(mk, x0, y0, size):
    """(bluest, yellow pixels) of the part of a shot from (x0, y0), `size` px wide, in the shot's own px."""
    bl, ye = mk
    n = bl.shape[0]
    i0, i1 = max(0, int(x0 // B)), min(n, int((x0 + size - 1) // B) + 1)
    j0, j1 = max(0, int(y0 // B)), min(n, int((y0 + size - 1) // B) + 1)
    if i0 >= i1 or j0 >= j1:
        return NONE, 0
    return int(bl[j0:j1, i0:i1].max()), int(ye[j0:j1, i0:i1].sum())


def order(scores):
    """scores: [(bluest, yellow)] of the shots of one square, nearest first -> their indexes, the plain ones
    first (still nearest first), the marked ones after them."""
    seen = [s[0] for s in scores if s[0] > NONE + 5]
    if not seen:
        return list(range(len(scores)))
    blue0, yel0 = min(seen), min(s[1] for s in scores)
    plain = [k for k, s in enumerate(scores) if s[0] <= blue0 + BLUE_TOL and s[1] <= yel0 + YELLOW_TOL]
    was = set(plain)
    return plain + [k for k in range(len(scores)) if k not in was]


def middle(mk):
    """(bluest, yellow pixels) of the middle of a shot - the part it is the nearest shot for."""
    n = mk[0].shape[0]
    h = max(1, min(n // 2, 6))
    return square(mk, (n // 2 - h) * B, (n // 2 - h) * B, 2 * h * B)


def plainer(new, kept):
    """Is a new shot's middle clearly plainer than every kept shot's of the same square (their `middle`s)?"""
    if not kept or new[0] <= NONE + 5:
        return False
    blues = [k[0] for k in kept if k[0] > NONE + 5]
    return bool(blues and new[0] < min(blues) - KEEP_BLUE) or new[1] + KEEP_YELLOW <= min(k[1] for k in kept)


def icon_mask(a):
    """Where the yellow icons are on an RGBA array (their dark edge included), or None: for painting them over."""
    y = _yellow(a.astype(np.int16))
    n = int(y.sum())
    if n < 6 or n > 1500:                 # nothing, or a whole yellow something that is no icon
        return None
    return np.asarray(Image.fromarray((y * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(7))) > 0


class Marks:
    """The coarse grids of one map's clips, by clip id."""

    def __init__(self, folder):
        self.path = os.path.join(folder, "marks.bin")
        self.by = {}                       # id -> (t, width, bl, ye)
        try:
            with open(self.path, "rb") as f:
                data = f.read()
        except OSError:
            data = b""
        at = 0
        while at + _HEAD.size <= len(data):
            cid, t, w = _HEAD.unpack_from(data, at)
            n = w // B
            end = at + _HEAD.size + 2 * n * n
            if n < 1 or end > len(data):
                break                      # a record cut short (the program stopped while writing): made again
            p = at + _HEAD.size
            self.by[cid] = (t, w, np.frombuffer(data, np.int8, n * n, p).reshape(n, n),
                            np.frombuffer(data, np.uint8, n * n, p + n * n).reshape(n, n))
            at = end
        self.good = at                     # (anything after this is cut off at the next write)
        self.new = []

    def get(self, c, img=None, load=None):
        """(width, (bl, ye)) of clip c; made from its picture (img, or load() -> picture or None) when new."""
        r = self.by.get(c["id"])
        if r is not None and abs(r[0] - (c.get("t") or 0)) < 0.2:
            return r[1], (r[2], r[3])
        if img is None:
            img = load() if load else None
        if img is None:
            return None
        bl, ye = coarse(img)
        self.by[c["id"]] = (float(c.get("t") or 0), img.width, bl, ye)
        self.new.append(c["id"])
        return img.width, (bl, ye)

    def flush(self):
        if not self.new:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "r+b" if os.path.exists(self.path) else "wb") as f:
            f.seek(self.good)
            f.truncate()
            for cid in self.new:
                t, w, bl, ye = self.by[cid]
                f.write(_HEAD.pack(cid, t, w) + bl.tobytes() + ye.tobytes())
            self.good = f.tell()
        self.new = []
