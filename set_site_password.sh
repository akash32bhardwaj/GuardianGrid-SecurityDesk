#!/bin/bash
# ============================================================================
# set_site_password.sh — set or replace an account's password on one site,
#                        stored as a hash, never in the clear (OCT-93)
# ----------------------------------------------------------------------------
# Usage, on the droplet:
#   sudo /opt/octa/set_site_password.sh <slug> admin
#   sudo /opt/octa/set_site_password.sh <slug> viewer <username>
#   sudo /opt/octa/set_site_password.sh <slug> guard  <username>
#
#   sudo /opt/octa/set_site_password.sh demo guard gate1
#
# It asks for the password twice, never echoes it, never writes it to the
# shell history or the process list, hashes it inside the app image and
# writes ONLY the hash into that site's site_config.json. The container is
# restarted at the end so the new account exists.
#
# "viewer" and "guard" create the account if it is not there yet:
#   viewer -> read-only (dashboards, cameras, reports; every write refused)
#   guard  -> gate operator (OCT-87: no admin surfaces, no resident export)
# ============================================================================
set -e

SLUG="$1"; KIND="$2"; UNAME="$3"
IMAGE="defender-octa"
CFG="/opt/societies/${SLUG}-config/site_config.json"
CONTAINER="octa-${SLUG}"

usage() {
  echo "Usage: sudo $0 <slug> admin"
  echo "       sudo $0 <slug> viewer <username>"
  echo "       sudo $0 <slug> guard  <username>"
  exit 1
}

[ -z "$SLUG" ] && usage
case "$KIND" in
  admin) ;;
  viewer|guard) [ -z "$UNAME" ] && usage ;;
  *) usage ;;
esac

if [ ! -f "$CFG" ]; then
  echo "No config for site '$SLUG' at $CFG"
  echo "Sites currently configured:"
  ls -1 /opt/societies/*-config/site_config.json 2>/dev/null \
    | sed 's#/opt/societies/##; s#-config/site_config.json##' | sed 's/^/  /'
  exit 1
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "Image '$IMAGE' not found — run a deploy first, then try again."
  exit 1
fi

# ── the password itself: read twice, silently, and kept only in a variable
read -r -s -p "New password for ${KIND} ${UNAME:-} on ${SLUG}: " P1; echo
read -r -s -p "Repeat it: " P2; echo
if [ -z "$P1" ] || [ "$P1" != "$P2" ]; then
  echo "Passwords did not match (or were empty). Nothing changed."
  unset P1 P2
  exit 1
fi
if [ "${#P1}" -lt 10 ]; then
  echo "Use at least 10 characters. Nothing changed."
  unset P1 P2
  exit 1
fi

# ── hash it inside the app image (werkzeug lives there), via the
#    environment so the password never appears in the process list
HASH=$(docker run --rm -e GG_NEW_PASS="$P1" --entrypoint python3 "$IMAGE" -c \
  'import os; from werkzeug.security import generate_password_hash as g; print(g(os.environ["GG_NEW_PASS"]))' \
  2>/dev/null | tail -n 1) || HASH=""
unset P1 P2

case "$HASH" in
  pbkdf2:*|scrypt:*|argon2:*) ;;
  *) echo "Hashing failed inside $IMAGE — nothing changed."; exit 1 ;;
esac

cp -a "$CFG" "$CFG.bak.$(date +%Y%m%d%H%M%S)"

GG_HASH="$HASH" python3 - "$CFG" "$KIND" "${UNAME:-}" <<'PY'
import json, os, sys

path, kind, uname = sys.argv[1], sys.argv[2], sys.argv[3]
h = os.environ["GG_HASH"]
with open(path, encoding="utf-8") as f:
    cfg = json.load(f)

if kind == "admin":
    cfg.setdefault("admin", {})["password"] = h
    who = cfg["admin"].get("username", "admin")
else:
    # Both shapes are supported by auth_models: a single block, or a list.
    # A list is used here so more than one account of a kind can exist.
    key = "viewers" if kind == "viewer" else "guards"
    single = cfg.get("viewer" if kind == "viewer" else "guard") or {}
    entries = cfg.get(key) or []
    if single.get("username", "").lower() == uname.lower():
        single["password"] = h
        cfg["viewer" if kind == "viewer" else "guard"] = single
    else:
        for e in entries:
            if str(e.get("username", "")).lower() == uname.lower():
                e["password"] = h
                break
        else:
            entries.append({"username": uname, "password": h})
        cfg[key] = entries
    who = uname

with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print(f"[OK] {kind} '{who}' stored as a hash in {path}")
PY

unset GG_HASH HASH

echo "Restarting $CONTAINER …"
docker restart "$CONTAINER" >/dev/null
sleep 4
echo ""
echo "Startup lines to check (should say this account is hashed):"
docker logs --tail 40 "$CONTAINER" 2>&1 | grep -E "^\[AUTH\]" || \
  echo "  (no [AUTH] lines yet — give it a few seconds and run: docker logs --tail 40 $CONTAINER | grep AUTH)"
echo ""
echo "A backup of the previous config is beside it as $CFG.bak.*"
