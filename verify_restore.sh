#!/usr/bin/env bash
# verify_restore.sh — prove the off-site backup can actually be restored.
#
#   /opt/octa-ops/verify_restore.sh [site-slug]        # default: demo
#
# OCT-178. A backup that has never been restored is a belief, not a backup.
# Everything else in this chain proves that bytes LEFT the machine. This is
# the only step that proves they can come back and still be a database.
#
# It downloads the newest off-site copy into a temporary directory, decrypts
# it (rclone's crypt remote does that on the way down), and then asks the
# restored files three questions:
#
#   1. does SQLite consider the database structurally intact
#   2. does it still hold the tables the product needs
#   3. is the resident registry there, with residents in it
#
# Question 3 exists because the original bug was a backup that ran happily
# every night while silently omitting the one file that makes the gate work.
# "The backup restored" and "the restore is usable" are different claims.
#
# Exits non-zero on any failure, so run_job.sh notifies a human.
set -uo pipefail

SLUG="${1:-demo}"
REMOTE="${OFFSITE_REMOTE-octa:}"
TMP="$(mktemp -d /tmp/octa-restore-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

echo "== $(date '+%F %T') restore drill for $SLUG from ${REMOTE}${SLUG}"

LATEST="$(rclone lsf "${REMOTE}${SLUG}/" 2>/dev/null | grep '^backup_' | sort | tail -1)"
if [ -z "$LATEST" ]; then
  echo "!! nothing off-site for $SLUG — there is no backup to restore" >&2
  exit 1
fi
LATEST="${LATEST%/}"
echo "-- newest off-site copy: $LATEST"

if ! rclone copy "${REMOTE}${SLUG}/${LATEST}" "$TMP" --retries 3; then
  echo "!! download failed" >&2; exit 1
fi

rc=0
shopt -s nullglob

# 1. the registry — the file that was missing from every backup until today
REG="$TMP/residents.json"
if [ ! -s "$REG" ]; then
  echo "!! residents.json is MISSING from the restored copy" >&2
  rc=1
else
  n=$(python3 -c "import json,sys; print(len(json.load(open('$REG'))))" 2>/dev/null || echo 0)
  if [ "$n" -lt 1 ]; then
    echo "!! residents.json restored but holds $n residents" >&2; rc=1
  else
    echo "   residents.json: $n resident(s)"
  fi
fi

# 2. every database: structurally intact, and still has its tables
for db in "$TMP"/*.db; do
  name="$(basename "$db")"
  integrity=$(python3 - "$db" <<'PY' 2>/dev/null
import sqlite3, sys
try:
    c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    print(c.execute("pragma integrity_check").fetchone()[0])
except Exception as e:
    print(f"ERROR {type(e).__name__}: {e}")
PY
)
  tables=$(python3 - "$db" <<'PY' 2>/dev/null
import sqlite3, sys
try:
    c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    rows = c.execute("select name from sqlite_master where type='table'").fetchall()
    print(len(rows))
except Exception:
    print(0)
PY
)
  if [ "$integrity" = "ok" ] && [ "${tables:-0}" -gt 0 ]; then
    echo "   $name: integrity ok, $tables table(s)"
  else
    echo "!! $name: integrity='$integrity' tables=$tables" >&2
    rc=1
  fi
done

if [ "$rc" -eq 0 ]; then
  echo "== restore drill PASSED — $LATEST came back and is usable"
else
  echo "!! restore drill FAILED for $SLUG" >&2
fi
exit "$rc"
