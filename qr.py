"""A small QR code maker for the tablet address (no package needed, nothing fetched from the internet).

Byte mode, error correction level L, versions 1-5 (up to 106 bytes: an address is about 30). Those versions have
one block of codewords each, which keeps this short. matrix(text) -> rows of True (dark) / False; svg(text) -> a picture.
Checked module for module against the `qrcode` package (test_qr.py holds one known-good code)."""

# version: (data codewords, error-correction codewords)   -- level L
SIZES = {1: (19, 7), 2: (34, 10), 3: (55, 15), 4: (80, 20), 5: (108, 26)}

_EXP, _LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i], _LOG[_x] = _x, _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    return 0 if not a or not b else _EXP[_LOG[a] + _LOG[b]]


def _ec(data, n):
    """The n Reed-Solomon codewords for these data codewords."""
    gen = [1]
    for i in range(n):                                   # times (x - a^i)
        gen = [g ^ _mul(h, _EXP[i]) for g, h in zip(gen + [0], [0] + gen)]
    rem = [0] * n
    for d in data:
        f = d ^ rem[0]
        rem = rem[1:] + [0]
        for j in range(n):
            rem[j] ^= _mul(gen[j + 1], f)
    return rem


def _codewords(raw, version):
    n_data, n_ec = SIZES[version]
    bits = [0, 1, 0, 0] + [(len(raw) >> i) & 1 for i in range(7, -1, -1)]
    for b in raw:
        bits += [(b >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, n_data * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    data += [pad[i % 2] for i in range(n_data - len(data))]
    return data + _ec(data, n_ec)


_MASKS = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
          lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
          lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
          lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0]


def _format_bits(mask):
    d = (1 << 3) | mask                                  # level L = 01, then the mask
    r = d
    for _ in range(10):
        r = (r << 1) ^ ((r >> 9) * 0x537)
    return ((d << 10) | r) ^ 0x5412


def _penalty(m):
    n = len(m)
    p = 0
    lines = [row for row in m] + [[m[y][x] for y in range(n)] for x in range(n)]
    for line in lines:                                   # runs of 5+ the same
        run = 1
        for i in range(1, n):
            if line[i] == line[i - 1]:
                run += 1
            else:
                p += run - 2 if run >= 5 else 0
                run = 1
        p += run - 2 if run >= 5 else 0
    for y in range(n - 1):                               # 2x2 blocks
        for x in range(n - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                p += 3
    a = [True, False, True, True, True, False, True, False, False, False, False]
    b = a[::-1]
    for line in lines:                                   # what looks like a finder pattern
        for i in range(n - 10):
            if line[i:i + 11] == a or line[i:i + 11] == b:
                p += 40
    dark = sum(map(sum, m))                              # balance of dark and light
    p += 10 * (abs(dark * 20 - n * n * 10) // (n * n))
    return p


def matrix(text, mask=None):
    """The code for this text: a list of rows, True = dark. ValueError when it is too long."""
    raw = text.encode("utf-8")
    version = next((v for v in sorted(SIZES) if len(raw) + 2 <= SIZES[v][0]), None)
    if version is None:
        raise ValueError("too long for this small QR maker")
    n = 17 + 4 * version
    m = [[False] * n for _ in range(n)]
    fixed = [[False] * n for _ in range(n)]

    def put(x, y, dark):
        if 0 <= x < n and 0 <= y < n:
            m[y][x], fixed[y][x] = bool(dark), True

    for i in range(n):                                   # timing lines
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):      # the three corner squares, with their light edge
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                d = max(abs(dx), abs(dy))
                put(cx + dx, cy + dy, d != 2 and d != 4)
    if version >= 2:                                     # the small square near the fourth corner
        c = n - 7
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                put(c + dx, c + dy, max(abs(dx), abs(dy)) != 1)

    def put_format(mask_no):
        f = _format_bits(mask_no)
        bit = lambda i: (f >> i) & 1
        for i in range(6):
            put(8, i, bit(i))
        put(8, 7, bit(6))
        put(8, 8, bit(7))
        put(7, 8, bit(8))
        for i in range(9, 15):
            put(14 - i, 8, bit(i))
        for i in range(8):
            put(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            put(8, n - 15 + i, bit(i))
        put(8, n - 8, True)

    put_format(0)                                        # keeps those places out of the data's way
    words = _codewords(raw, version)
    bits = [(w >> i) & 1 for w in words for i in range(7, -1, -1)]
    k = 0
    right = n - 1
    while right >= 1:                                    # two columns at a time, snaking up and down from the right
        if right == 6:
            right = 5
        for vert in range(n):
            for j in range(2):
                x = right - j
                up = ((right + 1) & 2) == 0
                y = n - 1 - vert if up else vert
                if not fixed[y][x] and k < len(bits):
                    m[y][x] = bool(bits[k])
                    k += 1
        right -= 2

    def masked(mask_no):
        f = _MASKS[mask_no]
        out = [[m[y][x] ^ (not fixed[y][x] and f(x, y)) for x in range(n)] for y in range(n)]
        keep = m[:]
        try:
            m[:] = out
            put_format(mask_no)
            return [row[:] for row in m]
        finally:
            m[:] = keep

    if mask is not None:
        return masked(mask)
    return min((masked(i) for i in range(8)), key=_penalty)


def svg(text, quiet=4):
    """The code as an SVG picture: dark on white with the quiet edge a camera needs."""
    m = matrix(text)
    n = len(m) + 2 * quiet
    d = "".join(f"M{x + quiet},{y + quiet}h1v1h-1z" for y, row in enumerate(m) for x, dark in enumerate(row) if dark)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{d}" fill="#000"/></svg>')


if __name__ == "__main__":
    import sys
    for row in matrix(sys.argv[1] if len(sys.argv) > 1 else "http://192.168.1.50:8765/hud"):
        print("".join("##" if c else "  " for c in row))
