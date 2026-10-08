"""OCT-143 addendum: the admit UPDATE must leave no row that is both
admitted and still showing the resident's pre-admit answer.

The schema is EXTRACTED from resident_app.py, not written here -- an invented
fixture is how a previous test passed against a table production does not have.
"""
import os, re, sqlite3, io, sys

SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "resident_app.py")
src = io.open(SRC, encoding="utf-8").read()

# 1. real CREATE TABLE, lifted verbatim
m = re.search(r"(CREATE TABLE IF NOT EXISTS arrival_requests \(.*?\);)", src, re.S)
assert m, "could not find arrival_requests schema in source"
ddl = m.group(1)

# 2. the columns the PRAGMA migration adds at boot
m2 = re.search(r"acols = \{r\[1\] for r in con\.execute\(\"PRAGMA table_info\(arrival_requests\)\"\)\}(.*?)\n\n", src, re.S)
extra = re.findall(r"[\"'](admitted_by|override_reason)[\"']", m2.group(1) if m2 else "")
extra = sorted(set(extra))
assert extra == ["admitted_by", "override_reason"], f"migration columns changed: {extra}"

# 3. the actual UPDATE statement, lifted verbatim from the handler
m3 = re.search(r'cur = con\.execute\(\n((?:\s*"[^"]*",?\n)+)\s*\(_now_str\(\)', src)
assert m3, "could not find the admit UPDATE"
stmt = "".join(re.findall(r'"([^"]*)"', m3.group(1)))
assert "arrival_requests" in stmt and "admitted_at IS NULL" in stmt, stmt
print("UPDATE under test:\n  " + stmt + "\n")

con = sqlite3.connect(":memory:")
con.row_factory = sqlite3.Row
con.execute(ddl)
for c in extra:
    con.execute(f"ALTER TABLE arrival_requests ADD COLUMN {c} TEXT")

# every status a row can hold when the guard presses Admit.
# DENY/DECLINED is excluded deliberately: the handler refuses it before
# reaching this statement.
cases = [("PENDING", None), ("APPROVED", "ALLOW"),
         ("WAITING", "WAIT"), ("EXPIRED", None)]
for st, dec in cases:
    con.execute("INSERT INTO arrival_requests (flat_no, visitor_name, status, decision) "
                "VALUES (?,?,?,?)", ("A-101", f"V-{st}", st, dec))
con.commit()

fails = []
for row in con.execute("SELECT id, status FROM arrival_requests").fetchall():
    cur = con.execute(stmt, ("2026-10-09 02:30:00", 7, "guard1", "reason", row["id"]))
    if cur.rowcount != 1:
        fails.append(f"id {row['id']} ({row['status']}): first admit won {cur.rowcount} rows, want 1")
con.commit()

for row in con.execute("SELECT * FROM arrival_requests").fetchall():
    if row["admitted_at"] and row["status"] != "ADMITTED":
        fails.append(f"{row['visitor_name']}: admitted_at set but status={row['status']!r}")
    if row["decision"] is not None and not row["decision"]:
        fails.append(f"{row['visitor_name']}: decision was wiped")

# idempotency: a second tap must win nothing
for row in con.execute("SELECT id FROM arrival_requests").fetchall():
    cur = con.execute(stmt, ("2026-10-09 02:31:00", 8, "guard2", "again", row["id"]))
    if cur.rowcount != 0:
        fails.append(f"id {row['id']}: second admit won {cur.rowcount} rows, want 0")

# the resident's answer must still be readable afterwards
got = {r["visitor_name"]: r["decision"] for r in
       con.execute("SELECT visitor_name, decision FROM arrival_requests")}
if got.get("V-WAITING") != "WAIT":
    fails.append(f"WAIT decision not preserved: {got.get('V-WAITING')!r}")
if got.get("V-APPROVED") != "ALLOW":
    fails.append(f"ALLOW decision not preserved: {got.get('V-APPROVED')!r}")

print(f"rows checked: {len(cases)}")
for r in con.execute("SELECT visitor_name, status, decision, override_reason FROM arrival_requests"):
    print(f"  {r['visitor_name']:12s} status={r['status']:9s} decision={str(r['decision']):5s} reason={r['override_reason']}")
print()
if fails:
    print("FAIL")
    for f in fails: print("  -", f)
    sys.exit(1)
print("PASS - no row is both admitted and labelled with its pre-admit answer")

# Run with no argument to test the working tree. Pass a path to test any
# other copy -- e.g. `git show HEAD~1:resident_app.py > /tmp/old.py` -- which
# is how this test was confirmed to FAIL on the pre-fix code rather than
# merely pass on the fixed code. A test that has never been seen to fail
# has not been shown to test anything.
