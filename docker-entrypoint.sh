#!/bin/sh
# Defender Octa container entrypoint.
# /data = this society's private folder (config, DB, backups, reports).
# Symlink code assets from /app so cwd-relative paths resolve.
set -e
cd /data
for item in indian_anpr frontend static templates dashboard.html models core backend; do
    if [ -e "/app/$item" ] && [ ! -e "/data/$item" ]; then
        ln -s "/app/$item" "/data/$item"
    fi
done
ln -sf /data/site_config.json /app/site_config.json
if [ ! -f /data/site_config.json ]; then
    echo "[ENTRYPOINT] No site_config.json in /data — copying template"
    cp /app/site_config.json.example /data/site_config.json
fi
exec python /app/serve.py --port 5000
