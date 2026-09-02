#!/bin/sh
# Defender Octa container entrypoint.
# /data = this society's private folder (config, DB, backups, reports).
# Symlink code assets from /app so cwd-relative paths resolve.
#
# Two config layouts are supported:
#   LEGACY (AGI line in sites.conf, config_dir "-"):
#       site_config.json lives in /data; we link it up to /app.
#   NEW (config_dir set): site_config.json is bind-mounted READ-ONLY at
#       /app/site_config.json; we link it DOWN into /data so cwd-relative
#       reads resolve. Trying to ln -sf over a mounted file fails with
#       "Device or resource busy" — that was the demo/primera crash loop.
set -e
cd /data
for item in indian_anpr frontend static templates dashboard.html models core backend; do
    if [ -e "/app/$item" ] && [ ! -e "/data/$item" ]; then
        ln -s "/app/$item" "/data/$item"
    fi
done

if [ -e /app/site_config.json ] && [ ! -L /app/site_config.json ] && [ ! -w /app/site_config.json ]; then
    # NEW layout: read-only mount at /app is the source of truth.
    if [ ! -e /data/site_config.json ] || [ -L /data/site_config.json ]; then
        ln -sf /app/site_config.json /data/site_config.json
    fi
    echo "[ENTRYPOINT] Using mounted site_config.json (read-only)"
else
    # LEGACY layout: /data is the source of truth, exposed at /app.
    if [ ! -f /data/site_config.json ]; then
        echo "[ENTRYPOINT] No site_config.json in /data — copying template"
        cp /app/site_config.json.example /data/site_config.json
    fi
    ln -sf /data/site_config.json /app/site_config.json
fi

exec python /app/serve.py --port 5000
