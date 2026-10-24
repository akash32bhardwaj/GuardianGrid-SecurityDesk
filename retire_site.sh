#!/usr/bin/env bash
# ============================================================================
# retire_site.sh — DEFENDER OCTA: take a client site out of service, safely
# ----------------------------------------------------------------------------
# Usage:
#   sudo /opt/octa-ops/retire_site.sh [--dry-run] <slug>
#   sudo /opt/octa-ops/retire_site.sh --purge <slug>      (deletes the data)
#
# OCT-181. Retiring a site was three edits in three places that nobody wrote
# down, so it was done from memory and the third one was missed. Escon came
# out of sites.conf on 21 Oct and stayed in heartbeat_config.json, and the
# monitor went on sending "BLIND -- Escon Primera: service not answering"
# every six hours for two more days. Every one of those alerts was CORRECT
# and every one was useless, which is the worst kind: it teaches you to
# ignore the monitor, and then you ignore the real one.
#
# A site exists in NINE places. This script handles the six it can reach and
# prints the three it cannot:
#
#   automatic                             manual (printed at the end)
#   1. sites.conf line                    7. Cloudflare tunnel hostname
#   2. docker container octa-<slug>       8. societies.json on the picker site
#   3. heartbeat_config.json sites[]      9. the client's own Pi, if installed
#   4. heartbeat_state.json entry
#   5. a final backup, taken first
#   6. /opt/societies/<slug>{,-config}, /opt/octa-ops/<slug>.env  (--purge)
#
# THE DEFAULT DOES NOT DELETE ANYTHING. A client leaving is not the same as
# a client's data being worthless: disputes, unpaid invoices, a committee
# that changes its mind in March, and DPDP retention all argue for keeping
# it until somebody decides otherwise ON PURPOSE. --purge is that decision,
# made separately, and it refuses to run unless a verified off-site backup
# exists.
# ============================================================================
set -uo pipefail

OPS="/opt/octa-ops"
SITES_CONF="${SITES_CONF:-$OPS/sites.conf}"
HB_CONF="${HB_CONF:-$OPS/heartbeat_config.json}"
HB_STATE="${HB_STATE:-$OPS/heartbeat_state.json}"
BACKUP_ALL="${BACKUP_ALL:-$OPS/backup_all_sites.sh}"

DRY=0; PURGE=0; NOBACKUP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)    DRY=1; shift ;;
    --purge)      PURGE=1; shift ;;
    --no-backup)  NOBACKUP=1; shift ;;
    -h|--help)    sed -n '2,34p' "$0"; exit 0 ;;
    -*)           echo "unknown option: $1" >&2; exit 2 ;;
    *)            break ;;
  esac
done

SLUG="${1:-}"
if [ -z "$SLUG" ]; then
  echo "Usage: sudo $0 [--dry-run] [--purge] <slug>" >&2
  exit 2
fi
if ! echo "$SLUG" | grep -qE '^[a-z0-9-]+$'; then
  echo "slug must be lowercase letters/digits/hyphens: $SLUG" >&2
  exit 2
fi
if [ "$DRY" = 0 ] && [ "$(id -u)" != "0" ]; then
  echo "must run as root (sudo) -- it edits $OPS and stops containers" >&2
  exit 2
fi

CONTAINER="octa-$SLUG"
DATA="/opt/societies/$SLUG"
CFG="/opt/societies/${SLUG}-config"
ENVF="$OPS/$SLUG.env"
STAMP="$(date +%Y%m%d-%H%M%S)"

run() { if [ "$DRY" = 1 ]; then echo "  [dry-run] $*"; else eval "$*"; fi }

echo "── Retiring: $SLUG ──"
[ "$DRY" = 1 ] && echo "   (dry run: nothing will be changed)"

# ── 0) is this site actually known? ─────────────────────────────────────────
# Refusing an unknown slug matters more than it looks: the common typo is a
# neighbouring site's name, and the next steps stop containers.
LINE="$(grep -E "^[[:space:]]*${SLUG}\|" "$SITES_CONF" 2>/dev/null | head -1)"
if [ -z "$LINE" ]; then
  if grep -qE "^[[:space:]]*#.*${SLUG}\|" "$SITES_CONF" 2>/dev/null; then
    echo "  ·  already retired in sites.conf (commented out)"
  else
    echo "!! $SLUG is not in $SITES_CONF" >&2
    echo "   active sites:" >&2
    grep -vE '^[[:space:]]*(#|$)' "$SITES_CONF" 2>/dev/null \
      | cut -d'|' -f1 | sed 's/^/     /' >&2
    exit 1
  fi
fi

# ── 1) final backup, BEFORE anything is deregistered ────────────────────────
# Order is the whole point. backup_all_sites.sh reads sites.conf, so the
# moment this site is commented out it stops being backed up -- and the one
# backup you most want is the last one.
if [ "$NOBACKUP" = 1 ]; then
  echo "  ⚠️  --no-backup: skipping the final backup (you asked for this)"
elif [ -n "$LINE" ]; then
  echo "── final backup ──"
  if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
    echo "!! $CONTAINER is not running, so it cannot be backed up." >&2
    echo "   Start it and re-run, or pass --no-backup if the data is already" >&2
    echo "   saved elsewhere and you know it." >&2
    exit 1
  fi
  TMPCONF="$(mktemp)"
  printf '%s\n' "$LINE" > "$TMPCONF"
  if [ "$DRY" = 1 ]; then
    echo "  [dry-run] SITES_CONF=<just $SLUG> $BACKUP_ALL 30"
  elif SITES_CONF="$TMPCONF" "$BACKUP_ALL" 30; then
    echo "  ✅ final backup taken and pushed off-site"
  else
    rm -f "$TMPCONF"
    echo "!! final backup FAILED -- nothing has been changed." >&2
    echo "   Fix the backup first. Retiring a site whose last backup failed" >&2
    echo "   is how data disappears quietly." >&2
    exit 1
  fi
  rm -f "$TMPCONF"
fi

PORT="$(printf '%s' "$LINE" | cut -d'|' -f2 | tr -d ' ')"

# ── 2) stop and remove the container ────────────────────────────────────────
if docker inspect "$CONTAINER" >/dev/null 2>&1; then
  run "docker rm -f '$CONTAINER'"
  [ "$DRY" = 0 ] && echo "  ✅ container $CONTAINER removed"
else
  echo "  ·  no container named $CONTAINER"
fi

# ── 3) comment the sites.conf line out, never delete it ─────────────────────
# The convention is already in backup_all_sites.sh: "A retired site is a '#'
# line". Keeping the line keeps the record of the port, the paths and the
# fact that this site ever existed -- which is what you need in six months
# when somebody asks why port 5009 is free.
if [ -n "$LINE" ]; then
  if [ "$DRY" = 1 ]; then
    echo "  [dry-run] comment out: $LINE"
  else
    cp -a "$SITES_CONF" "$SITES_CONF.bak-$STAMP"
    python3 - "$SITES_CONF" "$SLUG" "$STAMP" <<'PY'
import io, re, sys
path, slug, stamp = sys.argv[1], sys.argv[2], sys.argv[3]
src = io.open(path, encoding="utf-8").read()
out, done = [], 0
for ln in src.splitlines():
    if re.match(rf"^\s*{re.escape(slug)}\|", ln):
        out.append(f"# retired {stamp[:8]} :: {ln}")
        done += 1
    else:
        out.append(ln)
io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
print(f"  ✅ sites.conf: {done} line(s) commented out")
PY
  fi
fi

# ── 4) heartbeat: config entry AND state entry ──────────────────────────────
# This is the step that was missed. Matching is by the site's DATA PATH, not
# by display name: the heartbeat entry carries "Defender Octa Demo" while
# sites.conf carries "demo", and matching on a human-typed name is how you
# retire the wrong society.
#
# The config file holds live Twilio credentials. It is rewritten in place
# with a 600-mode backup beside it, and nothing from it is ever printed.
if [ -f "$HB_CONF" ]; then
  if [ "$DRY" = 1 ]; then
    echo "  [dry-run] remove $SLUG from heartbeat_config.json + state"
  else
    cp -a "$HB_CONF" "$HB_CONF.bak-$STAMP" && chmod 600 "$HB_CONF.bak-$STAMP"
    python3 - "$HB_CONF" "$HB_STATE" "$SLUG" "$DATA" "$PORT" <<'PY'
import io, json, os, sys
conf_path, state_path, slug, data_dir, port = (list(sys.argv[1:6]) + [""])[:5]

with io.open(conf_path, encoding="utf-8") as f:
    cfg = json.load(f)

root = data_dir.rstrip("/")


def belongs(s):
    """Does this heartbeat entry describe the site being retired?

    Substring matching is what the first version of this did, and a test
    caught it removing the wrong society: "/opt/societies/demo" IS a
    substring of "/opt/societies/demo2/guardiangrid.db", so retiring demo
    silently un-monitored demo2. Every comparison here is anchored.

    The display name is deliberately NOT a key. sites.conf says "demo"
    while the monitor says "Defender Octa Demo", and matching on text a
    human typed is how you retire somebody else's society.
    """
    db = str(s.get("db") or "")
    if db == root or db.startswith(root + "/"):
        return True
    # A site with no db (uptime-only, as Escon was) is identified by its
    # port, which is unique per site and comes from the sites.conf line.
    if port and (":" + port + "/") in str(s.get("url") or ""):
        return True
    return str(s.get("slug") or "") == slug


kept, dropped_names = [], []
for s in cfg.get("sites", []):
    if belongs(s):
        dropped_names.append(s.get("name", "?"))
    else:
        kept.append(s)

if dropped_names:
    cfg["sites"] = kept
    tmp = conf_path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, conf_path)
    print("  ✅ heartbeat_config.json: removed " + ", ".join(dropped_names))
else:
    print("  ⚠️  heartbeat_config.json: NO matching site found.")
    print("     This is exactly how Escon kept alerting after it left --")
    print("     open the file and check by hand before calling this done.")

# The state file keys on the DISPLAY NAME. A stale entry there is not
# harmless: a site left in state as DOWN can emit a "RESTORED" long after
# it stopped existing.
if dropped_names and os.path.exists(state_path):
    try:
        with io.open(state_path, encoding="utf-8") as f:
            st = json.load(f)
        gone = [n for n in dropped_names if n in st]
        for n in gone:
            st.pop(n, None)
        if gone:
            with io.open(state_path, "w", encoding="utf-8") as f:
                json.dump(st, f, indent=2, ensure_ascii=False)
            print(f"  ✅ heartbeat_state.json: cleared {len(gone)} entry(ies)")
    except (OSError, ValueError) as e:
        print(f"  ⚠️  heartbeat_state.json not updated: {e}")
PY
  fi
else
  echo "  ·  no $HB_CONF on this host"
fi

# ── 5) data: kept by default, deleted only on purpose ───────────────────────
if [ "$PURGE" = 1 ]; then
  echo ""
  echo "── --purge: this DELETES the client's data, permanently ──"
  echo "   $DATA"
  echo "   $CFG"
  echo "   $ENVF"
  echo ""
  echo "   The final backup above is the only remaining copy."
  if [ "$DRY" = 1 ]; then
    echo "  [dry-run] would delete the three paths above"
  else
    printf "   Type the slug (%s) to confirm, anything else to abort: " "$SLUG"
    read -r CONFIRM
    if [ "$CONFIRM" != "$SLUG" ]; then
      echo "  ·  aborted -- data left in place"
    else
      for p in "$DATA" "$CFG" "$ENVF"; do
        [ -e "$p" ] && rm -rf "$p" && echo "  ✅ deleted $p"
      done
    fi
  fi
else
  echo ""
  echo "  ·  data KEPT (default). To delete it later:"
  echo "       sudo $0 --purge $SLUG"
fi

if [ "$DRY" = 1 ]; then
  HEADLINE="[dry-run] NOTHING WAS CHANGED. The above is what would happen."
  REVERSIBLE="Run again without --dry-run to do it."
else
  HEADLINE="✅ $SLUG retired.${PORT:+  Port $PORT is now free.}"
  REVERSIBLE="Reversible: the sites.conf line is commented, not deleted, and
$HB_CONF.bak-$STAMP holds the monitor's previous state."
fi

# ── 6) what this script cannot reach ────────────────────────────────────────
cat <<EOF

══════════════════════════════════════════════════════════════
$HEADLINE

STILL MANUAL -- nothing here can do these for you:
  1. Cloudflare tunnel: remove the public hostname
       $SLUG.snguardiangrid.com
     Until you do, the subdomain resolves to a dead port.
  2. Resident app: remove this site from societies.json on the
     picker site, or residents keep seeing it in the list.
  3. The client's Pi, if one was installed: it will keep trying
     to reach a container that no longer exists.

$REVERSIBLE
══════════════════════════════════════════════════════════════
EOF
