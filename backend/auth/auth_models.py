"""
auth_models.py — the account list this application actually authenticates against
=================================================================================

OCT-93. Credentials for every site live in `site_config.json`, and until now
they lived there IN PLAIN TEXT. This module read them and called
`generate_password_hash()` at import, so the bcrypt-strength hashing was
real — it simply happened in memory, to a value stored in the clear on disk.

The comment at `api_server.py:44` said the opposite:

    #  credentials live as bcrypt hashes in site_config.json.

That comment described a security property the code did not have, which is
worse than no comment, because it stops the next person checking.

WHAT WAS ACTUALLY EXPOSED

`site_config.json` is gitignored, so none of this reached git history — that
is the one piece of good news and it is a real one. What remained: the admin
password for every site, readable on the droplet, in a file mounted into a
container, in a folder that gets backed up. Anyone with droplet access,
anyone holding a backup, and any process inside that container could read it.
Per-site, so it is not one password but one per society onboarded.

WHAT THIS CHANGE DOES, AND WHAT IT DELIBERATELY DOES NOT

It does NOT hash the stored values. `generate_password_hash` applied to an
already-hashed string produces a hash of the hash, and the account is then
unreachable — a lockout on the most privileged login in the product.

Instead, a value that is ALREADY a werkzeug hash is passed through untouched,
and a plain one is hashed as before. So:

  * every existing site keeps working, unchanged, with no migration
  * a site can move to stored hashes whenever you choose, one at a time
  * new sites can be provisioned with hashes from the start

A NOTE ON FORMAT, because getting this wrong is silent

Verification is `werkzeug.security.check_password_hash`, which understands
werkzeug's own formats — `pbkdf2:sha256:...$salt$hash`, `scrypt:...$salt$hash`.
It does NOT understand a raw bcrypt `$2b$...` string. A bcrypt hash dropped
into site_config.json would look hashed, be passed through by any naive
check, and then fail every login with "invalid credentials" — indistinguish-
able from a wrong password. So bcrypt-shaped values are detected and
announced loudly rather than quietly accepted.

To generate a storable hash, on the droplet, without the password passing
through anything else:

    sudo docker exec -it octa-demo python3 -c \\
      "from werkzeug.security import generate_password_hash as g; \\
       import getpass; print(g(getpass.getpass('password: ')))"

Paste the output into site_config.json in place of the plain password and
restart. The startup summary below will confirm the account is now stored
hashed.

VERIFIABILITY

OCT-42 made the same point about Twilio credentials: "moved to env files"
was something you believed rather than something you could check, until the
startup line said which source each credential came from. The same applies
here. Every run now prints how many accounts are stored hashed and how many
are still plain text, and names the plain ones — so "I moved the passwords"
becomes a thing the running system tells you.
"""

import sys

from werkzeug.security import generate_password_hash
from site_config import CONFIG


# ── Credential storage ───────────────────────────────────────────────

# Methods werkzeug's check_password_hash can verify. A stored value starting
# with one of these, followed by ':' or '$', is taken as already hashed.
_WERKZEUG_METHODS = ("pbkdf2", "scrypt", "argon2")

# Tracks what each account's password was stored as, for the summary.
_STORED_PLAIN: list = []
_STORED_HASHED: list = []
_STORED_BAD: list = []


def _looks_hashed(value: str) -> bool:
    """True when this is a werkzeug hash we can verify as-is."""
    v = (value or "").strip()
    if "$" not in v:
        return False
    head = v.split("$", 1)[0].lower()
    method = head.split(":", 1)[0]
    return method in _WERKZEUG_METHODS


def _looks_bcrypt(value: str) -> bool:
    """A raw bcrypt string. Looks hashed, cannot be verified by werkzeug."""
    return (value or "").strip().startswith(("$2a$", "$2b$", "$2y$"))


def _shout(title: str, *lines: str) -> None:
    width = 74
    print("\n" + "=" * width, file=sys.stderr)
    print(f"  {title}", file=sys.stderr)
    print("=" * width, file=sys.stderr)
    for line in lines:
        print(f"  {line}", file=sys.stderr)
    print("=" * width + "\n", file=sys.stderr, flush=True)


def _password_hash(value: str, who: str) -> str:
    """Hash a plain password, or pass an existing werkzeug hash through.

    This is the whole of OCT-93's fix. The value in site_config.json may be
    either, and which one it is decides whether the password sits readable
    on disk. Nothing here changes the file — it changes what the file is
    ALLOWED to contain, so the migration can happen site by site.
    """
    v = (value or "")

    if _looks_hashed(v):
        _STORED_HASHED.append(who)
        return v

    if _looks_bcrypt(v):
        # Accepted as a hash so it is never treated as a password, but this
        # account cannot log in and the operator has to be told why. Silent
        # acceptance here would present as "wrong password" forever.
        _STORED_BAD.append(who)
        _shout(f"UNSUPPORTED PASSWORD HASH FOR '{who}' - THIS ACCOUNT CANNOT LOG IN",
               "The stored value is a raw bcrypt hash ($2b$...). This app",
               "verifies with werkzeug's check_password_hash, which does not",
               "understand that format, so every login attempt will be",
               "rejected as if the password were wrong.",
               "",
               "Replace it with a werkzeug hash:",
               "  python3 -c \"from werkzeug.security import generate_password_hash"
               " as g; import getpass; print(g(getpass.getpass()))\"",
               "",
               "Or put the plain password back - it will be hashed at startup",
               "as before, which is what every other site is doing today.")
        return v

    _STORED_PLAIN.append(who)
    return generate_password_hash(v)


USERS = [
    {
        "id": 1,
        "username": CONFIG.admin_username,
        "password_hash": _password_hash(CONFIG.admin_password,
                                        CONFIG.admin_username),
        "role": "SUPER_ADMIN",
        "society_id": CONFIG.site_id
    }
]


def _site_config_path():
    """site_config.json, wherever this deployment keeps it.

    /app in the container (this file is /app/backend/auth/auth_models.py, so
    parents[2] is /app), and the entrypoint also links it into /data, which
    is the working directory the app runs from.
    """
    from pathlib import Path
    candidates = [
        Path(__file__).resolve().parents[2] / "site_config.json",
        Path.cwd() / "site_config.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _add_viewers_from_config():
    """Read-only demo accounts, defined in site_config.json.

    Two supported shapes (both may be present; every valid entry is added):

        "viewer":  { "username": "demo", "password": "..." }           # legacy single
        "viewers": [ { "username": "demo-escon", "password": "..." },
                     { "username": "demo-aura",  "password": "..." } ] # per-prospect

    Viewers can look at dashboards, cameras, reports and replay, but the API
    guard refuses every write and the resident directory, and personal data
    is redacted from their responses. Safe to print on a card or send to a
    prospect over WhatsApp. No block = no viewers.

    NOTE ON HISTORY: the multi-viewer version of this lived for weeks in an
    untracked auth_models.py at the repo root — a file nothing imported and
    git never saw. This module, the one actually imported, only ever
    understood the single "viewer" block, so a site_config.json carrying a
    "viewers" list silently produced no accounts at all. If per-prospect
    demo logins ever seemed not to exist, that is why.

    OCT-93: a viewer password is the one most likely to be handed around —
    printed on a card, sent over WhatsApp to a prospect — so it is also the
    one most worth storing hashed once you have somewhere to keep the plain
    copy.
    """
    import json
    try:
        with open(_site_config_path(), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"[AUTH] viewer accounts skipped: {e}")
        return

    entries = []
    single = cfg.get("viewer") or {}
    if single.get("username") and single.get("password"):
        entries.append(single)
    for v in cfg.get("viewers") or []:
        if v.get("username") and v.get("password"):
            entries.append(v)

    next_id = 2
    seen = {USERS[0]["username"].lower()}
    for v in entries:
        uname = v["username"]
        if uname.lower() in seen:
            print(f"[AUTH] duplicate viewer '{uname}' skipped")
            continue
        seen.add(uname.lower())
        USERS.append({
            "id": next_id,
            "username": uname,
            "password_hash": _password_hash(v["password"], uname),
            "role": "VIEWER",
            "society_id": CONFIG.site_id,
        })
        print(f"[AUTH] viewer account enabled: {uname} (read-only)")
        next_id += 1

    if not entries:
        print("[AUTH] no viewer accounts configured for this site")


def _add_guards_from_config():
    """Gate-operator accounts, defined in site_config.json.

    OCT-87. Until now this system had exactly two roles: SUPER_ADMIN and
    VIEWER. VIEWER is refused every non-GET request, so a viewer cannot
    take a gate decision, correct a plate, admit a visitor or press panic.
    Which meant the booth screen had to run as SUPER_ADMIN — the guard on
    shift holding rights to change site settings, manage accounts,
    blacklist vehicles and export the entire resident directory, with
    every action in the log attributed to "admin".

    Three problems in one: no least privilege on the account most exposed
    to a physical space; no attribution across a shift change, so no gate
    decision can be traced to a person; and a shared credential on a
    device that guards rotate through, which is hard to defend under the
    DPDP Act for a system holding residents' names, flats and movements.

    Same two shapes as viewers, so one account or one per guard:

        "guard":  { "username": "gate1", "password": "..." }
        "guards": [ { "username": "ramesh", "password": "..." },
                    { "username": "sunita", "password": "..." } ]

    PER-GUARD LOGINS ARE THE POINT. A single shared "guard" account fixes
    the privilege problem and leaves the attribution problem exactly where
    it was, one level down. The list form exists so a site can give each
    guard their own, and that is what an incident review needs.

    THIS IS INERT UNTIL CONFIGURED. A site_config.json with no guard block
    produces no guard accounts and changes nothing about how that site
    behaves today.
    """
    import json
    try:
        with open(_site_config_path(), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"[AUTH] guard accounts skipped: {e}")
        return

    entries = []
    single = cfg.get("guard") or {}
    if single.get("username") and single.get("password"):
        entries.append(single)
    for g in cfg.get("guards") or []:
        if g.get("username") and g.get("password"):
            entries.append(g)

    if not entries:
        print("[AUTH] no guard accounts configured — the booth must sign in "
              "as admin on this site (OCT-87)")
        return

    seen = {u["username"].lower() for u in USERS}
    next_id = max(u["id"] for u in USERS) + 1
    for g in entries:
        uname = g["username"]
        if uname.lower() in seen:
            print(f"[AUTH] duplicate guard '{uname}' skipped")
            continue
        seen.add(uname.lower())
        USERS.append({
            "id": next_id,
            "username": uname,
            "password_hash": _password_hash(g["password"], uname),
            "role": "GUARD",
            "society_id": CONFIG.site_id,
        })
        print(f"[AUTH] guard account enabled: {uname}")
        next_id += 1


def _report_credential_storage():
    """Say out loud how this site's credentials are stored.

    OCT-42's lesson, applied to passwords: a claim you cannot check is a
    claim you are believing. One line per run, so "the passwords are
    hashed now" stops being a memory and becomes an observation.

    Never prints a password, a hash, or any part of either.
    """
    total = len(_STORED_PLAIN) + len(_STORED_HASHED) + len(_STORED_BAD)
    if not total:
        return
    print(f"[AUTH] credential storage: {len(_STORED_HASHED)} hashed, "
          f"{len(_STORED_PLAIN)} plain text, {len(_STORED_BAD)} unusable "
          f"(of {total})")
    if _STORED_PLAIN:
        print(f"[AUTH] stored in PLAIN TEXT in site_config.json: "
              f"{', '.join(_STORED_PLAIN)} — readable by anyone with droplet "
              f"or backup access (OCT-93)", file=sys.stderr)


_add_viewers_from_config()
_add_guards_from_config()
_report_credential_storage()


def get_user_by_username(username):
    for user in USERS:
        if user["username"].lower() == username.lower():
            return user
    return None
