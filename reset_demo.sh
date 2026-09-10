#!/usr/bin/env bash
# reset_demo.sh — wipe and re-seed demo.snguardiangrid.com sample data
# ---------------------------------------------------------------------
# Runs every seed family in the right order inside the demo container:
#   purge everything seeded  ->  28 days of gate traffic (anomaly baseline)
#   ->  14 days of incidents ->  14 days of briefs  ->  "last night" hits
#
# Usage on the droplet — run the GIT-MANAGED copy, so deploy.sh keeps it
# current. A second, hand-edited copy under /opt/octa-ops drifted from this
# one and failed every night for a week with "DB not found: /app/..." while
# nothing surfaced the error:
#   sudo /opt/octa/reset_demo.sh                  # container octa-demo
#   sudo /opt/octa/reset_demo.sh octa-society1
#
# Nightly (3 AM IST) — root's crontab:
#   0 3 * * * /opt/octa/reset_demo.sh >> /var/log/octa-demo-reset.log 2>&1
#
# Adjust APP_DIR if the seed_*.py files are not at /app inside the image.

set -euo pipefail

CONTAINER="${1:-octa-demo}"
APP_DIR="${APP_DIR:-/app}"
DATA_DIR="${DATA_DIR:-/data}"

VEHICLE_DAYS="${VEHICLE_DAYS:-28}"   # >= 28 so anomaly_score leaves "learning"
INCIDENT_DAYS="${INCIDENT_DAYS:-14}"
BRIEF_DAYS="${BRIEF_DAYS:-14}"

run() {
  docker exec -w "$DATA_DIR" "$CONTAINER" python "$APP_DIR/$1" "${@:2}"
}

echo "== $(date '+%F %T') reset_demo on $CONTAINER"

echo "-- purge"
run seed_test_events.py --remove
run seed_incidents.py --purge
run seed_briefs.py --purge
run seed_demo.py --clear

echo "-- seed"
run seed_demo.py --days "$VEHICLE_DAYS"
run seed_incidents.py --days "$INCIDENT_DAYS"
run seed_briefs.py --days "$BRIEF_DAYS"
run seed_test_events.py

# The app holds open SQLite connections. Seeding from outside the process
# leaves those handles pointing at pages that have been rewritten underneath
# them, which surfaces later as "database disk image is malformed". Restarting
# after a reseed is what stops that, and it belongs here rather than in one
# hand-edited copy on the droplet.
echo "-- restart $CONTAINER"
docker restart "$CONTAINER" >/dev/null

# Wait for it to answer before declaring success. A reseed that leaves the
# site down is worse than no reseed, and this ran unattended for six nights
# with nobody reading the log.
for i in $(seq 1 30); do
  if docker exec "$CONTAINER" python -c "
import urllib.request, sys
try:
    urllib.request.urlopen('http://127.0.0.1:5000/', timeout=2)
except Exception as e:
    sys.exit(0 if '401' in str(e) or '403' in str(e) else 1)
" 2>/dev/null; then
    echo "-- $CONTAINER is answering"
    break
  fi
  if [ "$i" = "30" ]; then
    echo "!! $CONTAINER did NOT come back after the reseed" >&2
    exit 1
  fi
  sleep 2
done

echo "== done $(date '+%F %T')"
