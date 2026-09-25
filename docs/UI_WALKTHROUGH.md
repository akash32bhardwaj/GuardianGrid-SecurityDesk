# Frontend walkthrough — the largest untested surface

Nine frontend files changed on 18/19 Sep and **not one has been clicked**.
They compile and I read them closely, but every claim about them is
reasoning, not observation. These are the screens a guard actually
touches.

Everything below is already live on demo (`55ec84f`). Nothing needs
deploying first.

Budget about an hour. Do it awake — a pass done tired produces "looked
fine", which is worse than not looking, and is how OCT-70 spent two days
marked as verified.

For each item: what to do, what you should see, and **what would mean it
is broken**. That third column is the point. Anything in it, stop and
write it down rather than working around it.

---

## 1. The panic button — OCT-70

The most important item on this list. It is the control that exists for
an emergency, and it has been reporting success while reaching nobody.

**Do:** press and hold the red button on the booth screen for 1.5s.

**Expect:** given WhatsApp now works and cameras do not, the likely result
is **amber**, reading `PARTLY SENT — CHECK`, with a panel naming the
channel that failed — most likely the booth voice, since no audio device
is attached. It should **not** clear on its own.

**Broken if:** it shows green `HELP DISPATCHED ✓` while any channel
failed. That is the original bug. Also broken if an amber or red state
disappears after a few seconds — a failure must stay on screen until
tapped.

**Then check the log:**

```bash
sudo docker logs --since 5m octa-demo 2>&1 | grep -E "PANIC|WA-VERIFY"
```

The `[PANIC]` line ends `status=all|partial|none`. About 30 seconds later
a `[WA-VERIFY]` line says whether the message actually reached the
handset — accepted and delivered are different things.

**Note:** this sends a real WhatsApp to your number and will speak aloud
if audio exists. Do not do it during a client demo.

---

## 2. Resident SOS — OCT-85

**Do:** from the resident app, raise an SOS on a test flat.

**Expect:** the message now reads *"SOS recorded and alerting started. If
nobody acknowledges within 3 minutes it escalates automatically."* A
WhatsApp should arrive.

**Broken if:** it still says *"guard alerted, 3-minute clock started"* —
that means the old bundle is being served, so check `index.html` against
the assets folder.

**Also worth watching:** leave it unacknowledged for 3 minutes and see
whether the escalation WhatsApp arrives. That path was dead until today
and has never been observed working.

---

## 3. The blacklist pill — OCT-60

**Do:** open Residents DB and look at the status column.

**Expect:** every row shows an uppercase status. A blacklisted vehicle
shows a red pill and has **no** block button (it is already blocked).

**Broken if:** any row shows a green pill next to a status that is not
KNOWN/RESIDENT, or a blacklisted row still offers a block control. The
original bug was a lowercase `blacklisted` row rendering green *and*
keeping its block button — the screen saying the car was fine while
offering to block a car it thought was fine.

---

## 4. Gate console status badges — OCT-60

**Do:** look up a known plate, then an unknown one, in the gate console.

**Expect:** a resident reads RESIDENT in green. An unknown reads UNKNOWN
in grey. An unrecognised status shows **its own raw name**, not the word
UNKNOWN.

**Broken if:** a registered resident shows as UNKNOWN. Before the fix,
`RESIDENT` and `APPROVED` both fell through to UNKNOWN — a resident's own
car presented to the guard as an unknown vehicle.

---

## 5. The Access Mix donut — OCT-79, OCT-61

**Do:** look at the dashboard donut and its footer.

**Expect:** no two legend rows with the same label. The footer says
`(residents, visitors, approved)`. On demo's current data the verified
figure should be around **75%**.

**Broken if:** you see multiple slices labelled "Unknown", or a verified
figure near 50%. Both were true before: three separate legend lines all
reading Unknown, and APPROVED vehicles counted as unverified.

---

## 6. Time columns — OCT-67, OCT-82

**Do:** look at the vehicle log's Time column and the alerts feed.

**Expect:** short times like `14:32`.

**Broken if:** you see a raw `2026-09-19T14:32:07`, the literal text
`Invalid Date`, or `12:00 am` on a row that has no real timestamp. That
last one is the dangerous case — a missing time rendering as a plausible
overnight event.

---

## 7. Vehicle log paging — OCT-57

**Do:** open the vehicle log and read the header count.

**Expect:** it reflects the real history, now roughly 1,874 events.

**Broken if:** it says exactly 200. That was the old symptom on every
site forever, because the header counted the length of a capped response.

---

## 8. Resident export and template — OCT-73

**Do:** click both download buttons on the Residents page.

**Expect:** two files download. The export has 12 rows today.

**Broken if:** either shows an HTML error page or a 500. Both are already
verified working at the code level, so a failure here points at the route
wiring rather than the fix.

---

## 9. Activity timeline — OCT-63

**Do:** look at the Activity panel. Then restart the container and look
again.

```bash
sudo docker restart octa-demo
```

**Expect:** rows both times. That is the whole fix — it used to serve
from process memory and was empty after every restart.

**Broken if:** empty after the restart. To a guard an empty timeline
reads as "nothing happened" rather than "this panel lost its memory".

---

## 10. The two checks that need a browser session

Only doable logged in, and neither is reachable from `docker exec`:

- **OCT-10** — DevTools → Network → `/api/stream`. Content type should be
  `text/event-stream`. If it is `text/html`, the Cloudflare WAF is still
  blocking it and the next step is Security → Events. The JWT is no
  longer in that URL (it uses an opaque ticket now), so if a WAF rule was
  matching the token pattern, this may already be resolved.

- **OCT-56** — log in as a **viewer** and run a search. It timed out at
  20 seconds before the thread-pool changes. If it returns promptly,
  OCT-56 closes.

---

## Afterwards

Anything in a "broken if" column becomes a new finding. Anything that
behaves as expected gets noted in the register as **observed**, which is
a stronger word than anything currently written about these screens.

And the honest framing to keep: this list covers the changes made on
18/19 Sep. It is not a test of the product. The ~15 unrun test points
from the original pass — EVT-05, PWA-03/05/06, AUTH-04/05/06/11,
CFG-01/04/06/07/08 — are still outstanding, and four of those are on the
authentication path.
