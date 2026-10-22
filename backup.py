"""
backup.py — daily backup of everything a Defender Octa site cannot be
rebuilt without.
---------------------------------------------------------------------
OCT-178. What this used to be, and what it cost:

    FILES_TO_BACKUP = ["guardiangrid.db", "residents_data.json"]

The resident registry is `residents.json`. There has never been a file
called `residents_data.json` in this project. The copy loop skipped any
name that did not exist, so the registry -- 19 residents, every
plate-to-flat mapping, every phone number the WhatsApp alerts use, and
the blacklist -- was silently left out of every backup ever taken, and
the function reported "Saved 1 file(s)" each time.

Three rules come out of that, and they are why this file looks the way
it does:

  1. NEVER SPELL A PATH TWICE. The registry path is imported from
     resident_db, which owns it. A rename there cannot desynchronise
     from here, because there is nothing here to desynchronise.
  2. A MISSING FILE IS A FAILURE, NOT A SKIP. Every entry is declared
     required or optional. A missing required file makes the run fail
     loudly and exit non-zero, so cron can shout.
  3. SAY WHAT WAS COPIED AND WHAT WAS NOT. The old log line could not
     distinguish "backed up everything" from "backed up half of it".

Also fixed here: SQLite is copied through its own backup API, which
takes a consistent snapshot of a live WAL-mode database instead of
copying the file while it is being written.

NOT fixed here, and it is the one that matters most: these copies live
on the same disk as the originals. Until something leaves the droplet,
a host failure takes both. See OCT-178.

Usage:
    from backup import run_daily_backup
    run_daily_backup(CONFIG.backup_keep_days)      # at startup

    python /app/backup.py --keep-days 14           # from cron (preferred)
    python /app/backup.py --force                  # ignore today's marker
"""

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ── Where the data lives ────────────────────────────────────────────
# Containers run with -w /data, but cwd is not a contract. OCT-76 is in
# the register because a relative path moved when the working directory
# did, and the old BACKUP_DIR = Path("backups") was the same shape.
DATA_DIR = Path(os.environ.get("GG_DATA_DIR") or "/data")
BACKUP_DIR = DATA_DIR / "backups"


def _resident_registry_path() -> Path:
    """The registry path, from the module that owns it. Rule 1."""
    try:
        from resident_db import DB_FILE
        return Path(DB_FILE)
    except Exception:
        # resident_db unimportable (a bare restore box, a unit test).
        # Mirror its default rather than guessing a different one.
        return Path(os.environ.get("GG_RESIDENT_DB") or (DATA_DIR / "residents.json"))


def _items():
    """(path, kind, required) for everything a site cannot be rebuilt without.

    kind: "sqlite" -> snapshot through the sqlite backup API
          "file"   -> straight copy
    """
    return [
        (DATA_DIR / "guardiangrid.db", "sqlite", True),
        (_resident_registry_path(), "file", True),
        (DATA_DIR / "contractors.db", "sqlite", False),
    ]


def _copy_sqlite(src: Path, dst: Path):
    """Consistent snapshot of a live database, WAL included.

    shutil.copy2 on a WAL-mode database copies whatever is on disk at
    that instant; transactions still in the -wal are not in the .db, so
    the copy can be silently behind. sqlite3's backup API reads through
    the same engine the writer uses.
    """
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def run_daily_backup(keep_days: int = 14, force: bool = False) -> bool:
    """Back up once per day. Returns True only if every required item was
    copied. Never raises -- startup must not be blocked by a backup."""
    today = datetime.now().strftime("%Y-%m-%d")
    dest = BACKUP_DIR / f"backup_{today}"
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not force:
            # Already ran today. Say which one, so a reader can check it
            # rather than assume it was complete.
            print(f"[BACKUP] {dest} already exists today — nothing to do "
                  f"(use --force to retake)")
            return True
        dest.mkdir(parents=True, exist_ok=True)

        done, missing, failed = [], [], []
        for src, kind, required in _items():
            if not src.exists():
                if required:
                    missing.append(src.name)
                else:
                    print(f"[BACKUP] optional, absent: {src}")
                continue
            try:
                out = dest / src.name
                if kind == "sqlite":
                    _copy_sqlite(src, out)
                else:
                    shutil.copy2(src, out)
                done.append((src.name, out.stat().st_size))
            except Exception as e:
                failed.append(f"{src.name}: {type(e).__name__}: {e}")

        required_n = sum(1 for _, _, req in _items() if req)
        ok = not missing and not failed

        # Rule 3: the log line distinguishes complete from partial.
        for name, size in done:
            print(f"[BACKUP]   {size:>10,} bytes  {name}")
        # A summary line must not read like success during a failure --
        # which is how "Saved 1 file(s)" hid this bug for months.
        print(f"[BACKUP] copied {len(done)} file(s) into {dest} "
              f"— required {required_n - len(missing)} of {required_n}")
        for name in missing:
            print(f"[BACKUP] ** MISSING REQUIRED FILE: {name} — "
                  f"this backup is NOT complete")
        for msg in failed:
            print(f"[BACKUP] ** FAILED: {msg}")
        if ok:
            _prune(keep_days)
        else:
            print("[BACKUP] ** Backup incomplete; old backups NOT pruned.")
        return ok
    except Exception as e:
        print(f"[BACKUP] ** backup failed ({type(e).__name__}: {e}) — "
              f"continuing anyway.")
        return False


def _prune(keep_days: int):
    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = 0
    for d in BACKUP_DIR.glob("backup_*"):
        try:
            when = datetime.strptime(d.name.replace("backup_", ""), "%Y-%m-%d")
            if when < cutoff:
                shutil.rmtree(d)
                removed += 1
        except Exception:
            continue
    if removed:
        print(f"[BACKUP] Pruned {removed} old backup(s) beyond {keep_days} days.")


def main():
    ap = argparse.ArgumentParser(description="Back up a Defender Octa site")
    ap.add_argument("--keep-days", type=int, default=14)
    ap.add_argument("--force", action="store_true",
                    help="retake today's backup even if it exists")
    args = ap.parse_args()
    ok = run_daily_backup(args.keep_days, force=args.force)
    # Rule 2: cron can only alert on something it can see.
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
