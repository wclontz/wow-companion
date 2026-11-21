"""One-time setup on a new PC (Setup.bat runs this). It:
  1. installs the two Python packages the companion needs (numpy, Pillow),
  2. finds the game's folder (asks when it can't) and remembers it in settings.json,
  3. copies the Companion Beacon add-on into the game's Interface\\AddOns folder,
  4. offers to start the companion by itself whenever the game runs,
  5. when Claude is installed, asks whether clicking a quest may have Claude look it up (on your own Claude plan).
Safe to run again (after an update: it copies the newer add-on over the old one).

  python setup_companion.py                       asks its questions
  python setup_companion.py --wow "D:\\Games\\World of Warcraft" --autostart      no questions (or --no-autostart)
  ... --claude / --no-claude                      quest look-ups by Claude on or off (no flag: left as it is)
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
ADDON = "CompanionBeacon"


def arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv and sys.argv.index(name) + 1 < len(sys.argv) else None


QUIET = any(f in sys.argv for f in ("--wow", "--autostart", "--no-autostart", "--claude", "--no-claude"))


def ask(q):
    if QUIET:
        return ""
    try:
        return input(q).strip()
    except EOFError:
        return ""


def packages():
    print("1. Python packages")
    try:
        import numpy, PIL                                  # noqa: F401,E401
        print("   numpy and Pillow are already here.")
        return True
    except ImportError:
        pass
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--user", "-r", "requirements.txt"])
    if r.returncode:
        print("   !! pip could not install them. Try by hand:  python -m pip install numpy pillow")
        return False
    print("   installed.")
    return True


def game():
    import wowpath
    print("2. The game")
    given = arg("--wow")
    root = wowpath._as_root(given) if given else wowpath.wow_root()
    while not wowpath.has_game(root):
        if given or QUIET:
            print(f"   !! no _forever_ folder in {root!r}.")
            return None
        print("   Can't find World of Warcraft by itself (it helps to have the game running).")
        given_now = ask("   Paste the folder the game is in (the one with _forever_ in it), or press Enter to skip: ")
        if not given_now:
            return None
        root = wowpath._as_root(given_now)
    if root != wowpath.wow_root(ask_process=False):
        wowpath.save_setting("wow_dir", root)              # not one of the usual places: remember it
    flavor = wowpath.flavor_dir(root)
    print("   found:", flavor)
    return flavor


def addon(flavor):
    print("3. The add-on (Companion Beacon)")
    src = os.path.join(HERE, "addon", ADDON)
    dst = os.path.join(flavor, "Interface", "AddOns", ADDON)
    try:
        os.makedirs(dst, exist_ok=True)
        for f in (ADDON + ".lua", ADDON + ".toc"):
            shutil.copyfile(os.path.join(src, f), os.path.join(dst, f))
    except OSError as e:
        print(f"   !! could not copy it ({e}).")
        print(f"   Copy the folder  {src}\n   into             {os.path.dirname(dst)}  by hand.")
        return False
    print("   copied to", dst)
    return True


def autostart():
    print("4. Start by itself")
    want = "--autostart" in sys.argv
    if not want and "--no-autostart" not in sys.argv:
        want = ask("   Start the companion by itself whenever the game runs? [y/N] ").lower().startswith("y")
    if not want:
        print("   No: start it with Start_Companion.bat when you play.")
        return
    r = subprocess.run([sys.executable, os.path.join(HERE, "autostart.pyw"), "--install"])
    if r.returncode:
        print("   !! could not add it to Windows' Startup folder. Start_Companion.bat still works.")
    else:
        print("   It starts with Windows from the next sign-in. To undo: python autostart.pyw --uninstall")


def claude():
    import wowpath
    try:
        from quests import find_claude_cli
        here = bool(find_claude_cli())
    except Exception:
        here = False
    print("5. Quest look-ups by Claude")
    if not here:
        print("   Claude is not installed on this PC: quests get a Wowhead link only. Nothing to set.")
        return
    now = wowpath.settings().get("claude") is True
    if "--claude" in sys.argv or "--no-claude" in sys.argv:
        want = "--claude" in sys.argv
    elif QUIET:
        want = now
    else:
        print("   Claude is installed here. Clicking a quest on the HUD can have it look the quest up on the web")
        print("   and answer in a few lines. Each look-up runs your own Claude, on your plan.")
        a = ask(f"   Switch that on? [y/N{', Enter keeps it on' if now else ''}] ").lower()
        want = now if not a else a.startswith("y")
    if want != now:
        wowpath.save_setting("claude", want)
    print("   On." if want else '   Off. ("claude": true in settings.json switches it on later.)')


def main():
    print("WoW Companion setup\n")
    if sys.version_info < (3, 10):
        print("!! This needs Python 3.10 or newer; this is", sys.version.split()[0])
        return 1
    ok = packages()
    flavor = game()
    if flavor:
        ok = addon(flavor) and ok
    else:
        ok = False
        print("   Skipped the add-on. Run Setup.bat again once the game is installed, or with the game running.")
    autostart()
    claude()
    import wowpath
    print("\nWhat is left, in the game:")
    print("  - Restart the game. On the character screen click AddOns and tick Companion Beacon")
    print('    (tick "Load out of date AddOns" too if it is greyed out).')
    print("  - Options > Network > Advanced Combat Logging: on. The add-on switches the logs on at every login.")
    print("  - A small block of coloured squares in the bottom-right corner of the game is the add-on working.")
    print("    Keep that corner uncovered, and play in a window or borderless window (not exclusive fullscreen).")
    print("\nThen double-click Start_Companion.bat. The HUD opens at http://127.0.0.1:%d/hud" % wowpath.port())
    for ip in wowpath.lan_ips() if wowpath.lan_on() else []:
        print("On a tablet or phone on the same Wi-Fi: http://%s:%d/hud" % (ip, wowpath.port()))
        print("  (the first time, Windows asks whether Python may be reached on the network: allow it for private networks)")
    print("\nSetup finished." if ok else "\nSetup finished with something left to do: see the !! lines above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
