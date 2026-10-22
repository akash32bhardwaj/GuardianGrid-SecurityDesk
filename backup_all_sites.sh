#!/usr/bin/env bash
# backup_all_sites.sh — back up every site listed in sites.conf.
#
#   /opt/octa-ops/backup_all_sites.sh [keep_days]
#
# OCT-178. The first version of the nightly backup cron named one
# container, octa-demo. new_site.sh writes `"backup": {"enabled": true}`
# into every new site's config and schedules nothing, so a new client
# site would come up announcing backups it was not taking -- a config
# flag that says enabled while nothing is scheduled is worse than no
# flag at all.
#
# The fix is to make the SCHEDULE site-agnostic rather than to add
# another thing for new_site.sh to remember. This reads the same
# sites.conf that deploy.sh reads, so a site is covered the moment it
# exists.
#
# Exit code is the point: non-zero if ANY site's backup failed, so
# run_job.sh notifies a human. A partial success is a failure here.
set -uo pipefail

SITES_CONF="${SITES_CONF:-/opt/octa-ops/sites.conf}"
KEEP_DAYS="${1:-14}"

[ -r "$SITES_CONF" ] || { echo "!! cannot read $SITES_CONF" >&2; exit 2; }

rc=0
found=0
while IFS='|' read -r slug port data_dir config_dir env_file; do
  # Skip blanks and commented-out sites. A retired site is a '#' line,
  # which is how primera and the six societies were taken out on 21 Oct.
  case "${slug// /}" in ''|'#'*) continue ;; esac
  slug="${slug// /}"
  container="octa-${slug}"
  found=$((found + 1))

  if ! docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null | grep -q true; then
    echo "!! $container is not running — NOT backed up" >&2
    rc=1
    continue
  fi

  echo "-- $container"
  if ! docker exec "$container" python /app/backup.py --keep-days "$KEEP_DAYS"; then
    echo "!! $container: backup failed" >&2
    rc=1
  fi
done < "$SITES_CONF"

if [ "$found" -eq 0 ]; then
  # An empty site list is not success. It is the shape of a backup that
  # silently protects nothing, which is the whole of OCT-178.
  echo "!! no active sites found in $SITES_CONF" >&2
  exit 2
fi

echo "== $found site(s) processed, exit $rc"
exit "$rc"
