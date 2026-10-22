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

# The rclone remote that holds the off-site copy. "octa:" is a crypt remote
# wrapping b2raw:octa-backups-sn/octa, so everything written through it is
# encrypted before it leaves this machine. Set to empty to disable, which
# makes this script fail loudly rather than silently keeping one copy.
OFFSITE_REMOTE="${OFFSITE_REMOTE-octa:}"

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
    continue
  fi

  # ── off-site copy ────────────────────────────────────────────────
  # OCT-178. Everything above produces a copy on the SAME DISK as the
  # original. That is not a backup; it is a second file. This step is the
  # only part of the chain that survives the droplet.
  #
  # Encrypted before it leaves: the archive holds residents' names, phone
  # numbers, flat numbers and vehicle plates, which is personal data under
  # DPDP. rclone's crypt remote encrypts contents AND filenames, so the
  # bucket cannot even advertise what is in it. The passphrases are in
  # /root/.rclone.conf AND in a password manager off this machine --
  # a backup whose key dies with the server is not a backup.
  # NO DATE ARITHMETIC HERE, deliberately. The first version of this step
  # uploaded "backup_$(date +%F)" computed on the HOST, which runs UTC,
  # while the container names its folder from its own clock, which is IST.
  # Between 00:00 and 05:30 IST those are different days -- and the backup
  # cron fires at 02:30 IST, inside that window every single night. It
  # uploaded yesterday's folder, reported "3 file(s) verified", and exited
  # 0. The off-site copy would have been permanently one day stale, and the
  # missing day would have been the most recent one.
  #
  # That is OCT-24b's shape for the fourth time: two clocks, one boundary,
  # and whatever stood outside the fix kept its own time. The cure is not a
  # better timezone conversion. It is to copy the whole directory and let
  # rclone work out what is new -- the entire tree is under 10MB.
  SRC="${data_dir}/backups"
  DEST="${OFFSITE_REMOTE}${slug}"

  if [ -z "${OFFSITE_REMOTE:-}" ]; then
    echo "!! OFFSITE_REMOTE unset — local copy only, nothing left this host" >&2
    rc=1
  elif [ ! -d "$SRC" ]; then
    echo "!! $slug: no local backup at $SRC to upload" >&2
    rc=1
  elif ! rclone copy "$SRC" "$DEST" --transfers 2 --retries 3 2>&1; then
    echo "!! $slug: off-site upload failed" >&2
    rc=1
  else
    # Verify by READING IT BACK, not by trusting the exit code. A copy
    # that silently wrote nothing exits 0.
    want=$(find "$SRC" -type f | wc -l)
    got=$(rclone lsf -R --files-only "$DEST" 2>/dev/null | wc -l)
    if [ "$want" -eq 0 ] || [ "$want" -ne "$got" ]; then
      echo "!! $slug: off-site verify FAILED — $want file(s) local, $got off-site" >&2
      rc=1
    else
      echo "   off-site: $got file(s) verified at $DEST"
      # Prune old off-site copies. --min-age matches the local keep_days so
      # the two sides do not quietly diverge.
      rclone delete "$DEST" --min-age "${KEEP_DAYS}d" --rmdirs 2>/dev/null || true
    fi
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
