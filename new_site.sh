#!/bin/bash
# ============================================================================
# new_site.sh — DEFENDER OCTA: onboard a new client site in one command
# ----------------------------------------------------------------------------
# Usage:
#   sudo /opt/octa-ops/new_site.sh <slug> "<Site Display Name>" <port>
#   sudo /opt/octa-ops/new_site.sh singhania "Singhania Industries" 5008
#
# What it does:
#   1. Creates /opt/societies/<slug>/            (data volume: DB, snapshots)
#   2. Creates /opt/societies/<slug>-config/     (site-local configs, from
#      templates: site_config.json, config.py, whatsapp_config.py) with
#      fresh random admin password + 48-char JWT secret
#   3. Creates /opt/octa-ops/<slug>.env          (API keys; copies agi.env)
#   4. Registers the site in /opt/octa-ops/sites.conf (deploy.sh reads it)
#   5. Starts the container: defender-octa image, per-site configs
#      BIND-MOUNTED over the baked ones — one image serves every site
#   6. Prints the Cloudflare tunnel route + heartbeat entry to add
#
# Safe to re-run with --dry-run to preview without changing anything.
# ============================================================================
set -e

DRY=0
if [ "$1" = "--dry-run" ]; then DRY=1; shift; fi

SLUG="$1"; NAME="$2"; PORT="$3"
if [ -z "$SLUG" ] || [ -z "$NAME" ] || [ -z "$PORT" ]; then
  echo "Usage: sudo $0 [--dry-run] <slug> \"<Site Display Name>\" <port>"
  echo "   ex: sudo $0 singhania \"Singhania Industries\" 5008"
  exit 1
fi
if ! echo "$SLUG" | grep -qE '^[a-z0-9-]+$'; then
  echo "slug must be lowercase letters/digits/hyphens"; exit 1
fi

DATA="/opt/societies/$SLUG"
CFG="/opt/societies/${SLUG}-config"
OPS="/opt/octa-ops"
ENVF="$OPS/$SLUG.env"
CONTAINER="octa-$SLUG"
IMAGE="defender-octa"

run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else eval "$*"; fi }

echo "── Onboarding: $NAME  (slug=$SLUG, port=$PORT) ──"

# 1) data + config dirs -------------------------------------------------------
run "mkdir -p '$DATA' '$CFG' '$OPS'"

# 2) per-site configs ---------------------------------------------------------
ADMIN_PASS=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 14)
JWT_SECRET=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48)

if [ "$DRY" = 0 ]; then
cat > "$CFG/site_config.json" <<JSON
{
  "society": {
    "name": "$NAME",
    "slug": "$SLUG"
  },
  "server": { "port": 5000 },
  "camera": { "enabled": false },
  "rtsp_cameras": [],
  "recording": { "enabled": false },
  "detection": { "enabled": true },
  "admin": { "username": "admin-$SLUG" },
  "viewer": { "enabled": true, "username": "$SLUG-demo" },
  "backup": { "enabled": true },
  "face": { "enabled": false },
  "demo": { "enabled": false }
}
JSON

cat > "$CFG/config.py" <<PY
# Site-local secrets for $NAME — NEVER commit this file.
ADMIN_USERNAME = "admin-$SLUG"
ADMIN_PASSWORD = "$ADMIN_PASS"
SECRET_KEY = "$JWT_SECRET"
PY

cat > "$CFG/whatsapp_config.py" <<PY
# WhatsApp config for $NAME — fill Twilio + client numbers, NEVER commit.
TWILIO_ACCOUNT_SID = ""
TWILIO_AUTH_TOKEN  = ""
TWILIO_WHATSAPP_FROM = "whatsapp:+14155238886"
ENABLE_WHATSAPP_ALERTS = False        # flip True when numbers are filled
SECURITY_WHATSAPP = ""                # site security head
PATTERN_WHATSAPP  = "whatsapp:+918847406740"   # founder during trial
REPORT_WHATSAPP   = ""                # morning brief recipient
PUBLIC_BASE_URL   = "https://$SLUG.snguardiangrid.com"
PY
  chmod 600 "$CFG/config.py" "$CFG/whatsapp_config.py"
else
  echo "[dry-run] would write $CFG/{site_config.json,config.py,whatsapp_config.py}"
fi

# 3) env file (API keys) ------------------------------------------------------
if [ ! -f "$ENVF" ]; then
  if [ -f "$OPS/agi.env" ]; then
    run "cp '$OPS/agi.env' '$ENVF' && chmod 600 '$ENVF'"
  else
    run "echo 'ANTHROPIC_API_KEY=' > '$ENVF' && chmod 600 '$ENVF'"
  fi
fi

# 4) register in sites.conf ---------------------------------------------------
LINE="$SLUG|$PORT|$DATA|$CFG|$ENVF"
if [ "$DRY" = 0 ]; then
  touch "$OPS/sites.conf"
  grep -q "^$SLUG|" "$OPS/sites.conf" || echo "$LINE" >> "$OPS/sites.conf"
else
  echo "[dry-run] would append to sites.conf: $LINE"
fi

# 5) start the container ------------------------------------------------------
RUN_CMD="docker run -d --name $CONTAINER \
  -p 127.0.0.1:$PORT:5000 \
  -v $DATA:/data \
  -v $CFG/site_config.json:/app/site_config.json:ro \
  -v $CFG/config.py:/app/config.py:ro \
  -v $CFG/whatsapp_config.py:/app/whatsapp_config.py:ro \
  --env-file $ENVF -e TZ=Asia/Kolkata \
  --restart unless-stopped $IMAGE"
run "docker rm -f $CONTAINER 2>/dev/null || true"
run "$RUN_CMD"

# 6) operator instructions ----------------------------------------------------
echo ""
echo "══════════════════════════════════════════════════════════════"
echo "✅ $NAME is up:  http://127.0.0.1:$PORT   (container $CONTAINER)"
echo ""
echo "Admin login   : admin-$SLUG"
if [ "$DRY" = 0 ]; then
echo "Admin password: $ADMIN_PASS      <-- record this now, shown once"
fi
echo ""
echo "NEXT STEPS (manual, ~10 min):"
echo "  1. Cloudflare tunnel — add public hostname:"
echo "       $SLUG.snguardiangrid.com  ->  http://localhost:$PORT"
echo "  2. WhatsApp — edit $CFG/whatsapp_config.py"
echo "     (Twilio creds + client numbers), then restart:"
echo "       docker restart $CONTAINER"
echo "  3. Heartbeat — add to $OPS/heartbeat_config.json sites[]:"
echo "       {\"name\": \"$NAME\", \"pi_ip\": \"<tailscale-ip-when-installed>\","
echo "        \"db\": \"$DATA/guardiangrid.db\", \"max_silent_hours\": 6}"
echo "  4. Cameras/Pi later: update site_config.json rtsp_cameras + restart."
echo "══════════════════════════════════════════════════════════════"
