# WoW Companion (second screen)

A second-screen dashboard for WoW Forever (Classic-era): XP and pace, kills, deaths, rested, money, your party,
dungeon runs, and a map that draws itself as you walk. It opens in a browser, on a second monitor or on a tablet
or phone on the same Wi-Fi.

Read-only: it reads the game's log files and what is on the screen. It never sends anything to the game.

## Get it

From https://github.com/wclontz/wow-companion: open **Releases** and download the newest `WoWCompanion_….zip`.
Or, with git: `git clone https://github.com/wclontz/wow-companion`, and later `git pull` in that folder for updates.

## Set it up (once)

You need Windows 10 or 11 and Python 3.10 or newer (python.org; tick "Add python.exe to PATH" in its installer).

1. Unzip this folder anywhere (Documents is fine).
2. Double-click **Setup.bat**. It installs two Python packages (numpy, Pillow), finds the game, copies the
   add-on into the game's `Interface\AddOns` folder, and asks whether the companion should start by itself
   whenever the game runs.
3. In the game: restart it, click **AddOns** on the character screen and tick **Companion Beacon** (tick
   "Load out of date AddOns" too if it is greyed out). Then Options > Network > **Advanced Combat Logging** on.
4. Double-click **Start_Companion.bat**. The HUD opens in your browser.

Using Claude? Open this folder in Claude and say "set this up": `SETUP_FOR_CLAUDE.md` tells it what to do.

## The game side

- The add-on draws a small block of coloured squares in the bottom-right corner of the game. That is how the
  companion gets your exact position, level, XP, party and quest log. Keep that corner uncovered. `/cbeacon`
  hides or shows it.
- Play in a window or borderless window, not exclusive fullscreen, or the screen can't be read.
- The add-on switches the combat and chat logs on at every login. Without it, type `/combatlog` (and
  `/chatlog` for exact XP, loot and gold) each session.
- Keep the minimap zoomed all the way out while Mapping.

## The pages

`http://127.0.0.1:8765/hud` is the one-screen HUD. From there: all the numbers (`/`), `/group`, `/dungeon`,
`/instance`, `/map` (wheel, pinch or + − to zoom).

## One monitor? Use a tablet or phone

The companion prints an address like `http://192.168.1.50:8765/hud` when it starts, and shows it at the top of
the HUD ("▦ tablet: …"; click it for a QR code to point the tablet's camera at). Open that in a browser on any
device on the same network.

- The first time, Windows asks whether Python may be reached on the network: allow it (private networks).
  If the tablet can't connect, that prompt was probably dismissed: allow Python in Windows Security >
  Firewall > "Allow an app through firewall".
- Other devices can look, set your level and switch Mapping. Restoring or importing maps, clearing the trail,
  renaming labels and quest look-ups only work on the PC itself.
- Anyone on your network can open the pages. To keep them to this PC, put `{"lan": false}` in `settings.json`.

## Settings (optional)

`settings.json` next to `companion.py`; Setup writes it when it needs to:

```json
{"wow_dir": "D:\\Games\\World of Warcraft", "port": 8765, "lan": true}
```

## Good to know

- The game writes its log in chunks, so the numbers run 30 s to 2 min behind. What comes from the add-on
  (position, level, XP, party, quests) is live.
- Without the chat log, XP per kill is estimated from the mob's level until the add-on's XP reading corrects it.
- The screen reads other than the add-on's strip (health bars, buffs, the dungeon timer) were measured on a
  1920x1080 game with one particular UI layout. On another layout or size they may show nothing; the strip,
  the logs and the map still work. `screen_layout.json` holds those positions.
- Mob, item and icon info is looked up on wow-forever.gg / Wowhead the first time each is seen, and kept in this
  folder, so those fill in as you play.
- Your maps, logs and numbers stay in this folder. `maps` grows as you explore (several GB over months).
- Map packages: the map page can export a map and import a friend's.
- Update: unzip the new release over the old folder (or `git pull`) and run Setup.bat again, which copies the
  newer add-on into the game. Your maps and numbers are kept.
- `python companion.py --dump` reads the logs once and prints everything (for checking).
- Remove: `python autostart.pyw --uninstall`, delete the `CompanionBeacon` folder from the game's
  `Interface\AddOns`, delete this folder.

## Licence

MIT (see `LICENSE`). A hobby project, not affiliated with or endorsed by Blizzard Entertainment. World of
Warcraft is a trademark of Blizzard Entertainment, Inc.
