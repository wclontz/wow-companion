"""Keeps a permanent, compressed copy of every WoW combat log (and chat log) in combat_logs/.
WoW's own Logs folder is only read, never changed."""
import glob
import gzip
import os
import shutil
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(HERE, "combat_logs")
SETTLE = 300            # a log untouched this long is finished being written


class LogArchiver(threading.Thread):
    def __init__(self, log_dir, every=60):
        super().__init__(daemon=True)
        self.log_dir, self.every = log_dir, every
        self.saved = 0

    def archive_once(self):
        os.makedirs(ARCHIVE, exist_ok=True)
        done = 0
        for src in glob.glob(os.path.join(self.log_dir, "WoWCombatLog*.txt")) + \
                glob.glob(os.path.join(self.log_dir, "WoWChatLog*.txt")):
            try:
                st = os.stat(src)
            except OSError:
                continue
            if time.time() - st.st_mtime < SETTLE:
                continue                                    # WoW may still be writing it
            dest = os.path.join(ARCHIVE, os.path.basename(src) + ".gz")
            marker = dest + ".size"
            try:
                if os.path.exists(dest) and int(open(marker).read()) == st.st_size:
                    continue                                # already saved at this size
            except (OSError, ValueError):
                pass
            tmp = dest + ".part"
            try:                                            # (one unreadable log must not block the ones after it)
                with open(src, "rb") as fin, gzip.open(tmp, "wb", compresslevel=6) as fout:
                    shutil.copyfileobj(fin, fout, 1 << 20)
                os.replace(tmp, dest)
                with open(marker, "w") as f:
                    f.write(str(st.st_size))
            except OSError:
                continue
            done += 1
        self.saved += done
        return done

    def run(self):
        while True:
            try:
                self.archive_once()
            except Exception:
                pass
            time.sleep(self.every)
