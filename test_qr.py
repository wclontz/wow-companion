"""The QR maker: one known-good code (checked module for module against the `qrcode` package on 2026-11-21,
9 texts x 8 masks), and the shapes every code must have."""
import hashlib

import qr

URL = "http://192.168.1.50:8765/hud"
m = qr.matrix(URL)
assert len(m) == 25 and all(len(r) == 25 for r in m), "version 2 is 25 x 25"
h = hashlib.sha256("".join("1" if c else "0" for r in m for c in r).encode()).hexdigest()
assert h == "c923592b1886f10851133b5a1c0ca9c587ae0ec91efd02473ff5b451647bf14d", h
for text, size in (("a", 21), ("x" * 18, 25), ("x" * 33, 29), ("x" * 54, 33), ("y" * 106, 37)):
    mm = qr.matrix(text)
    assert len(mm) == size, (text[:5], len(mm))
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):                 # the three corner squares
        assert mm[cy][cx] and not mm[cy - 2][cx] and mm[cy - 3][cx], "corner square"
    assert all(mm[6][i] == (i % 2 == 0) for i in range(8, size - 8)), "timing line"
    assert mm[size - 8][8], "the dark module"
try:
    qr.matrix("z" * 107)
    raise AssertionError("107 bytes should be too long")
except ValueError:
    pass
s = qr.svg(URL)
assert s.startswith("<svg") and 'viewBox="0 0 33 33"' in s, "25 modules + 4 quiet on each side"
print("ALL PASS")
