"""Runs at Windows logon (no window). Starts the companion when WoW starts and stops it
when WoW closes. Install/uninstall with:  python autostart.pyw --install / --uninstall
"""
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wowpath
PORT = wowpath.port()
WOW_EXES = {"wow.exe", "wowb.exe", "wowt.exe", "wowclassic.exe", "wowclassicb.exe", "wowclassict.exe"}
LOG = os.path.join(HERE, "companion.log")
NO_WINDOW = 0x08000000
STARTUP = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup",
                       "WoW Companion.lnk")


def wow_running():
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout.lower()
    return any(f'"{exe}"' in out for exe in WOW_EXES)


def port_busy():
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def pythonw():
    exe = sys.executable
    return exe if exe.lower().endswith("pythonw.exe") else exe.replace("python.exe", "pythonw.exe")


def install():
    target = pythonw()
    ps = (f'$s=(New-Object -ComObject WScript.Shell).CreateShortcut("{STARTUP}");'
          f'$s.TargetPath="{target}";$s.Arguments=\'"{os.path.abspath(__file__)}"\';'
          f'$s.WorkingDirectory="{HERE}";$s.Description="Starts the WoW companion when WoW runs";$s.Save()')
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    print("installed:", STARTUP)


def main():
    child = None
    opened = False            # HUD already shown this WoW session: don't pop a tab over the game
    closed_t = None           # when WoW last closed (to archive its final log a bit later)
    while True:
        try:
            running = wow_running()
            if running and child is None and port_busy():
                opened = True                     # another companion (e.g. a dev run) is serving it
            if running and child is None and not port_busy():
                log = open(LOG, "a", encoding="utf-8")
                log.write(f"\n--- {time.ctime()} WoW detected, starting companion\n")
                log.flush()
                args = [pythonw(), os.path.join(HERE, "companion.py")] + (["--no-browser"] if opened else [])
                child = subprocess.Popen(args, cwd=HERE, stdout=log, stderr=log, creationflags=NO_WINDOW)
                opened = True
            elif not running:
                opened = False
                if child is not None:
                    child.terminate()
                    child = None
                    closed_t = time.time()
                # once the last log has settled, save it (the companion isn't running to do it)
                if closed_t and time.time() - closed_t > 330:
                    closed_t = None
                    sys.path.insert(0, HERE)
                    from logarchive import LogArchiver
                    from companion import LOG_DIR
                    LogArchiver(LOG_DIR).archive_once()
            elif child is not None and child.poll() is not None:
                child = None                      # it died; restart on the next pass if WoW still runs
        except Exception:
            pass
        time.sleep(5)


if __name__ == "__main__":
    if "--install" in sys.argv:
        install()
    elif "--uninstall" in sys.argv:
        if os.path.exists(STARTUP):
            os.remove(STARTUP)
        print("removed:", STARTUP)
    else:
        main()
