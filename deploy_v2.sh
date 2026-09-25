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

# ── [1b] Prune stale frontend bundles — OCT-17 ──────────────────────────────
# The frontend is copied into this repo by `npm run deploy` (vite build, then
# xcopy /E /Y). xcopy copies but never deletes, and each build emits a new
# content-hashed bundle, so every build's bundle stays behind. They are
# COMMITTED, then baked into the image by `COPY . /app`. Measured 20 Sep: 21
# index-*.js bundles, 37 MB, of which one was live. Wiped by hand twice,
# regressed twice. A fix that depends on somebody remembering is not a fix.
#
# This runs on the checkout before the build, so the IMAGE is clean however
# many bundles git holds. It keeps everything reachable from index.html
# TRANSITIVELY — a code-split chunk is loaded by another bundle, not by
# index.html, and deleting what index.html alone mentions would break the app.
# Only .js/.mjs/.css are ever removed; images and fonts are left alone. Any
# failure skips the prune and the deploy carries on.
#
# The source of the stale files is still the xcopy on Windows; this stops
# them reaching a container, it does not stop them being committed.
echo "── [1b] Pruning stale frontend bundles (OCT-17) ──"
python3 - /opt/octa/frontend <<'PRUNE_PY' || echo "  ⚠️  prune skipped (error above) — deploy continues"
import os, re, sys
root = sys.argv[1] if len(sys.argv) > 1 else "/opt/octa/frontend"
assets = os.path.join(root, "assets")
index = os.path.join(root, "index.html")
if not (os.path.isdir(assets) and os.path.isfile(index)):
    print("  ·  no frontend/assets or index.html — nothing to prune"); sys.exit(0)
files = set(os.listdir(assets))
ref = re.compile(r"[A-Za-z0-9_.\-]+\.(?:js|mjs|css)")
keep, todo = set(), []
def visit(text):
    for name in ref.findall(text):
        if name in files and name not in keep:
            keep.add(name); todo.append(name)
with open(index, encoding="utf-8", errors="ignore") as f:
    visit(f.read())
if not keep:
    print("  ⚠️  index.html references no bundle — refusing to prune"); sys.exit(0)
while todo:
    name = todo.pop()
    try:
        with open(os.path.join(assets, name), encoding="utf-8", errors="ignore") as f:
            visit(f.read())
    except OSError:
        pass
stale = sorted(f for f in files - keep if f.endswith((".js", ".mjs", ".css")))
freed = 0
for f in stale:
    p = os.path.join(assets, f); freed += os.path.getsize(p); os.remove(p)
print(f"  kept {len(keep)} reachable bundle(s), removed {len(stale)} stale ({freed/1e6:.1f} MB)")
for f in sorted(keep): print(f"     keep  {f}")
for f in stale:        print(f"     drop  {f}")
PRUNE_PY

echo "── [2/5] Building image (shared by all sites) ──"
docker build --build-arg GIT_SHA="$SHA" -t "$IMAGE" .

# ── [2b] Lint — OCT-103 ─────────────────────────────────────────────────────
# On 19 Sep an undefined name (threading, never imported) reached both live
# sites and stayed a day, making every WhatsApp alert report failure while it
# was being delivered. pyflakes finds that class in under a second. Nothing in
# this pipeline ran it. On 21 Sep it caught the same mistake again — an
# unimported `sys` — before it shipped, because by then it was being run by hand.
#
# Only undefined names and syntax errors are reported: those fail at runtime.
# Unused imports and similar are left out deliberately — a lint step that
# prints forty warnings every deploy is a lint step that stops being read.
#
# NON-BLOCKING by default: it reports and the deploy proceeds, so a
# pre-existing problem cannot wedge a release. OCTA_LINT_STRICT=1 makes it
# stop the deploy BEFORE any container is touched.
echo "── [2b] Lint: undefined names and syntax errors (OCT-103) ──"
LRC=0
LINT=$(docker run --rm --entrypoint sh "$IMAGE" -c \
  'pip install -q pyflakes >/dev/null 2>&1 || exit 3; cd /app && python3 -m pyflakes *.py backend 2>&1') || LRC=$?
if [ "$LRC" = "3" ]; then
  echo "  ⚠️  could not install pyflakes in the image — lint skipped"
else
  # test_whatsapp.py is a shell command saved with a .py extension; it is not
  # imported by anything and should be deleted from the repo.
  PROBLEMS=$(printf '%s\n' "$LINT" \
    | grep -E "undefined name|invalid syntax|SyntaxError|unterminated|unexpected indent|expected an indented" \
    | grep -v "test_whatsapp.py" || true)
  if [ -n "$PROBLEMS" ]; then
    N=$(printf '%s\n' "$PROBLEMS" | wc -l)
    echo "  ⚠️  $N problem(s) that will fail when that code runs:"
    printf '%s\n' "$PROBLEMS" | sed 's/^/     /'
    if [ "${OCTA_LINT_STRICT:-0}" = "1" ]; then
      echo "❌ OCTA_LINT_STRICT=1 — stopping before any container is touched"
      exit 1
    fi
    echo "     (not blocking — set OCTA_LINT_STRICT=1 to make this stop a deploy)"
  else
    echo "  ✅ no undefined names or syntax errors"
  fi
fi

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

  # Always ensure the executable bit, whatever else happens. Git does not
  # reliably carry it across a Windows commit, and the first version of this
  # function skipped chmod whenever the contents matched — which for a script
  # already living in the checkout (source and destination the same file) was
  # every single time. The result was a script present, current, and not
  # runnable: "command not found" for a file plainly there.
  chmod +x "$DEST" 2>/dev/null || true

  if [ "$SRC" = "$DEST" ]; then
    echo "  ·  $DEST in place (executable)"
    return
  fi
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
# OCT-118. Left off this list when it was written, so it arrived through the
# git pull as -rw-r--r-- and `sudo /opt/octa/set_site_password.sh` answered
# "command not found" -- for a file plainly sitting there. Git does not
# reliably carry the executable bit across a Windows commit, which is the
# entire reason this step exists. Hit for real on 25 Sep while resetting a
# guard password mid-verification: the script you reach for under pressure
# was the one that would not run.
install_script /opt/octa/set_site_password.sh /opt/octa/set_site_password.sh

echo "✅ Deployed $(git log --oneline -1) to all matching sites"
