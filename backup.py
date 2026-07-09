"""
backup.py — automatic daily backup of the Defender Octa database
-----------------------------------------------------------------
Copies guardiangrid.db (and residents_data.json if present) into a
timestamped file under ./backups/, and prunes backups older than
keep_days. Safe to call at every startup — it backs up once per day.

Called from api_server.py at startup:
    from backup import run_daily_backup
    run_daily_backup(CONFIG.backup_keep_days)
"""

import shutil
from datetime import datetime, timedelta
from pathlib import Path

BACKUP_DIR = Path("backups")
FILES_TO_BACKUP = ["guardiangrid.db", "residents_data.json"]


def run_daily_backup(keep_days: int = 14):
    try:
        BACKUP_DIR.mkdir(exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")

        # Back up once per day: skip if today's backup already exists
        marker = BACKUP_DIR / f"backup_{today}"
        if marker.exists():
            return

        marker.mkdir(exist_ok=True)
        copied = 0
        for fname in FILES_TO_BACKUP:
            src = Path(fname)
            if src.exists():
                shutil.copy2(src, marker / fname)
                copied += 1
        print(f"[BACKUP] Saved {copied} file(s) to {marker}")

        _prune(keep_days)
    except Exception as e:
        print(f"[BACKUP] WARNING: backup failed ({e}) — continuing anyway.")


def _prune(keep_days: int):
    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = 0
    for d in BACKUP_DIR.glob("backup_*"):
        try:
            datestr = d.name.replace("backup_", "")
            when = datetime.strptime(datestr, "%Y-%m-%d")
            if when < cutoff:
                shutil.rmtree(d)
                removed += 1
        except Exception:
            continue
    if removed:
        print(f"[BACKUP] Pruned {removed} old backup(s) beyond {keep_days} days.")
