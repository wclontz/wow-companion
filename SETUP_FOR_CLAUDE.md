# Setting up WoW Companion: notes for Claude

The person you are helping was given this folder by a friend and wants it working. `README.md` is their guide;
this file is yours. Everything runs on their own Windows PC. Nothing here needs an account, a key or the internet
beyond `pip`.

## What it is

A local Python web server (`companion.py`, port 8765) that reads World of Warcraft's log files and the game
window's pixels, and serves dashboard pages to a browser. A small add-on (`addon/CompanionBeacon`) draws a strip
of coloured squares in the game's bottom-right corner that the server reads. It is read-only by design: it never
sends input to the game. Keep it that way.

## Do this

1. Check Python: `python --version` must be 3.10 or newer. If it is missing, ask the person to install it from
   python.org with "Add python.exe to PATH" ticked. Don't install it for them without asking.
2. Run the setup without questions:
   `python setup_companion.py --no-autostart`
   It installs numpy and Pillow (`pip install --user`), finds the game, and copies the add-on's two files into
   `<game>\_forever_\Interface\AddOns\CompanionBeacon`. Exit code 0 = all done; otherwise read its `!!` lines.
   - Game not found: ask where World of Warcraft is installed (or have them start the game and run it again),
     then `python setup_companion.py --wow "<that folder>" --no-autostart`. `python wowpath.py` prints what is found.
   - Ask whether they want it to start by itself whenever the game runs. Only on a yes:
     `python autostart.pyw --install` (a shortcut in their Startup folder; `--uninstall` removes it).
   - Quest look-ups: the companion can run `claude -p` (web search only) when they click a quest on the HUD, to
     answer where to go and what to do. That uses their Claude plan, so it is off by default. Ask them; on a yes
     add `--claude` to the setup command; they can switch it themselves with the "Claude: on / off" button on the
     HUD's Quests card (`"claude"` in `settings.json`). `quests.py`
     `find_claude_cli` is how it finds the program; `"claude_path"` in `settings.json` names it outright.
     It must be signed in by itself (`claude auth status` says; the desktop app's sign-in does not count):
     `claude auth login` is theirs to run, not yours.
3. Tell them what only they can do, in the game:
   - restart the game; on the character screen click AddOns and tick Companion Beacon ("Load out of date
     AddOns" too if it is greyed out);
   - Options > Network > Advanced Combat Logging on;
   - play in a window or borderless window, with the bottom-right corner uncovered.
4. Start it: `python companion.py` (or they double-click `Start_Companion.bat`). It prints the address of the
   HUD, and a second address for a tablet or phone on the same network.

## Check it works

- `python beacon.py` with the game in front prints one reading of the strip. "no strip on screen" = the add-on is
  not ticked, the corner is covered, or the game is in exclusive fullscreen.
- `http://127.0.0.1:8765/api/state` is everything the pages show, as JSON: `strip` is what the add-on gives,
  `logs` says whether the game's logs are being written, `lan` is the tablet address.
- `http://127.0.0.1:8765/api/threads` shows where every thread is, if something looks stuck.
- `companion.log` and `minimap_errors.log` in this folder hold errors.
- Right after a start the pages lag up to a minute while the day's combat log is read.

## What may not fit their PC

- The strip, the logs and the map do not depend on screen size or UI layout.
- The other screen reads (health and mana bars, buff bar, dungeon timer, quest tracker text) were measured on a
  1920x1080 game with the author's UI layout; positions are in `screen_layout.json`. On another setup they may
  read nothing. Say so plainly; re-measuring them is a real job, not part of setup.
- Tablet can't connect: Windows Firewall has not allowed Python on their network. That is a security setting:
  explain it (Windows Security > Firewall > Allow an app through firewall > Python) and let them change it.
- Port 8765 taken: `{"port": 8770}` in `settings.json`.

## Rules of the house

- Never send keys, clicks or anything else to the game.
- Screen grabs go through `fastgrab`, never PIL's `ImageGrab.grab`.
- Their maps, logs and numbers live in this folder (`maps/`, `combat_logs/`, `companion_state.json`): don't delete them.
