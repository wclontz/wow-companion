"""Where things are on THIS PC: the companion's own settings, the game's folder, this PC's network address.

settings.json (next to this file, optional; every key optional):
  {"wow_dir": "D:\\Games\\World of Warcraft",   the folder that holds _forever_ (or the _forever_ folder itself)
   "port": 8765,
   "lan": true}                                  false = only this PC can open the pages

The environment still wins where it did before: WOW_LOG_DIR (the Logs folder itself), WOW_COMPANION_PORT.
WOW_DIR does what "wow_dir" does."""
import glob
import json
import os
import socket
import string
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(HERE, "settings.json")
FLAVORS = ("_forever_", "_classic_beta_", "_classic_era_", "_classic_")
DEFAULT_ROOT = r"C:\Program Files (x86)\World of Warcraft"
NO_WINDOW = 0x08000000


def settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_setting(key, value):
    d = settings()
    d[key] = value
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, SETTINGS)


def _as_root(p):
    """A path someone gave (the game folder, a flavor folder, or a file in one) -> the folder holding the flavors."""
    p = os.path.normpath(os.path.expandvars(str(p).strip().strip('"')))
    if os.path.isfile(p):
        p = os.path.dirname(p)
    for _ in range(3):
        if os.path.basename(p).lower() in FLAVORS:
            return os.path.dirname(p)
        if any(os.path.isdir(os.path.join(p, f)) for f in FLAVORS):
            return p
        up = os.path.dirname(p)
        if up == p:
            break
        if os.path.basename(up).lower() in FLAVORS:
            return os.path.dirname(up)
        break
    return p


def has_game(root):
    return bool(root) and any(os.path.isdir(os.path.join(root, f)) for f in FLAVORS)


def _from_process():
    """The folder of a running Wow.exe (one PowerShell call: only made when the usual places have nothing)."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-Process Wow,WowB,WowClassic -ErrorAction SilentlyContinue | "
                              "Select-Object -First 1).Path"],
                             capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW).stdout.strip()
        return _as_root(out) if out else None
    except Exception:
        return None


def _from_registry():
    try:
        import winreg
    except ImportError:
        return []
    out = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key in (r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft",
                    r"SOFTWARE\Blizzard Entertainment\World of Warcraft"):
            try:
                with winreg.OpenKey(hive, key) as k:
                    out.append(_as_root(winreg.QueryValueEx(k, "InstallPath")[0]))
            except OSError:
                pass
    return out


def _usual_places():
    out = [DEFAULT_ROOT]
    for letter in string.ascii_uppercase:
        drive = letter + ":\\"
        if not os.path.exists(drive):
            continue
        for sub in ("Program Files (x86)", "Program Files", "", "Games", "Blizzard", "Battle.net"):
            out.append(os.path.join(drive, sub, "World of Warcraft"))
    return out


def wow_root(ask_process=True):
    """The folder that holds the game's _forever_ folder, or None when it can't be found."""
    for given in (os.environ.get("WOW_DIR"), settings().get("wow_dir")):
        if given and has_game(_as_root(given)):
            return _as_root(given)
    if has_game(DEFAULT_ROOT):
        return DEFAULT_ROOT
    for p in _from_registry() + _usual_places():
        if has_game(p):
            return p
    p = _from_process() if ask_process else None
    return p if has_game(p) else None


def flavor_dir(root=None):
    """The install played last: the flavor folder whose Logs holds the newest combat / chat log (WoW Forever moved
    from _classic_beta_ to _forever_ on 2026-11-04; a future move should not strand the companion again)."""
    root = root or wow_root()
    if not root:
        return None
    best, best_t = None, -1.0
    for flavor in FLAVORS:
        d = os.path.join(root, flavor)
        if not os.path.isdir(d):
            continue
        fs = (glob.glob(os.path.join(d, "Logs", "WoWCombatLog*.txt"))
              + glob.glob(os.path.join(d, "Logs", "WoWChatLog*.txt")))
        t = max(map(os.path.getmtime, fs)) if fs else 0.0
        if best is None or t > best_t:
            best, best_t = d, t
    return best


def log_dir():
    """The game's Logs folder (it may not exist yet on an install that has never logged)."""
    if os.environ.get("WOW_LOG_DIR"):
        return os.environ["WOW_LOG_DIR"]
    return os.path.join(flavor_dir() or os.path.join(DEFAULT_ROOT, "_forever_"), "Logs")


def port():
    try:
        return int(os.environ.get("WOW_COMPANION_PORT") or settings().get("port") or 8765)
    except (TypeError, ValueError):
        return 8765


def lan_on():
    return settings().get("lan", True) is not False


def lan_ips():
    """This PC's addresses on the home network (private ranges only), the one traffic leaves by first."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))             # nothing is sent: it only picks the outgoing address
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if a[4][0] not in ips:
                ips.append(a[4][0])
    except OSError:
        pass

    def private(ip):
        p = [int(x) for x in ip.split(".")]
        return p[0] == 10 or (p[0] == 172 and 16 <= p[1] <= 31) or (p[0] == 192 and p[1] == 168)
    return [ip for ip in ips if private(ip)]


if __name__ == "__main__":
    print("game folder:", wow_root())
    print("played last:", flavor_dir())
    print("logs       :", log_dir())
    print("port       :", port(), "| other devices:", [f"http://{ip}:{port()}/hud" for ip in lan_ips()] if lan_on() else "off")
