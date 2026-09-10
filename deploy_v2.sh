#!/bin/bash
# ============================================================================
# deploy.sh v2 — DEFENDER OCTA multi-site deploy
# ----------------------------------------------------------------------------
# One command updates EVERY site: pull -> build once -> recreate each
# container from /opt/octa-ops/sites.conf -> verify each.
#
#   sudo /opt/octa/deploy.sh              # deploy all sites
#   sudo /opt/octa/deploy.sh agi-infra    # deploy one site only
#
# sites.conf format (one line per site):
#   slug|port|data_dir|config_dir|env_file
# Legacy AGI line (configs baked in image, no config_dir) uses "-" :
#   agi-infra|5007|/opt/societies/agi-infra|-|/opt/octa-ops/agi.env
# ============================================================================
set -e

BRANCH="mediamtx-hls-integration"
IMAGE="defender-octa"
SITES_CONF="/opt/octa-ops/sites.conf"
ONLY="$1"

cd /opt/octa

echo "── [1/5] Pulling latest $BRANCH ──"
git fetch origin
git reset --hard "origin/$BRANCH"
echo "    now at: $(git log --oneline -1)"

SHA="$(git log --format=%h -1)"

echo "── [2/5] Building image (shared by all sites) ──"
docker build --build-arg GIT_SHA="$SHA" -t "$IMAGE" .

if [ ! -f "$SITES_CONF" ]; then
  echo "❌ $SITES_CONF missing. Create it, e.g.:"
  echo "   agi-infra|5007|/opt/societies/agi-infra|-|/opt/octa-ops/agi.env"
  exit 1
fi

echo "── [3/5] Recreating site containers ──"
DEPLOYED=0
while IFS='|' read -r SLUG PORT DATA CFG ENVF; do
  [ -z "$SLUG" ] && continue
  case "$SLUG" in \#*) continue;; esac
  if [ -n "$ONLY" ] && [ "$SLUG" != "$ONLY" ]; then continue; fi

  CONTAINER="octa-$SLUG"
  echo "  ── $CONTAINER (port $PORT) ──"
  docker stop "$CONTAINER" 2>/dev/null || true
  docker rm "$CONTAINER" 2>/dev/null || true

  MOUNTS="-v $DATA:/data"
  if [ "$CFG" != "-" ] && [ -d "$CFG" ]; then
    MOUNTS="$MOUNTS \
      -v $CFG/site_config.json:/app/site_config.json:ro \
      -v $CFG/config.py:/app/config.py:ro \
      -v $CFG/whatsapp_config.py:/app/whatsapp_config.py:ro"
  fi

  eval docker run -d --name "$CONTAINER" \
    -p "127.0.0.1:$PORT:5000" \
    $MOUNTS \
    --env-file "$ENVF" -e TZ=Asia/Kolkata \
    --restart unless-stopped "$IMAGE"
  DEPLOYED=$((DEPLOYED+1))
done < "$SITES_CONF"

if [ "$DEPLOYED" = 0 ]; then
  echo "❌ no site matched '$ONLY' in $SITES_CONF"; exit 1
fi

echo "── [4/5] Verifying ──"
sleep 6
FAIL=0
while IFS='|' read -r SLUG PORT DATA CFG ENVF; do
  [ -z "$SLUG" ] && continue
  case "$SLUG" in \#*) continue;; esac
  if [ -n "$ONLY" ] && [ "$SLUG" != "$ONLY" ]; then continue; fi
  CONTAINER="octa-$SLUG"
  STATUS=$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo missing)
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/api/search/ping" || echo 000)
  RUNSHA=$(docker exec "$CONTAINER" printenv OCTA_GIT_SHA 2>/dev/null || echo "?")
  if [ "$STATUS" = "running" ] && { [ "$CODE" = "401" ] || [ "$CODE" = "200" ]; } \
     && [ "$RUNSHA" = "$SHA" ]; then
    echo "  ✅ $CONTAINER: $STATUS, ping $CODE, running $RUNSHA"
  elif [ "$STATUS" = "running" ] && [ "$RUNSHA" != "$SHA" ]; then
    # The container is up and answering, but on OTHER code. Every previous
    # version of this script would have printed a green tick here.
    echo "  ❌ $CONTAINER: up and answering, but running $RUNSHA — expected $SHA"
    echo "     The deploy did not take. Check the build context and sites.conf."
    FAIL=1
  else
    echo "  ❌ $CONTAINER: $STATUS, ping $CODE — logs:"
    docker logs --tail 15 "$CONTAINER" 2>&1 | sed 's/^/     /'
    FAIL=1
  fi
done < "$SITES_CONF"

[ "$FAIL" = 1 ] && exit 1
# ── [5/5] Install the ops scripts from the repo ─────────────────────────────
# These used to exist twice — once in git, once hand-edited under /opt — with
# nothing keeping them in step. reset_demo.sh drifted that way and failed
# every night for a week. The repo is the only source now; this step puts
# each script where it is actually run from.
#
# cp would truncate a running script in place (this file is one of them), so
# write beside it and mv, which swaps the inode and leaves the running
# process on the old one.
echo "── [5/5] Installing ops scripts ──"
install_script() {
  SRC="$1"; DEST="$2"
  [ -f "$SRC" ] || { echo "  ⚠️  $SRC missing in repo — skipped"; return; }
  if cmp -s "$SRC" "$DEST" 2>/dev/null; then
    echo "  ·  $DEST unchanged"
    return
  fi
  cp "$SRC" "$DEST.new" && chmod +x "$DEST.new" && mv "$DEST.new" "$DEST"
  echo "  ✅ $DEST updated"
}

install_script /opt/octa/deploy_v2.sh   /opt/octa/deploy.sh
install_script /opt/octa/reset_demo.sh  /opt/octa/reset_demo.sh
install_script /opt/octa/run_job.sh     /opt/octa/run_job.sh
install_script /opt/octa/new_site.sh    /opt/octa-ops/new_site.sh

echo "✅ Deployed $(git log --oneline -1) to all matching sites"
