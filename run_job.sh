#!/usr/bin/env bash
# run_job.sh — run a scheduled job; if it fails, tell a human.
#
#   run_job.sh <job-name> <container> <command...>
#
# Cron entries become, for example:
#   0 3 * * * /opt/octa/run_job.sh nightly-reseed octa-demo \
#             /opt/octa/reset_demo.sh octa-demo >> /var/log/octa-jobs.log 2>&1
#
# The point is the failure path. A job that dies at 3am and writes to a log
# nobody reads has not told anyone anything — that is exactly how the demo
# reseed stayed broken for six nights.
set -uo pipefail

NAME="${1:?usage: run_job.sh <job-name> <container> <command...>}"
CONTAINER="${2:?usage: run_job.sh <job-name> <container> <command...>}"
shift 2

START=$(date '+%F %T')
echo "== $START  job=$NAME"

set +e
"$@"
RC=$?
set -e

END=$(date '+%F %T')

if [ "$RC" -eq 0 ]; then
  echo "== $END  job=$NAME OK"
  exit 0
fi

echo "!! $END  job=$NAME FAILED (exit $RC)" >&2

# Best effort. If the notification itself cannot be sent, say so in the log
# and still exit non-zero — never swallow the original failure.
docker exec -w /data "$CONTAINER" python /app/ops_notify.py \
  "Scheduled job '$NAME' failed (exit $RC) at $END. Nothing was retried — check /var/log/octa-jobs.log on the droplet." \
  || echo "!! could not send the failure notification either" >&2

exit "$RC"
