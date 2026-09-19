# Tier decisions — the input OCT-08 needs

**This is a proposal, not a specification.** The lists in
`site_profile.py` are mine; you have not approved them. Wiring feature
gates from an unapproved list is how a paying client loses a screen they
were using and finds out before you do.

Work down the table, change what is wrong, and when the **Decision**
column is filled in I will wire it. Nothing is gated until then.

---

## Read this before the table

**Today nothing is enforced.** `@feature_required` guards exactly one
route (`contractor_passes`) and no React component imports `FeatureGate`.
Every site — whatever it pays — gets the whole product. So every entry
below is a decision to *take something away* from somebody, and the
question for each one is not "which tier does this belong to" but "am I
willing to remove this from a Watch-tier client who has it today".

**Check the live sites before anything is switched on.** A site whose
`site_config.json` has no `tier` now falls back to the full product with a
loud warning (OCT-69), so nothing breaks by accident. But if you set tiers
carelessly, a client loses features the same afternoon. Confirm what
Escon Primera and any other live site are actually on first.

**Each tier contains the one below.** Upgrading never removes anything.
The three steps are meant to be one loop, not three bundles:

| tier | the promise |
|---|---|
| **Watch** | *detect* — the site is read and you are told what happened |
| **Guard** | *verify* — a guard can close the loop on what was detected |
| **Command** | *prove* — the closed loop leaves evidence someone else accepts |

---

## The table

For each feature: what it is, where I would put it, and — the column that
actually matters — what it costs you if the placement is wrong.

### Proposed WATCH (7)

| feature | what it is | why Watch | risk if wrong |
|---|---|---|---|
| `anpr` | plate recognition at the gate | it *is* detection; without it Watch is a camera | none — it cannot sit higher |
| `whatsapp_alerts` | outbound alerts to guard/ops | how "you are told what happened" actually reaches a person | gating it higher makes Watch silent, which is not a product |
| `morning_brief` | daily 07:35 summary | the committee-facing artefact that makes a society renew | higher = the buyer never sees value between incidents |
| `resident_directory` | flat → resident/phone | the resident app needs it, and the resident app is in every tier | higher = no resident app at Watch |
| `visitor_management` | log visitors in/out | table stakes; every competitor has it | higher = you lose deals on the feature list |
| `flat_visitor_notifications` | "your visitor is at the gate" | the thing residents actually feel daily | higher = residents never engage, renewal suffers |
| `bulk_resident_import` | onboarding spreadsheet | you cannot onboard without it | higher = onboarding needs your manual help |

**The deliberate call here:** the resident app sits in *every* tier. A
Watch site with no resident app is a camera the committee never sees the
value of. That is a commercial judgement, not a technical one — push back
if you disagree.

### Proposed GUARD (5)

| feature | what it is | why Guard | risk if wrong |
|---|---|---|---|
| `guard_decision_flow` | Entry/Hold/Exit at the console | the literal definition of *verify* | lower = you give away the tier's whole reason |
| `dvr_recording` | continuous recording | verification needs something to look at | lower = storage costs on a Watch price |
| `pdf_reports` | on-demand reports | proof-lite; the committee asks for these | lower = weakens Command's pitch |
| `contractor_passes` | contractor entry passes | a verify-loop feature; the only one gated today | lower = the one gate you have stops meaning anything |
| `security_score` | live site score | summarises the verify loop | **arguable** — see below |

**`security_score` is the one I am least sure about.** It is a number a
committee looks at, which is a Watch-style "tell me what happened", but it
only means anything once guard decisions are feeding it. I put it in Guard
for that reason. If you want Watch sites to see a score, move it — it
computes fine from detections alone.

### Proposed COMMAND (6)

| feature | what it is | why Command | risk if wrong |
|---|---|---|---|
| `face_watchlist` | face matching against a watchlist | the highest-sensitivity thing you run, and a DPDP conversation of its own | lower = you ship face recognition to clients who never discussed it |
| `smart_replay` | event reels | evidence someone else can review | lower = Command loses a headline |
| `intelligence_hub` | analytics, forecast, patterns | the "prove" layer | lower = nothing left to upsell |
| `floor_heatmap` | camera/zone activity map | analytics | lower = same |
| `voice_assistant` | ask the site questions | a differentiator you demo | **arguable** — it demos so well it may sell Watch |
| `weekly_audit` | weekly PDF to the committee | the artefact a client shows a third party | lower = Command's strongest recurring proof point |

**`voice_assistant` is the other arguable one.** It is your most
impressive demo. Gating it to Command means Watch prospects never
experience it; leaving it in Watch means it stops being a reason to
upgrade. That is a sales decision and I have no basis for it.

---

## What I need from you

Fill in a Decision column, or just mark the ones you want moved:

```
feature                      proposed   decision
---------------------------  ---------  --------
anpr                         watch
whatsapp_alerts              watch
morning_brief                watch
resident_directory           watch
visitor_management           watch
flat_visitor_notifications   watch
bulk_resident_import         watch
guard_decision_flow          guard
dvr_recording                guard
pdf_reports                  guard
contractor_passes            guard
security_score               guard      <- least sure
voice_assistant              command    <- least sure
face_watchlist               command
smart_replay                 command
intelligence_hub             command
floor_heatmap                command
weekly_audit                 command
```

---

## How it gets wired once you have decided

Three steps, in this order, and none of them before the table is settled.

**1. Set the tier on every live site**, and verify each one reads back
what you expect:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://<site>/api/site-config | python3 -m json.tool | head -20
```

That response now carries `tier`, `source` and `warning` (OCT-69), so a
site running on a fallback is visible rather than silent.

**2. Backend first, one feature at a time.** `@feature_required` on the
routes belonging to a single feature, deployed, watched for a day. A 403
from a route a client's browser still calls is visible in the log
immediately — which is exactly why the backend goes first.

**3. Frontend last.** `useSiteConfig` / `FeatureGate` around nav items, so
a disabled feature disappears rather than 403-ing. Doing this first would
*hide* the evidence you need in step 2.

The reason for that order: if the UI hides a feature before the API
refuses it, you never learn which sites were using it.

---

## One thing to decide separately

`site_profile.py` currently treats an **unknown** tier as the full
product, and a **missing** tier as the full product with a loud warning.
That is right while nothing is enforced.

Once gating is real, you may want `OCTA_STRICT_CONFIG=1` on new sites —
it refuses to start rather than guessing. A provisioning mistake then
fails at deploy, in front of you, instead of quietly handing a Watch
client the Command product for a year.
