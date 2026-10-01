# WhatsApp sender switchover runbook

Moving every alert off the Twilio sandbox (`whatsapp:+14155238886`) onto the
production sender (`whatsapp:+918427590032`), and turning on the four
approved templates at the same time.

Written 1 Oct 2026. Register entries: OCT-86, OCT-96, OCT-128, OCT-129,
OCT-130, and OCT-04 for the restart trap.

---

## Why this is one change and not several

Freeform WhatsApp bodies work today **only because the sender is the
sandbox.** Joining the sandbox requires messaging it a join code, and that
join opens a 24-hour window. Every test phone has one, so freeform delivers.

The production sender has no join step and therefore no window. A freeform
body sent from it to a resident who has never messaged the business fails
with **Twilio error 63016** and nothing arrives.

So:

- Switch the number **without** setting the template SIDs → every alert
  breaks, silently, including visitor notifications with somebody standing
  at a gate.
- Set the template SIDs **without** switching the number → harmless but
  pointless; templates work on the sandbox too.

Do both in one change, verify, and have the rollback ready. Do not split it
across days.

---

## Preconditions — do not start until every line is true

- [ ] All four templates show **Approved** in the Twilio Content Template
      Builder, not "Pending" and not "Approved by Meta, pending Twilio".
- [ ] All four Content SIDs are in hand: SOS, escalation, brief, visitor.
- [ ] `visitor_notify.py` (OCT-128 / OCT-129) is committed and pushed.
- [ ] You have a **cold phone** — a WhatsApp number that has NOT messaged
      +918427590032 in the last 24 hours — that you are allowed to send a
      test message to. Without one, Step 6 cannot prove anything. See the
      note in Step 6 about why your own phone probably will not do.
- [ ] You have 30 uninterrupted minutes. Half of this change is verification
      and the verification is the part that matters.

---

## Step 0 — check the SIDs before you paste them anywhere

A Twilio Content SID is `HX` followed by **32 hexadecimal characters** —
34 characters in total. The code rejects anything else by shape (OCT-120),
which means a mistyped SID produces a *freeform* send rather than an error.
That is the quiet failure this whole runbook exists to avoid.

For each of the four, count the characters and confirm it starts `HX`. If
any one of them is not exactly 34 characters, stop and fix it in the Twilio
console before going further.

---

## Step 1 — record the rollback state

On the droplet:

```
sudo cp /opt/octa-ops/demo.env            /opt/octa-ops/demo.env.pre-switchover
sudo cp /opt/octa-ops/primera.env         /opt/octa-ops/primera.env.pre-switchover
sudo cp /opt/octa-ops/heartbeat_config.json /opt/octa-ops/heartbeat_config.json.pre-switchover
```

Then capture the container IDs, so Step 4 can prove the containers were
actually recreated rather than restarted:

```
sudo docker inspect -f '{{.Name}} {{.Id}}' octa-demo octa-primera
```

Keep that output. You are going to compare against it.

---

## Step 2 — edit the two env files

Use `nano` over SSH. **Do not use PowerShell `>>`** — it writes UTF-16 and
the container reads the file as binary (OCT-122).

```
sudo nano /opt/octa-ops/demo.env
```

Set or add these five lines. Format is strict: `KEY=value`, no quotes, no
spaces around the `=`, no trailing spaces. `docker --env-file` does not
strip quotes — it would make them part of the value.

```
TWILIO_WHATSAPP_FROM=whatsapp:+918427590032
WA_TPL_SOS=
WA_TPL_ESCALATION=
WA_TPL_BRIEF=
WA_TPL_VISITOR=
```

Paste each SID after its `=`. Save, then repeat for `primera.env` with the
same five lines and the same four SIDs — the templates are account-level,
not per-site.

Before saving, check there is no second `TWILIO_WHATSAPP_FROM` line further
down the file. A duplicate key in an env file means the last one wins, and
the one you edited may not be the last one.

---

## Step 3 — edit heartbeat_config.json (this is the step that gets skipped)

`site_heartbeat.py` runs on the **host**, not in a container, and takes its
sender from `cfg["twilio_from"]` in its JSON config. **It does not read the
environment at all** (OCT-130). Steps 2 and 4 will not touch it.

```
sudo nano /opt/octa-ops/heartbeat_config.json
```

Change `"twilio_from"` to `"whatsapp:+918427590032"`. Then check the file is
still valid JSON before you walk away from it:

```
python3 -c "import json;json.load(open('/opt/octa-ops/heartbeat_config.json'));print('valid json')"
```

Miss this step and every alert moves except the monitor — the one whose job
is to tell you a site has gone dark. It fails silently, because the
heartbeat only transmits when something is already wrong.

---

## Step 4 — deploy, and confirm it recreated

```
sudo /opt/octa/deploy.sh
```

`--env-file` is expanded into the container config at **create** time. A
`docker restart` re-runs the baked-in environment and your new lines are
invisible forever (OCT-04). `deploy_v2.sh` does `stop` + `rm` + `run`, so it
is a real recreate — but verify rather than trust:

```
sudo docker inspect -f '{{.Name}} {{.Id}}' octa-demo octa-primera
```

**Both IDs must differ from Step 1.** If either is unchanged, the env did
not reload and everything below will mislead you. Stop and find out why.

---

## Step 5 — verify the values landed, without printing them

```
sudo docker exec octa-demo python -c "
import os
for k in ('TWILIO_ACCOUNT_SID','TWILIO_AUTH_TOKEN','TWILIO_WHATSAPP_FROM',
          'WA_TPL_SOS','WA_TPL_ESCALATION','WA_TPL_BRIEF','WA_TPL_VISITOR'):
    v = os.environ.get(k,'')
    print('%-22s set=%-5s len=%-3d tail=%s' % (k, bool(v), len(v), v[-4:]))
"
```

What to look for:

- `TWILIO_WHATSAPP_FROM` tail is **0032**. If it is **8886** you are still
  on the sandbox and Step 2 did not take.
- All four `WA_TPL_*` show `len=34`. Any other length is a bad paste.
- `TWILIO_AUTH_TOKEN` shows `set=True`. The value is never printed.

Then confirm the modules *resolved* them, which is a different claim from
the environment holding them:

```
sudo docker exec octa-demo python -c "
import whatsapp_alerts as wa
print('CONFIG_LOADED', wa.CONFIG_LOADED)
print('from tail    ', wa.TWILIO_WHATSAPP_FROM[-4:])
print('templates ok ', {k: len(v)==34 for k,v in wa.WA_TEMPLATES.items()})
"
```

And the visitor module, which has its own credential path:

```
sudo docker exec octa-demo python -c "
import visitor_notify as vn
print('from tail', vn.TWILIO_WHATSAPP_FROM[-4:])
print('tpl ok   ', vn._template_sid_looks_real(vn.WA_TPL_VISITOR))
"
```

Expect `from tail 0032` and `tpl ok True`. You should also see no
`[VISITOR-NOTIFY] WARNING` line — if one appears, `notify_config.py` has got
into the image and is disagreeing with the environment (OCT-128). The
environment wins, so nothing is broken, but find out how that file got
there.

Repeat all three of these against `octa-primera`.

---

## Step 6 — the only test that proves anything

**A phone with an open 24-hour window will receive a freeform message
perfectly.** So testing on a phone you have just been messaging from proves
nothing at all: it cannot distinguish "the template works" from "the
template silently fell back to freeform and the window carried it".

This is the same error the register keeps recording — OCT-84, OCT-120,
OCT-124 — measuring the thing that is easy to look at instead of the thing
that decides.

Use a **cold** number. Put it against a test flat in the directory, then:

```
sudo docker exec -it octa-demo python visitor_notify.py <that-flat> "Test Courier"
```

The harness polls Twilio for the real delivery status. Read the output
carefully:

- `path=template` in the result line — the template was used. **If it says
  `path=freeform`, the SID was rejected by shape and the test is void.**
- `delivery status: delivered` — it reached the phone.
- `accepted`, `queued` or `sent` is **not** success. Those mean Twilio took
  the message. OCT-86 exists because those were once read as delivery.
- `error 63016` — freeform outside the window. The template did not engage.

Only `path=template` **and** `delivered` passes.

**Do not test with the panic button.** It fires real alerts to real
recipients. Same for the escalation path. The brief can be triggered safely
if you want a second data point.

---

## Step 7 — the heartbeat

The heartbeat is host cron and will pick up the JSON on its next run, but
it only sends when something is wrong, so waiting proves nothing. Force it:

```
sudo python3 /opt/octa-ops/site_heartbeat.py
```

Expect `OK` for both sites and no message. To prove the sender actually
works, the honest test is the one that worked for OCT-123 — stop a
container, confirm a real 🔴 BLIND arrives from the new number, start it
again, confirm 🟢 RESTORED:

```
sudo docker stop octa-primera && sudo python3 /opt/octa-ops/site_heartbeat.py
sudo docker start octa-primera && sudo python3 /opt/octa-ops/site_heartbeat.py
```

A monitor that has never been seen to fire from its current sender is not a
monitor.

---

## Rollback

If anything above fails and you need the sandbox back:

```
sudo cp /opt/octa-ops/demo.env.pre-switchover    /opt/octa-ops/demo.env
sudo cp /opt/octa-ops/primera.env.pre-switchover /opt/octa-ops/primera.env
sudo cp /opt/octa-ops/heartbeat_config.json.pre-switchover /opt/octa-ops/heartbeat_config.json
sudo /opt/octa/deploy.sh
```

Then re-run Step 5 and confirm the `from tail` reads **8886** again.

Rolling back is cheap and safe. A half-switched system — production sender,
no working templates — is the single worst state this stack can be in,
because it looks fine until a real resident needs a real message.

---

## After the switchover, but not part of it

- **Published contact number.** The Play listing and privacy policy publish
  the personal number 8847406740. Move both to 8427590032 — but only once
  Play review has completed. Editing a listing mid-review can restart the
  clock.
- **Messaging limit.** A newly verified portfolio starts at a 250-message
  rolling 24-hour cap. Twelve testers will not approach it; a real society
  of 200 flats with visitor alerts will. Watch it before the first paid
  rollout, not after.
- **OCT-130 properly.** The fix is to have `site_heartbeat.py` read
  `TWILIO_WHATSAPP_FROM` from the environment with `twilio_from` as a
  fallback, so one variable moves every sender on the box and Step 3 stops
  existing. `deploy_v2.sh` step [5/5] already installs this script from the
  repo, so the fix ships through the normal deploy.
- **Display name.** The sender shows "GuardianGrid", not "Defender Octa".
  Decide which one residents should see; the app installs as Octa Resident.

---

## Known limits of this runbook

It proves that a template-based message reaches one cold phone from the new
sender. It does not prove the template *text* is right for every variable
combination, that an empty variable renders acceptably (the code substitutes
an em dash so the send does not fail), or that the 250-message cap will hold
under a real society's traffic. Those are separate checks and this document
does not cover them.
