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
`/instance`, `/map` (wheel, pinch or + − to zoom), and `/characters`: each of your characters' level, money,
gear and kills over days and weeks (a line is saved to `character_log.jsonl` every few minutes while you play).

## What you see

- **HUD**: XP, pace and time to level, rested, money, your target, buffs, party health, the quest log, and a
  small map around you. A header alert shows when a hostile player is in your fights.
- **Map**: drawn from your own minimap as you walk, so it starts empty and fills in. Switch **Mapping** on at
  the top of the page. Marked on it:
  - flight points of both sides (yours brighter once you have used one), named when in view;
  - zone exits: a starter list of well-known passes, replaced by the exact spot once you walk through one;
  - dungeon (D) and raid (R) entrances, with their level range;
  - quest places, when a quest has been looked up;
  - your own marks (★): put a file `my_marks.json` next to `companion.py`, for example
    `[{"zone": "Ashenvale", "x": 31.4, "y": 31.5, "label": "Heartswood"}]` (x, y as the game's map shows
    them). It is read again whenever you save it.
- **Dungeon**: the run's clock, bosses, deaths, a map of the instance built from your kills, and damage, healing
  and damage taken per person for the run (pets count for their owner).
- **Group**: who is in the party, their gear level once they have been in range out of combat, and who is
  doing what.
- **Characters**: level, money, gear and kills of each of your characters over days and weeks.
- **Quests done** (`/done`): every quest you have finished, by character and day, from the chat log; with a box to find one by name.

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

## Quest look-ups by Claude (optional)

Click a quest on the HUD and you get a Wowhead link. If Claude (the desktop app or Claude Code) is installed on
the PC, the companion can also have it look the quest up on the web and answer in a few lines: where to go, what
to do, anything worth knowing.

- It is off until you switch it on: the **Claude: off / on** button at the top of the HUD's Quests card (it only
  shows when Claude is installed, and only works on the PC itself). Setup.bat asks too.
- Each look-up runs your own Claude, signed in as you, on your plan. Nothing goes through anyone else's account.
- Claude has to be signed in as a command-line program, which is separate from being signed in to the desktop
  app. If it is not, the button reads **Claude: sign in**: click it and Claude opens its own sign-in window and
  your browser, where you sign in. The companion never sees your password.
- What is sent: the quest's name, level and tracker text, your level and zone.
- Claude also marks the quest's places on the maps: the turn-in (?), where it starts (!) and where to hunt (◆),
  on the HUD's map (arrows on its edge when they are off the card) and on the map page. They are as good as
  the look-up: often from memory of Classic, so treat them as "around here".
- The answer and its marks are kept until the quest leaves your quest log (finished or abandoned), then put
  away. Take the quest again and they come back without a new look-up. "Look it up again" under an answer asks
  afresh.
- Only from the PC itself: a tablet on the network can read answers, not start look-ups.
- Without Claude the companion works the same, minus those answers.

## Settings (optional)

`settings.json` next to `companion.py`; Setup writes it when it needs to:

```json
{"wow_dir": "D:\\Games\\World of Warcraft", "port": 8765, "lan": true, "claude": false}
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
- Each character has its own session numbers (kills, deaths, pace, spells); switching characters switches them.
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
