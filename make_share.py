"""Builds the package to give away: share/WoWCompanion_<date>.zip. The same list is what goes to the public
GitHub repository (.gitignore names the same files).

Only what is listed here goes in: the program, the add-on, and a few small lists of names and measurements.
Nothing of the owner's play is included: no maps, logs, state, fights, markers, backups or hand backups.
Not included either, because it is public: the cached game data (mobdb.json, itemcache.json, icons/,
label_images/). It is Blizzard's and Wowhead's material, and every companion fills its own as its owner plays.
The package is refused if a character name from companion_state.json or the Windows user name turns up in any
file meant to go in.

  python make_share.py            build it
  python make_share.py --list     only show what would go in
  python make_share.py --git      check what git tracks the same way (before a push to GitHub)
"""
import fnmatch
import json
import os
import re
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = "WoWCompanion"
FILES = ["*.py", "autostart.pyw", "*.html", "*.bat", "ocr.ps1", "requirements.txt", "README.md", "SETUP_FOR_CLAUDE.md",
         "LICENSE",
         # the same for everyone: game data looked up or measured, and what the readers have learned
         "dungeons.json", "dungeon_rooms.json", "subzones.json", "zone_bounds.json", "timerfont.json", "map_ids.json",
         "label_fixes.json", "screen_layout.json"]
SKIP = ["test_*.py", "make_share.py", "import_old_fights.py"]
DIRS = {"addon/CompanionBeacon": ["CompanionBeacon.lua", "CompanionBeacon.toc"]}


def chosen():
    out = []
    for f in sorted(os.listdir(HERE)):
        if (os.path.isfile(os.path.join(HERE, f)) and any(fnmatch.fnmatch(f, p) for p in FILES)
                and not any(fnmatch.fnmatch(f, p) for p in SKIP)):
            out.append(f)
    for d, pats in DIRS.items():
        for f in sorted(os.listdir(os.path.join(HERE, d))):
            if any(fnmatch.fnmatch(f, p) for p in pats):
                out.append(f"{d}/{f}")
    return out


def names():
    """The owner's character names (the keys of companion_state.json that hold a character)."""
    try:
        with open(os.path.join(HERE, "companion_state.json"), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return []
    return [k for k, v in st.items() if isinstance(v, dict) and k[:1].isupper() and len(k) >= 4]


def leaks(files):
    user = os.path.basename(os.path.expanduser("~")).lower()
    words = [n.lower() for n in names()] + ([user] if len(user) >= 4 else [])
    found = []
    for f in files:
        if f.lower().endswith((".png", ".jpg")):
            continue
        text = open(os.path.join(HERE, f), encoding="utf-8", errors="replace").read().lower()
        text = re.sub(r"github\.com/[\w.-]+", "github.com/", text)     # the repository's own address is no leak
        found += [(f, w) for w in words if w in text]
    return found


def tracked():
    import subprocess
    out = subprocess.run(["git", "ls-files"], cwd=HERE, capture_output=True, text=True).stdout
    return [f for f in out.splitlines() if f]


def main():
    if "--git" in sys.argv:                   # the same check on what git tracks (run before a push)
        files = tracked()
        extra = sorted(set(files) - set(chosen()) - {".gitignore", "make_share.py", "import_old_fights.py"}
                       - {f for f in files if fnmatch.fnmatch(f, "test_*.py")})
        bad = leaks(files)
        for f, w in bad:
            print(f"PERSONAL: {f} contains {w!r}")
        for f in extra:
            print(f"NOT ON THE SHARE LIST: {f}")
        print(len(files), "tracked files;", "do not push" if bad or extra else "nothing personal found, all on the list")
        return 1 if bad or extra else 0
    files = chosen()
    if "--list" in sys.argv:
        print("\n".join(files))
    bad = leaks(files)
    if bad:
        print("NOT built: something personal is in a file that would be shared:")
        for f, w in bad:
            print(f"  {f}: contains {w!r}")
        return 1
    if "--list" in sys.argv:
        print(len(files), "files; nothing personal found in them")
        return 0
    os.makedirs(os.path.join(HERE, "share"), exist_ok=True)
    out = os.path.join(HERE, "share", f"WoWCompanion_{time.strftime('%Y-%m-%d')}.zip")
    tmp = out + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(os.path.join(HERE, f), f"{TOP}/{f}")
    os.replace(tmp, out)
    print(f"{out}\n{len(files)} files, {os.path.getsize(out) / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
