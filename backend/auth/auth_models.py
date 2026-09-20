from werkzeug.security import generate_password_hash
from site_config import CONFIG

USERS = [
    {
        "id": 1,
        "username": CONFIG.admin_username,
        "password_hash": generate_password_hash(CONFIG.admin_password),
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
            "password_hash": generate_password_hash(v["password"]),
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
            "password_hash": generate_password_hash(g["password"]),
            "role": "GUARD",
            "society_id": CONFIG.site_id,
        })
        print(f"[AUTH] guard account enabled: {uname}")
        next_id += 1


_add_viewers_from_config()
_add_guards_from_config()


def get_user_by_username(username):
    for user in USERS:
        if user["username"].lower() == username.lower():
            return user
    return None
