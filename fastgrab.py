"""Fast screen grab of just a rectangle (Windows BitBlt). PIL's ImageGrab.grab(bbox, all_screens=True)
copies EVERY monitor and then crops (~75 ms here with several screens); this copies only the box (~1-3 ms).

Every screen read in the companion goes through here, and none through PIL's ImageGrab: when a grab FAILS
(lock screen, a Windows permission prompt, the display asleep) Pillow 12.1 gives back its two DCs but not the
bitmap. At 4 failed grabs a second that used up all 10,000 of the process's GDI handles in about 40 minutes,
and from then on no grab could ever work again until a restart (2026-11-18: every screen read dead from
21:01, so a dungeon was not seen). Here the handles are freed whether the grab works or not."""
import ctypes
from ctypes import wintypes

from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000

# handles are pointer-sized: say so, or ctypes cuts them to 32 bits
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                            ctypes.c_void_p, wintypes.UINT]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
user32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
user32.GetGuiResources.restype = wintypes.DWORD


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def grab(bbox):
    """bbox = (x0, y0, x1, y1) in screen pixels -> RGB PIL image (None on failure)."""
    x0, y0, x1, y1 = (int(v) for v in bbox)
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return None
    hdc = user32.GetDC(None)
    if not hdc:
        return None
    mdc = bmp = old = None
    try:
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h) if mdc else None
        if not mdc or not bmp:
            return None
        old = gdi32.SelectObject(mdc, bmp)
        if not gdi32.BitBlt(mdc, 0, 0, w, h, hdc, x0, y0, SRCCOPY | CAPTUREBLT):
            return None
        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth, bi.biHeight, bi.biPlanes, bi.biBitCount = w, -h, 1, 32
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0):
            return None
        return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    finally:
        if old:
            gdi32.SelectObject(mdc, old)
        if bmp:
            gdi32.DeleteObject(bmp)
        if mdc:
            gdi32.DeleteDC(mdc)
        user32.ReleaseDC(None, hdc)


def screen(bbox):
    """As grab(), but a failure raises OSError (what PIL's ImageGrab.grab did, so callers keep their handling)."""
    im = grab(bbox)
    if im is None:
        raise OSError("screen grab failed")
    return im


def gdi_count():
    """GDI handles this process holds (Windows stops handing them out at 10,000)."""
    try:
        return int(user32.GetGuiResources(kernel32.GetCurrentProcess(), 0))
    except Exception:
        return None
