# apps/utskick: Flamingo 2.0 (Kontakter och Utskick)

Architecture and build plan. Status 2026-10-10: **S1, S2, S3 and "After S3" (Giovanni's three
changes: branding on the recipient pages, instant replies, empty fields in the Brev editor) are
built, committed and deployed** (see "S1 as built", "S2 as built", "S3 as built" and "After S3").
**S4 is built and tested locally, not committed and not deployed** (see "S4 as built" and its
checklist). S5 and S6 are not built. This file is the contract for the build; update it when a
stage ships or a decision changes. Revision 2 (2026-10-09) folds in the security, ops and product reviews; what
was not taken over is listed in section L.

Inputs: `mockups/flamingo-utskick.html` (all views, flowcharts, data model, "Senare", "Medvetet
bortvalt"), `mockups/flamingo-epost-brev.html` (the chosen email style Brev, 24 elements), memory
notes `flamingo-utskick`, `sms-api`, `flamingo-synlighet`, `inga-automatiska-kundmejl`,
`inga-loften-i-koden`, `mobilt-ar-inte-tillval`. Code references were checked against HEAD
`3cbdae0` (paths relative to `webapp/`).

Language: prose in English, UI copy in Swedish. Every Swedish example follows the house rules: no
en/em dashes, no typographic quotes, no ellipsis character, no `[ ]` decoration, no response-time
promises, du-form. Examples use the demo company **Exempelrör** (`exempelror.example`, accent
`#1A57D6`), never a real customer; seeds, tests and templates copy them as they are.

## Fixed decisions (referenced as D1 to D14)

| # | Decision |
|---|---|
| D1 | New app `apps/utskick`, label `utskick`. Never reuse "Campaign" (`flamingo.Campaign` is Google Ads). UI words "Kontakter", "Utskick". Paths `/flamingo/app/kontakter/`, `/flamingo/app/utskick/`. |
| D2 | Per-customer activation, off by default, toggled by the agency on the customer card with its own endpoint. No pilot. `APP_NAV` shows Kontakter and Utskick only when enabled. |
| D3 | Stages S1 to S6, deploy after each (contents in section J). |
| D4 | Shared reply number `+46766860046` (46elks) for all customers. Replies and STOPP are routed to the customer of the latest outbound reply-number sms to that phone. Per sms utskick the sender is the reply number (replies and STOPP work) or an approved alphanumeric name (no replies, unsubscribe link mandatory). Setting the number's `sms_url` is a manual step that is asked first. |
| D5 | Sms billed exactly as today through `apps/sms` statements; `service.send` is refactored into an account-level send with a `source` field. Caps and contact counts are built; no plan fee on statements yet. |
| D6 | DPA (biträdesavtal) is a public page `adx.se/bitradesavtal/`, created in production like the other legal pages and linked in the footer, plus an acceptance step in the app before the first contact is added or imported. Privacy policy and sms terms are updated when S2 goes live. |
| D7 | No A/B tests, no guessed age or gender, no exit-intent popups, no send-time optimisation, no resend on opens. |
| D8 | Email replies via SES inbound in eu-west-1 (eu-north-1 cannot receive); data stays in the EU. |
| D9 | Email style is only Brev. Logo left, center or none; one accent colour per utskick (light colour gives black button text and darker links). Open pixel off by default, the customer can enable it. |
| D10 | `/lp/` keeps its no-cookie policy. Attribution travels as a URL token (`ut=`) from the redirect; LP visit and engagement may be logged server-side keyed by the token, never with cookies or browser storage. |
| D11 | Utskick-driven leads are not uploaded to Google; they are reported in Flamingo and excluded from Google cost-per-deal. |
| D12 | Hard rules: no automatic emails to ADX's customers (agency alerts and the subscriber's own double opt-in mail are fine); no response-time promises (guard scans `test_*.py` too); no AI typography anywhere incl. `test_*.py`; no `[ ]` decoration; every view works at 375 px; the demo account never sends; staff in view-as may act for real, but sending needs an explicit confirmation step and is logged with the acting user. |
| D13 | Background work is one every-minute cron command `utskick_tick` with a time budget and `select_for_update(skip_locked=True)`, plus a daily maintenance command. No Celery, no Redis. |
| D14 | AWS through SES v2 API with boto3 and `apps/cloud/aws.base_session()`. IAM, Route53, SES identities, configuration sets, SNS/SQS and receipt rules are infra steps run by the lead (needs Giovanni's SSO login), listed as checklists in section J. |

Design choices inside these decisions that matter everywhere (details in the sections named):
- **All utskick mail (DOI included) is sent from SES in eu-west-1**, the same EU region as inbound
  (D8), with its own identities, configuration set, quota and account-level suppression list, so a
  customer's bounces or complaints can never pause or suppress ADX's own mail in eu-north-1 (H.8).
- **AWS access goes through a dedicated role `adx-utskick`** assumed from `base_session()`; the
  shared instance role only gets `sts:AssumeRole` on it (H.8).
- **SES events and inbound-mail notifications reach us through SQS queues polled by the tick**, not
  an HTTPS webhook (D.7). Nothing is lost while adx.se is down, and the two web workers never parse
  mail.
- **Nothing sends until the agency has turned the global switches on** after the infra checklist
  (`Switchboard`, D.8). Deploying code never starts sending.

## Naming rule

"Kontakter" already means portal users in Flamingo (`access.CONTACT`, `is_contact`,
`flamingo_contact_count`, settings.html "Kontakter hos ..."). In code the register model is
`utskick.Contact`; in templates and view contexts it is always `kontakt` / `kontakter`, never
`contact`. Related names on `FlamingoAccount` are prefixed `utskick_` (`account.utskick_contacts`).
S1 renames the settings heading "Kontakter hos {customer}"
(`templates/flamingo/app/onboarding/settings.html` line 66) to "Inloggningar hos {customer}", so the
customer sees one meaning of "Kontakter".

## Deviations from the mockup forced by the decisions, the code or the reviews

- Flowcharts B and E set a visitor cookie on adx.se. Replaced by the `ut` URL token and server-side
  logging (D10). Later visits without a link are not recognised.
- Report box "Skickat vidare till Google Ads, 31 konverteringar" is dropped (D11). The report shows
  "Förfrågningar via utskick" instead.
- "Besökte 3 sidor" / "Vad de gjorde på sidan": a landing page is one page. We report visit, time on
  the page, number click and form lead per click ("Tid på sidan" replaces "Sidor").
- "Ungefärlig ort" on clicks is dropped (no GeoIP database on the box). Device type only.
- "Svarar oftast på sms, kvällstid" on the contact card is computed in S4 from `ThreadMessage` times
  (I.7); nothing guessed.
- STOPP words are a fixed list (G.1). "NEJ TACK" alone is a normal answer, but a reply to a reklam
  send containing it is flagged "Ser ut som en avregistrering" with a one-click button.
- START does not re-subscribe on its own: the answer carries a confirm link (sender numbers can be
  spoofed).
- "Sms bekräftas inte separat" on the signup page is replaced: sms consent from the signup page
  becomes `yes` only after a click on a confirm link in an sms (S2; question K.2.5). The LP checkbox
  gives `yes` directly until Giovanni answers K.2.5.
- Sms link codes are 6 characters, not 5 (enumeration margin, E.2).
- Named links are `klick.adx.se/<public_slug>/<slug>`, not `klick.adx.se/vinter`.
- `k.adx.se/s/...` needs a button press: a GET never unsubscribes (previews and scanners open links).
- `/s/` and `/p/` show masked contact details only, no first name (codes can be guessed and sms are
  forwarded).
- On the preference page the other channel shows "Anmäl dig" (starts a confirmation), not a toggle;
  turning a channel on always needs a confirmation.
- DOI mail comes from `bekrafta@utskick.adx.se` and the confirm page needs a second click (a GET
  never confirms).
- The Inkorg stays list-then-detail (the existing inbox), not the mockup's two panes.
- The exclusion "Fick sms senaste 14 dagarna" is relabelled "Fick ett utskick senaste 14 dagarna" (any
  channel).
- The ADX-domain card shows the reply mode actually set (Inkorgen or own address), not a fixed
  "svar till hej@...".
- DNS row "klick CNAME (Valfri)" is dropped (own click domains are not built, K.2.4).
- No "ny" badge on the nav items.
- "Nyhetsbrev ungefär en gång i månaden" on the preference page is customer-written text, empty by
  default (a frequency line is a promise).
- The Underskrift script font is a system script stack; the mockup's Caveat web font is not loaded
  (no external fonts in mail).
- "Färger och logotyp hämtas från Företaget": there is no account brand colour. Logo comes from
  `MediaAsset(is_logo=True)`; the default accent from
  `media.logo_colors_for_account(account_id)["primary"]`, stored per utskick.
- The reply number is the real `+46766860046` (shown "0766 86 00 46"); the settings row has no "Byt"
  button (one shared number).
- "Senare" chips and the sentence "Anmälan med sms-ord ... kommer senare" are never rendered: no
  unbuilt feature and no "kommer senare" text appears in the product (it reads as a promise).
- Own click domain (`klick.<kundens domän>`) is not in S1 to S6 (K.2.4).

## S1 as built (2026-10-09, deployed)

Everything in J S1 exists: the 17 models (`utskick.0001`, `flamingo.0016`), the core modules, the
Kontakter views (list, card, consent panel, add and edit, GDPR export and delete, register export,
prune, lists and tags, fields, signup settings, contact settings, DPA step), the importer, the
public pages and the DOI mail, the LP checkboxes and capture, the agency side (`manage_views.py`:
customer card, `/manage/utskick/` overview, nödstopp and readiness marks, DPA publishing, "Avsluta
utskick och radera allt"), the tick (`sending/tick.py`, `utskick_tick`, `utskick_daily`,
`retention.py`), the `monitor_check` heartbeat, the demo (`demo.py`), the server files
(`server/crontab.d/adx-utskick`, `server/logrotate.d/adx-utskick`, `server/aws-utskick-role.sh`, the
tick lock in `server/deploy.sh`) and the cross-cutting changes of C.3 (robots, analytics skip,
Sentry, static_version, `RESERVED_SLUGS`, MCP forbidden names, "Inloggningar hos"). Tests:
`test_s1_{models,consent,import,views,public,lp,tick,guards,manage}.py` plus the extensions in
`test_demo.py`, `test_inbox.py`, `test_security.py`, `test_sentry.py` and `apps/assistant/tests.py`.
`S1-HANDOFF.md` lists file ownership, helper APIs and the per-agent notes; delete it when S1 ships.

Deviations from this contract, decided while building S1:

- Models: `Consent.basis` and `ConsentLog.basis` are `max_length=17` (`existing_customer`);
  `Event.idempotency_key` comes with S5 (`db_default=""`); `ImportJob.size` defaults to 0; the
  pending-confirmation partial index is on `changed_at` (the tick reads oldest first).
- Consent rules: `looks_like_personnummer` also needs the month digit 0 or 1 (an org number in a
  field is not refused); customer sources need `evidence` for `yes`/`existing`; a form submission
  over `yes`, `existing` or `company` is refused as `already` (no downgrade to `pending`); derived
  `company` may be upgraded to `yes`/`existing` by customer sources.
- Import: templates are `kontakter/import/*.html` (not `import_*.html`); the Excel child runs as
  `python -I <path>/xlsx2csv.py` (with `-I` the `apps` package is not importable, so not `-m`);
  each in-request step gets at most 20 s, the rest goes to the tick; the upload is deleted as soon
  as the CSV exists; rows without phone and email are errors; rows whose every address is
  suppressed, and rows marked unsubscribed in the file, create no contact.
- Contacts UI: the consent change is a panel on the card (works without JS), not a `<dialog>`;
  bulk delete and prune take at most 1 000 contacts per request; the 10-per-day export limit
  counts every register export, selections included; the contacts table turns into cards under
  820 px of its own width (container query).
- Public pages and DOI: staff see the signup page's email option before `doi_ready_at` (preview
  strip, for checklist step 6). The DOI queue (`optin.due`) filters `sending_blocked=False` (D.2)
  and is sent only while `doi_ready_at` is set; before the mark only consents last changed by a
  staff user go out (the staff test signup is saved with the staff actor). "Stoppa all sändning
  nu" also clears `doi_ready_at`, so it stops DOI mails too; removing the mark does the same.
  `confirm_sent_at` means "handled by the tick" (sent, or skipped by a limit, a missing DPA or a
  rejected mail); queued rows older than 7 days are not sent; on Mina utskick only turning a
  channel on runs the botcheck (and the botcheck logs a scrubbed path, never the token); the DOI
  button uses the default accent `#1A57D6` and no SES configuration set yet (S3).
- Abuse limits added after the S1 security review: at most 100 signups per account per hour
  (`signup_account`, 429 "Det har kommit många anmälningar just nu.") and at most 100 DOI mails
  per account per hour (`optin_account_hour`; the rest wait in the queue), each with one agency
  alert per hour and account.
- No enumeration (S1 security review): the signup thanks page says the same for every outcome
  ("Om a***@e***.example inte redan får erbjudanden från Exempelrör får du ett mejl dit."), the
  signup form's list and tags are added when the person confirms in the DOI mail (not at the
  unverified signup), and the SIGNUP event is recorded only when the consent changed.
- Signup preview: `?forhandsgranska=1` shows a closed signup page (inactive, missing privacy
  facts and so on) to the account's own users and to staff, with a strip and a disabled button;
  the POST is refused (404). The Förhandsgranska button shows whenever the page has an address.
- LP: the thanks page shows the DOI sentence when the redirect carries `?epost=1`, which is added
  whenever the email box was ticked, whatever capture did (no cookie, no personal data, no
  enumeration); its text is "Om du inte redan får e-post från oss skickar vi ett mejl till dig.
  Klicka på länken i mejlet för att börja få e-post."; after a validation error the visitor's own
  tick stays (a fresh page never has `checked`) and the failing box is marked `is-error`.
- LP capture never changes an existing contact (S1 security review): no name or address is filled
  in from the form (anyone can type someone else's number plus their own email), consent is
  stored only for a channel whose submitted address already is the contact's, and the lead is
  linked only when `contacts.match` finds the contact without a conflict.
- Import (S1 correctness review): an extra-field value that does not validate (a bad date, a word
  in a number column, text over 500 characters) is dropped and the contact is still imported; the
  error report lists it ("... Kontakten importeras utan värdet.", `counts["values"]`). Date and
  number fields are guessed only when every sampled value parses. Several phone-like headers
  ("Telefon" and "Mobil"): the column whose values look like mobiles (else a `mobil*` header)
  becomes Mobilnummer. The width is the widest of the header and the first 50 rows; a column
  without a header is named "Kolumn N". Excel ignores the sheet's declared `<dimension>`.
  `recover()` fails jobs stuck in `uploaded` for 10 minutes and deletes their files. Updating an
  existing contact from a file records the "Importerad" event without counting as activity (so
  the inactive flag of E.7 survives re-imports). The consent log and timeline show the file name
  ("kunder-2026.xlsx", "Inklistrade rader"), not the job number.
- Agency side: `RESERVED_SLUGS[DESIGN_ADX]` gets `utskick` only. `bitradesavtal` is not reserved,
  because the DPA page (D6) is exactly that ADX BlockPage and reserving the slug would make
  checklist step 1 impossible. The DPA snapshot (`manage_views.page_text`) is the text of the
  page's visible blocks (text, multi-line and rich fields and list rows from the block schema,
  tags stripped), at least 200 characters, refused when identical to the current version.
  "Avsluta utskick och radera allt" keeps the `UtskickSettings` row (turned off), `DpaAcceptance`
  and `ExportLog`, and blanks `ConsentLog.evidence` like the GDPR delete.
- Tick: phase 1 in S1 hands import jobs a request left behind to the tick
  (`importer.recover`); phases 2, 4 to 7 and 9 come with S2, S3 and S5. On a key mismatch the tick
  returns without a heartbeat (D.2), so `monitor_check` also reports it when work waits.
  `UTSKICK_TICK_MAX_MB=0` turns `RLIMIT_AS` off; the test runner sets 0 so a test that runs the
  command never caps the test process.
- C.3 items for link hosts that cost nothing before S2 are in (analytics early return, Sentry
  sampler by Host); the MCP link-host 404 in `asgi_app` is S2.
- `config/test_runner.py`: `--parallel` now starts (the worker setup loads settings first).
  `apps.flamingo` and `apps.utskick` pass in parallel, but the whole suite under `--parallel 6`
  showed failures outside these apps (closed database connections in later tests of a worker, a
  missing file in `apps.cloud`'s invoice test), and without `tblib` one failure stops the whole
  parallel run. The full run therefore uses `--parallel 1`.

## S2 as built (2026-10-10, committed and deployed)

Everything in J S2 exists. Decisions Giovanni confirmed after revision 2: the reply number may be
pointed at the inbound webhook after S2 is deployed (K.2.1, a manual step); the yearly 999 kr sms
fee also applies to customers who use sms only through Utskick (K.2.7, no fee change in code);
SES eu-west-1 has production access, the identity `utskick.adx.se` is verified and the role
`adx-utskick` exists (S3 inputs).

What exists, by layer:

- **Data**: `utskick.0002` (Utskick, Recipient, AllowedHost, TrackedLink, LinkCode, Click, Thread,
  ThreadMessage, InboundMessage, the nullable utskick FKs on ConsentLog, Suppression and Event),
  `flamingo.0017` (Lead utskick FKs, attribution, `activity_at` with the backfill, source `reply`),
  `sms.0006` (source, sender 16, the indexes, `by_source`), all per B.0. Every S2 foreign key also
  carries its `ON DELETE` in Postgres (`dbfk.py`, see deviations).
- **apps/sms** (C.1 in full): `send_for_account` with `source`, the reply-number rule, headroom,
  the 429 mapping, `hooks.py`, API reads limited to `source="api"`, the suppression endpoint,
  `by_source`, portal source filter and labels, `/smsz/`, `elks.list_messages`.
- **Engine** (D.1 to D.5, D.8, D.9): `audience.py`, `timing.py`, `composer.py`,
  `sending/{state,checks,freeze,sms,recover,tick,sms_wrapper}.py`, `smsbridge.py` (delivery
  hook), `reports.py`, `retention.purge_s2`, the month-end RESERVED check, agency alerts.
- **Links and attribution** (E.1 to E.5, E.7, E.8): `links.py` (router and rules),
  `link_views.py`, `link_actions.py`, `attribution.py`, `codes.py`, `tokens.py` S2 part,
  `optin.py` S2 part (confirm sms, sms on the signup page and Mina utskick),
  `config/urls_links.py`, the LP changes in apps/flamingo (`ut`, beacon, raised limits).
- **Inbound and Inkorg** (G.1, G.2): `inbound/{elks,routing,stop}.py`, `webhook_urls.py`,
  `threads.py`, `app_views/inbox_reply.py`, the inbox changes in apps/flamingo
  (`app_views/inbox.py`, owner texts in `sms.py`).
- **UI** (I.1, I.4 to I.10): `app_views/utskick.py` (list, the five steps, Granska, confirm,
  state, report, recipients, save as list, test sms, settings, the JSON endpoints), templates under
  `templates/flamingo/app/utskick/`, `static/css/flamingo-app-utskick-views.css`,
  `static/css/flamingo-app-utskick-thread.css`, `static/js/flamingo-app-utskick.js` (incl. the sms
  counter the inbox thread reuses); outside the app `rules.three_things`, `overview.numbers_for`
  ("Varav via utskick"), the contact-card timeline and summary.
- **Agency** (`/manage/utskick/`): `manage_sending.py` (#sandning: running and paused utskick,
  the breaker, information utskick with the staff override, "Provsms till mig"),
  `manage_inbound.py` (#inkommande: held replies, "Koppla till kund", "Lägg åt sidan"),
  `manage_links.py` (#vardar and the customer card: link hosts to approve or refuse), the D.8
  pause in `customer_update`, and "Avsluta utskick och radera allt" removes the S2 rows.
- **Server** (C.5): `sites.d/adx.conf` `LINK_DOMAINS`/`LINK_CERT_NAME`, `lib.sh`
  `build_link_block` (port 80 always, 443 once `/etc/letsencrypt/live/adx-links/` exists),
  `templates/nginx.conf.template` `${LINK_BLOCK}` and `access_log off` for `/api/utskick/`,
  `/utskick/val/`, `/utskick/bekrafta/`, `certs.sh` lineage `adx-links`, `nginx-only.sh`.
- **Demo**: `demo.seed` builds one sent (simulated) utskick "Spolning inför vintern" with three
  clicks, two leads via the utskick, two reply threads (one new, one answered and "Klar") and one
  STOPP, one scheduled and one draft, all through the real engine, routing and STOPP code;
  `demo.reset` removes them.
- **Tests**: `test_s2_{foundation,tick,ui,links,inbound,inbox,info,flow}.py`, the S2 tests in
  `apps/sms/tests.py`, and the extensions in `apps/flamingo/test_demo.py` (S2 content, idempotent
  counts, the page walk with `apps.sms.elks._post` failing, the tick simulating the demo, the
  sending buttons refusing it), `test_s1_guards.py` (link templates), `apps/common/test_sentry.py`.
  `test_s2_flow.py` walks the whole path with the real views, the tick and apps/sms: guide, Granska
  and confirm, freeze and send, delivery report through the hook, report; click on `k.adx.se`,
  LP with `ut`, beacon, form lead with attribution, Inkorg; inbound reply, thread, answer from the
  Inkorg and the next reply in the same thread, batched owner sms; STOPP, its confirmation from
  the tick, START and the `/b/` link.

`S2-HANDOFF.md` lists file ownership, helper APIs and the per-builder notes; delete it when S2
ships.

Deviations from this contract, decided while building S2:

- **Migrations and models.** Django 6.0.5 has no database-level `on_delete`, so each S2 migration
  ends with `dbfk.apply` (drop and re-add the FK with `ON DELETE CASCADE / SET NULL`, same name);
  guard `test_s2_foundation.DbOnDeleteTests`. **Any later migration that adds an FK to or from
  utskick ends with `migrations.RunPython(lambda a, s: dbfk.apply(a, s, [...]), RunPython.noop)`.**
  `Recipient.ses_message_id` (partial index on `!= ''`) and `opened_at` (S3) and
  `Utskick.merge_fallbacks` (F.3, used by sms) are in S2, so S3 adds no column to a table S2
  writes; `TrackedLink.slug` and its partial unique constraint too (nothing left for S4 there);
  extra index `LinkCode (kind, created_at)` for retention. `Recipient.basis` is 17 characters.
  `Utskick.objects.listed()` is the one place S5 excludes flow-step utskick. `purpose` defaults
  to `reklam`, `channel_mode` to `sms_only`.
- **apps/sms.** The delivery hook is `smsbridge.sync_from_message` (not
  `sending.sms.sync_from_message`), registered in `UtskickConfig.ready()` with the portal labeler.
  A 429 on the dryrun estimate is also `rate_limited`, without a row. Headroom applies to sources
  `utskick` and `flow` only (single sms such as replies, confirmations and tests use the full
  limits). Portal filter groups: "Utskick" = utskick, flow, test; "Svar" = reply, system. The
  suppression endpoint answers 404 only when the customer never had utskick (a disabled utskick
  still answers: the STOPPs bind the customer's marketing either way).
  `elks.list_messages` pages with `start=<next>` (as remembered from the 46elks docs; the
  contract said `end`); it stops after one page if the parameter is wrong, so verify it before
  relying on the reconcile (checklist step 6).
- **Engine.** Swedish holidays include midsommarafton, julafton and nyårsafton (weekend window).
  The demo is simulated before the Switchboard checks (it never sends) and `state.confirm` accepts
  a demo utskick while sms is off. The breaker reads real time. When every due account has used
  its minute budget the sms phase sleeps 1 s and tries again until its deadline. `sms_not_enabled`
  (and a missing or disabled SmsAccount) pauses all of the account's sending utskick
  (`sms_disabled`). `invalid_number` / `country_not_allowed` at send time: recipient `failed`
  with `skip_reason` `invalid_number` / `country`. Person codes are frozen only for the name
  sender; a reply-number recipient gets one at send time when a collision switches the sender.
  The D.9 stops share is `max(Recipient.stopped_at count, Suppression(utskick, stop|link) count)`.
  Tick phase 4 also pauses utskick of accounts that can no longer send
  (`freeze.pause_unsendable`). The freeze cost pre-check uses `composer.preview` parts times
  `recent_part_cost("SE")` or 5 200 plus markup (no dryrun). Weekly cap: reklam recipients sent or
  sending in the Stockholm ISO week of the send moment; information neither counts nor is capped.
  A stale `sending` recipient without a held message is requeued; after 5 attempts it is `failed`.
  `links.rollup` runs on worked ticks while an utskick is sending or finished within 30 days, and
  once a day in `utskick_daily`.
- **"Provsms till mig"** (J S2 step 6) is on `/manage/utskick/#sandning-prov` (POST
  `manage:utskick_probe`), not on `/manage/utskick/nodstopp/` (a POST-only switch): staff pick the
  paying customer in the form among customers with an enabled SmsAccount and a non-demo account
  (choose ADX's internal test customer), at most 10 per staff user and hour; source `test`, sender
  the reply number, works before `sms_enabled`, not past the breaker. Replies to it route to that
  customer's Inkorg.
- **Links.** `clean_external(account, url, allow_pending=False)`; `add_link` stores a link to a new
  host and requests it (Granska blocks through `link_problems`). `check_destinations` returns
  `None` for a URL it did not check. Retention of Click and LinkCode is `retention.purge_s2`.
  HTML pages on the link hosts send `Cache-Control: private, no-store` but keep
  `Referrer-Policy: same-origin`; only the 302s get `no-referrer`. The botcheck on `/p/` sees the
  path without the code. `/p/` "Anmäl dig" exists for email only (a person code always has a
  number). ConsentLog rows written by `consent.set_status` carry no `utskick` (no such argument);
  the Suppression does. A test-sms link code (no recipient) redirects without counting anything;
  `/s/` works by `(account, value_hash)` for it and for an erased contact. `/b/` with purpose
  `start` and no contact lifts the suppression by hash. The click endpoint runs 6 to 7 queries,
  not 4 (the miss check and the scanner check). Link-host 404s all say "Länken har gått ut"; the
  link-host home page links ADX's privacy page at `SITE_BASE_URL + "/integritetspolicy/"`
  (verify the production slug).
- **Inbound and Inkorg.** The thread template is `flamingo/app/utskick/_thread.html`. Reply leads
  leave `Lead.utskick` empty (the answered utskick is `Thread.utskick`), so replies never count as
  "Förfrågningar via utskick". No `Event` rows for reply, stop or start: the timeline reads the
  thread messages and the consent log. A reply from a number that is not a contact creates one
  only when the account can collect and the number is not suppressed. STOPP and START answers are
  not gated by `sms_enabled` (step 6 tests STOPP before sms is on) but by the breaker and the cap
  pre-check, and are dropped after 6 hours unsent. Extra limits: one START link per number and
  customer per day; more than 20 ordinary sms an hour from one number are only counted. Inbox
  replies: a 60 s double-submit guard, and a sent reply marks the lead "Klar". The inbox month
  count leaves replies out; the "E-postsvar" chip is hidden while it is empty. "Production" for
  the empty-IP-list refusal means `DEBUG` off.
- **UI.** Purpose (Reklam, Information with the reason) is on the Kanal step (I.8), not on
  Mottagare as in the mockup. The opt-out line is added to every utskick sms, information
  included; with a name sender a typed "Svara STOPP för att inte få fler sms." is removed. A raw
  `http(s)://` or `www.` address in the text is a validation error: links go through `{länk:x}`.
  "Skicka test till kunden" is a `<details>` with the I.4 checkbox (works without JS); Skicka nu in
  Granska is a `<dialog>` with the same sentence above the button without JS. "Byt kontakt"
  cycles through the first 50 audience contacts. A pause from the app is reason `customer`, from
  staff in view-as `staff`. Cost estimates use `recent_part_cost("SE")` or 5 200 per part plus the
  account's markup (verify the 52 öre fallback against a live dryrun in the checklist). The email
  row of "Tak per kontakt" (I.9) is already editable because the field exists.
- **Integration.** The demo's utskick go through the real engine; the scheduled one is simulated
  by the tick when its time comes (the demo still never sends), and `flamingo_demo` rebuilds it.
  The demo's own leads get `activity_at = created_at`, so the Inkorg keeps their order. A thread
  message that predates its thread and has no SmsMessage, sender or inbound (the demo's simulated
  sms, or an sms row that no longer exists) is labelled "Utskick: <namn>". The nginx link block
  also sets `X-Robots-Tag` itself (static files included) and hides any `Set-Cookie` from the app.
  Sentry also masks link-host links without a scheme (`k.adx.se/Ab12Cd`, as they stand in sms).

Fixes after the three S2 reviews (security, sending, UX), 2026-10-10:

- **Content after the confirmation.** Adding or removing a link unconfirms a scheduled utskick
  like any other change. `state.content_problems` (`links.link_problems` plus
  `checks.information_problems`) runs at freeze start and in the pre-checks; a problem pauses
  with the new reason `content` (in `RECONFIRM_REASONS`, choice added to `utskick.0002`, no SQL).
  The click endpoint re-checks external hosts (`links.destination_ok`), test codes included.
- **Edits race the tick.** Every step save runs under `select_for_update` on the utskick row with
  the status re-checked under the lock (`app_views.utskick._editing`); a lost race answers "Utskicket
  går inte att ändra nu." and changes nothing. The agency alert for a new link host is sent after
  the lock (`links.request_if_new`), never while it is held.
- **Own site.** Only `Customer.website` (staff-set) counts, never `FlamingoAccount.website_url`;
  shared hosts and public suffixes never count (E.8). Information uses the same domains
  (`checks.own_hosts` is `links.own_domains`). The shared test fixture gives Exempelrör the
  website `https://exempelror.example` in the customer register.
- **Information rules.** The agency override stores `fingerprint` (`checks.content_fingerprint`:
  reason, reason text, body, fallbacks) and holds only while it matches; the agency overview says
  when the text changed since. The ad-word check covers inline fallbacks and `merge_fallbacks`;
  `{fält:...}` values are checked across the audience in Granska and the frozen values after the
  freeze (`checks.field_problems`, never released by the override). Names (`{förnamn}`) are not
  checked: "Rea" is a first name.
- **Test sms** is refused while `content_problems` is non-empty, and every attempt is logged with
  the user pk and staff flag (the contact event too).
- **STOPP confirmation** is one per account (G.1).
- **References** carry `~` (C.1); `sms.apply` and `recover.held_message` adopt only rows with
  source utskick (or flow) to the recipient's own number.
- **"Avsluta utskick och radera allt"** also deletes the reply leads. **"Koppla till kund"** lists
  only customers that sent from the reply number to that number (candidates first), never the
  demo; `route_held` creates no contact for an account without such an sms.
- **Small hardening.** The inbound webhook compares the token as bytes (a non-ASCII token is 404,
  not 500). Sentry masks `sok=`. Recovery cancels rows it requeued into a cancelled utskick.
  `adopt` and the thread paths re-read the sms status after linking it (a delivery report in that
  gap is no longer lost). Price hints are per country and only from countries without history.
  The reconcile reads back to the previous run minus 20 minutes (at most 48 h), keeps the tick's
  deadline (`elks.list_messages(deadline=)`, each GET bounded by the time left, `complete=False`
  when cut short), and keeps the previous timestamp after a 46elks error.
- **Stats.** `retention.refresh_stats` recomputes `Utskick.stats` daily for utskick finished in the
  last 30 days, and `purge_s2` recomputes them right before the recipients (and clicks) go.
- **Re-confirming a paused utskick.** Granska sends now when the scheduled time has passed (late,
  audience grew); a future time is kept. The "Ändra" links, Byt automatiskt and the step chips are
  hidden or inert in that mode, and "Spara utkast" becomes "Tillbaka till utskicket". A paused
  utskick that needs a new confirmation and has nothing frozen can be reopened as a draft
  ("Ändra utskicket", `state.reopen`; late goes to Tid, content to Innehåll).
- **Guide.** The step chips, "Utskick" above the title and "byt kontakt" are submit buttons that
  save the step first (`form=` attribute; a hidden default button first in the page keeps Enter
  as before), and `flamingo-app-utskick.js` asks before leaving a step with unsaved changes
  (`form[data-ut-guard]`). "Spara utkast" and the chips leave Mottagare without recipients; only
  Nästa needs a choice. "Ändra" on a scheduled utskick is a link (it becomes a draft only when
  something changes). Cancelling from the pause banner asks first.
- **Copy.** The cost-raising characters are named ("ett hårt mellanslag"; a visible one is
  followed by its name, "(en typografisk apostrof)") and the Byt automatiskt box is always in the page (the script shows it); the counter
  is not `aria-live` (the notes are). Missing link: "Lägg till den under Länkar i Innehåll." The
  demo's dialog says nothing is sent. A time under 5 minutes ahead: "Välj en tid minst 5 minuter
  fram." The locked Flamingo-page option says how to unlock it. The thread writes 09.00 and "för
  dig". The recipients page has a "Gick inte fram" chip and an empty text per view. The email cap
  row in Inställningar shows only once email is live (the stored value is posted hidden).

Checklist notes for the lead (in addition to J S2):

- Step 2: nginx serves `/static/` on the link hosts (the pages load `utskick-public.css` and, on
  `/p/`, `utskick-public.js`); `collectstatic` in the deploy covers it.
- Step 4: `server/nginx-only.sh adx` (port 80 only), `server/certs.sh adx` (lineage `adx-links`;
  certbot's `--nginx` challenge works with the server-level `return 301` as for adx.se), then
  `server/nginx-only.sh adx` again (443).
- Step 6: verify the 46elks history paging parameter (`start` vs `end`) with a message older than
  the first page, and the 46elks part price against the 5 200 fallback.
- Before a real customer: the ADX privacy page slug the link-host home page links to.

## S3 as built (2026-10-10, committed and deployed)

Everything in J S3 exists in code; the AWS side (checklist steps 1 to 3) does not, and every S3
path works without it: empty `UTSKICK_SQS_*` and `UTSKICK_SES_INBOUND_BUCKET` mean off, and no
email utskick goes out before `Switchboard.email_enabled` (D.8). Built by a foundation agent,
four builders (A Brev and the renderer, B the editor and the email parts of the utskick UI, C
sending, events, health and domains, D inbound mail, Inkorg and the link-host email pages) and
an integration pass. `S3-HANDOFF.md` has the file ownership, the helper APIs and the per-builder
notes; delete it when S3 ships.

What exists, by layer:

- **Data**: `utskick.0003_brev_och_epost` (the B.2 email columns on `Utskick` plus
  `email_snapshot`, `SenderDomain`, `EmailImage`, `EventReceipt`, the account email block on
  `UtskickSettings`, `Switchboard.ses_account` and `ses_checked_at`), every column with
  `db_default`, every new FK with its `ON DELETE` in Postgres (`dbfk.apply`). Settings of C.4
  in `base.py` and `.env.example`; the test runner blanks the role, the queues and the bucket.
- **Brev** (F.1 to F.5): `email/registry.py` (22 blocks plus header and footer, the new field
  kinds), `email/blocks.py` (validation, `rich_basic` as an AST, `save` with `email_rev`, terms,
  signatures with the email salt), `email/style.py` (one accent, the light-colour rule),
  `email/render.py` (tables, inline styles, 560 px, the mso wrapper, editor, preview and send
  modes, the frozen snapshot, per-recipient `klick.adx.se` links, the pixel only for
  `open_tracking` and `tracking_ok`, web view, `.ics`), `email/text.py`, `email/images.py`
  (email renditions, absolute URLs, retention), `email/checks.py` (F.5), templates
  `templates/utskick/brev/`.
- **Editor** (F.6 to F.8): `app_views/brev.py` (the seven `app_brev*` views), `ai.py`,
  `brev_editor.html` and partials, `flamingo-app-brev.css` and `.js`, `flamingo-pb.js` with a
  profile per editor (`pages.PAGE_PROFILE` unchanged, asserted in `test_pagebuilder_editor.py`);
  the email parts of the guide (Kanal with the four modes, Innehåll with the E-post tab, Granska
  with the email checks and the ADX cap, the test mail), the report's email tiles, the settings
  rows of I.9 and the list's channel label.
- **Sending** (D.5 to D.9): `sending/email.py` (tick phase 6, the caps, the probe, throttling,
  unknown and never resent, `deliver` and `send_test` for every single mail), `email/transport.py`
  and `email/mime.py` (configuration set, tags, one-click headers never RFC 2047 encoded),
  `sending/health.py` (per utskick, per account with "Släpp spärren", ADX-wide), `email/domains.py`
  (claim rules, Easy DKIM, MAIL FROM, dnspython checks, expiry, `ses_created` delete rule),
  `inbound/queues.py` and `inbound/events.py` (SQS, receipts, DLQ), the S3 parts of `tick`,
  `freeze`, `state`, `recover`, `retention` and `utskick_daily` (`--only ses|domains|dlq`).
- **Inbound and link pages** (E.5, G.2, G.3): `inbound/email.py` (bucket pin, token from the
  receipt, verdicts, hourly caps, autoreplies, quote stripping, attachments listed, mailto
  unsubscribe, the bucket sweep), the email threads in `threads.py`, `_thread.html` and the
  inbox reply by email; all six `klick.adx.se` views in `link_views.py` (`/m/`, `/a/` with the
  one-click POST, `/v/`, `/w/` with the F.4 CSP, `/o/`, `/c/`).
- **UI pages**: Leveranshälsa (`app_views/health.py`), Avsändare och svar (`app_views/domain.py`:
  Egen domän and the own reply address with its confirmation link).
- **Agency**: `manage_email.py` (the e-post panel on `/manage/utskick/` with warnings, accounts
  with an email block and "Släpp spärren", customer domains and `/manage/utskick/doman/<pk>/`,
  queues and DLQs with "Skicka tillbaka" on `/manage/utskick/koer/`, "Provmejl till mig"), the
  information override fingerprint for email (`manage_sending.info_override`), "Avsluta utskick
  och radera allt" removes the S3 rows.
- **Server**: `server/aws-utskick-role.sh` writes the whole H.8 policy; `server/aws-utskick-s3.sh`
  creates the configuration set, SNS, SQS with DLQs, receiving (identity, MX, bucket, rule set)
  idempotently and prints the `.env` lines. Neither has run.
- **Demo**: `demo.seed` adds "Höstbrevet", an email-only utskick in Brev to the list Kunder,
  built through `email.blocks`, confirmed, frozen (snapshot and an lp TrackedLink) and simulated
  by `sending.email.simulate`, with one click to the Flamingo page; `demo.reset` also removes the
  account's `EmailImage` and `SenderDomain` rows.
- **Tests**: `test_s3_{foundation,registry,render,media,link_check,editor,transport,caps,events,
  queues,domains,inbound_email,one_click,link_pages,flow,review}.py`, plus the S3 extensions in
  `test_s1_guards.py` (mail templates), `test_s2_ui.py`, `test_s1_views.py`, `test_s1_public.py`,
  `apps/flamingo/test_demo.py` (the email utskick, `email.transport.send` and `aws.client`
  patched to fail in the command, the page walk, the report and the tick),
  `test_pagebuilder_editor.py` and `apps/common/test_sentry.py`. `test_s3_flow.py` walks the
  whole path with the real views, renderer, tick and transport: editor (`rita/`, `spara/`),
  Innehåll, Tid, Granska, confirm, freeze, the email loop into `FakeSes`, SES events through the
  queue (delivery, bounce, duplicate receipt), health and the report; the click on `klick.adx.se`
  to the landing page with `ut` and a lead with the trail; the pixel; the web view; the `.ics`;
  `/v/`; one-click and mailto unsubscribe; a reply through the inbound queue and bucket into the
  Inkorg and the answer by email with `In-Reply-To`; a reply to the test mail; the timeline.

Deviations from this contract, decided while building S3 (the builders' notes in `S3-HANDOFF.md`
have the detail):

- **Data.** `Utskick.sender_domain` is `RESTRICT`, not `PROTECT` (PROTECT blocks the cascade of
  an account delete). `EmailImage.purpose` is 7 characters. Added columns not in B:
  `Utskick.email_snapshot`, the `UtskickSettings.email_blocked_*` and `email_released_*` fields
  (the D.9 account block, separate from `sending_blocked`), `Switchboard.ses_account` and
  `ses_checked_at`. DLQ URLs are the queue URL plus `-dlq`.
- **Opens.** The configuration set publishes no OPEN (and no CLICK): with OPEN, SES inserts its
  own pixel into every mail, also for recipients without `tracking_ok` (H.5, LEK 9 kap. 28 §).
  Opens come only from our pixel `klick.adx.se/o/<token>.gif`, which counts only a recipient
  that carried it (`open_tracking` on the utskick, `tracking_ok`, a sent-like status), sets
  `opened_at` once and writes `Event(kind="opened")` once; HEAD records nothing, a forged token
  is 404 and pixel misses are not counted (image proxies share addresses). An open is not
  contact activity ("Senast").
- **Tokens.** Formats the contract left open: `/w/` `<utskick62>.<recipient62>.<sig12>`, `/o/`
  `<recipient62>.<sig8>`, `/c/` `<utskick62>.<block_id>.<sig10>`; reply local parts with a
  lower-case base36 signature and the kind `t` (an Inkorg thread) next to `r` and `u`; `/v/`
  reuses the S1 preference token. The own reply address is confirmed through a login-required
  app route (`app_utskick_reply_confirm`), not a public page.
- **Sending.** The daily cap is a wait, not a pause (D.9, I.5). `ConfigurationSetName` is sent
  only once `UTSKICK_SQS_EVENTS_URL` is set (before checklist step 4 mail goes as in S1; the DOI
  mail gets the set and the tags k and a through the transport). In production the email loop
  waits with an agency alert while the events queue is missing. Test mails and inbox replies on
  the ADX domain are counted in `Counter("adx_mail")` with a window at the start of the next
  Stockholm month. Health runs on bounce and complaint events and every 50 sends; a resumed
  utskick is judged on what was sent after the resume; a staff resume after a failed probe
  passes the probe (only a staff resume does); the customer may "Ta bort studsade och fortsätt"
  after `bounces` outside the probe. A
  transient bounce makes the recipient `failed` ("Tillfällig studs"), the fifth in a row is a
  hard bounce. Test mails have no `List-Unsubscribe` and inert footer links; test and probe
  mails carry the recipient id `NO_RECIPIENT`, so replies route by account and sender.
- **Domains.** One domain per account at a time; an expired domain also has its SES identity
  deleted (when `ses_created`); removing a domain moves drafts to the ADX domain and is refused
  while a scheduled, sending or paused utskick uses it.
- **Brev.** Email validation lives in `email/blocks.py` (it reuses the page builder's id
  patterns, structure rules, sanitizers and signing) instead of `types=` on `validate_blocks` and
  `clean_fields`; `types=` and `salt=` exist where the contract asked. Required fields and
  `min_items` never stop a draft save (the checks block them). One variant per block (`brev`).
  Every transparent image is flattened onto white. The footer name is the legal customer name.
  Unconfirmed offer terms warn, they do not block. An information utskick may keep the
  "Hitta hit" map link of the hours block. The link checker runs only on request (the hourly
  quota). **A link to one of the account's own Flamingo pages** (`exports.landing_page_url`) is
  never requested from ADX and never blocks Granska (`email.blocks.own_page`); the freeze makes
  it an lp TrackedLink, so the click carries `ut` like an sms link.
- **Editor.** The desktop canvas is 680 px (at 560 the mail's own phone layout would show). A new
  block is fetched from `rita/` with `{"type"}` (no extra route). Subject, preheader, accent,
  logo, sender, fallbacks and "Uppgifterna stämmer" save with the blocks in one `spara/` call; a
  save without changes keeps the rev; `blocks.save` runs outside the row lock (it may alert the
  agency), so a scheduled utskick that became a draft stays a draft if the block save fails.
  "Förhandsvisa" is a `<dialog>`; "Mörkt läge" approximates inverting clients. The report's
  email tiles are not links (`reports.recipients_for` lists sms recipients only). The tile
  Avregistreringar counts the utskick's email suppressions except the hard bounces' (those are
  under Studsar; found in the final verification).
- **Inbound.** A mailto unsubscribe applies as soon as the notification arrives (no fetch, no
  spam or hourly checks). A per-token limit (`inbound_mail_ref`, 20 per hour) sits under the
  ADX-wide 500. An email reply never creates a contact; a reply from another address joins the
  recipient's thread with a note. `/a/` redirects to its done page after the button and has no
  Ångra (only `/s/` has one). The web view allows `http:` images only with `DEBUG`.
- **Link pages (integration).** `/m/` with a recipient row that retention removed, and a test
  mail's link, redirect without counting (`utm_medium=email`); a recipient of another utskick
  gets the "Länken har gått ut" page. `/v/` is "Dina val" for the token's address with the `/p/`
  rows and rules (`link_actions` takes the channel and the log detail "Dina val (länk i mejl)");
  without a contact, "Avregistrera mig från allt" suppresses the token's hash.
- **Contact card and GDPR (integration).** The timeline shows email bounces ("Studsade: adressen
  finns inte"), complaints and "Öppnade (indikation)"; "Senast" gets "Svarade på mejl", "Adressen
  finns inte, 12 sep" and "Markerade som skräppost". The export adds the open indication per
  recipient, the bounce time and the reply subjects; the delete also blanks
  `InboundMessage.subject`.
- **Agency (integration).** The e-post panel starts with warnings: a DLQ alert today, SES not
  healthy, ADX's 30-day rates over the alert levels, and email switched on without the events
  queue. "Provmejl till mig" is on `/manage/utskick/#epost-prov` (not `/nodstopp/`), as the S2
  probe sms. "Be ADX om hjälp" alerts with the account and domain pks and the link to
  `/manage/utskick/doman/<pk>/`, never a person's name. The settings page also lists a scheduled
  utskick from the ADX domain that does not fit the month cap.
- **Guards (integration).** The foundation stubs (`_stub.html`, `render_stub`, `not_built`) are
  gone and `test_s2_ui` asserts it again. Mail templates (`utskick/mail`, `utskick/brev`) have
  their own guard in `test_s1_guards` (no script, no relative address, the copy rules). Sentry
  also masks the `klick.adx.se` token paths when only the path is logged and the own reply
  address's confirmation path.

Review fixes (security, correctness and UX reviews of S3, 2026-10-10; `test_s3_review.py` has
one test per fix):

- **Inbound mail never stalls the tick.** Text and HTML are cut to 200 000 characters before
  they are read (`inbound/email.py` `TEXT_CHARS`), header values to 998 (`HEADER_CHARS`), lines
  are matched on their first 400 characters, and no pattern backtracks: `<head>` and the quote
  markers are found with plain searches, the break pattern has bounded repeats. 1 MB crafted
  bodies, HTML and headers are read in well under a second.
- **From is trusted only when authenticated.** When the token's recipient row (or thread) is
  gone, blanked, or the `NO_RECIPIENT` of a test or probe mail, the `From` address is used only
  when SPF or DKIM passed and DMARC did not fail (`inbound.email.from_verified`). Otherwise a
  mailto unsubscribe is `ignored` with reason `unverified_from`, and a reply is routed without a
  contact, never into a contact's open thread, and marked "Avsändaren går inte att bekräfta".
- **klick.adx.se counts no misses.** The email tokens carry HMAC signatures, so `/m/`, `/a/`,
  `/v/`, `/w/` and `/c/` answer an unknown token with the plain 404 and never check or count the
  per-visitor miss limit; a shared address (Gmail's one-click POSTs, a company NAT) can never be
  blocked from unsubscribing. `k.adx.se` (short codes) keeps the limit.
- **The demo never reaches SES, DNS or the agency from the domain page.** Every POST on Avsändare
  och svar is refused for the demo; `domains.claim`, `check` and `delete_identity` refuse it too,
  and `check_due` skips demo rows.
- **Staff test mails** go to the staff user's own login address; only a staff login without an
  address may type one, never a customer login's or a contact's, and the test then renders with
  the fallbacks (no contact). A typed sms test number also renders without a contact.
- **IAM**: the role also has a Deny on `ses:SendEmail`/`ses:SendRawEmail` with `ses:FromAddress`
  on every protected identity except `utskick.adx.se` (`NoSendFromProtected`). The
  `sqs:SendMessage` grant for "Skicka tillbaka" is kept (see the review notes in the stage report).
- **No mail without its frozen snapshot.** `sending.email.compose` raises `NotFrozen` without
  `email_snapshot["links"]`; the loop pauses the utskick with `content`. A frozen utskick whose
  mail freeze failed is frozen again in `state.prechecks` (confirm or resume), so a reconfirm
  never sends the live draft.
- **One normalisation for own pages.** The freeze and Granska use `email.blocks.own_page` (also
  with `#anchor`, `?query` and no trailing slash); an lp link keeps its anchor through the click
  (`links.bare_destination`).
- **An unclear SES answer stops the email loop** for the tick (timeouts and 5xx rarely come
  alone) with one agency alert per hour (`ses_unknown`); `SSLError` and proxy errors are unclear
  too, only `EndpointConnectionError` and `ConnectTimeoutError` count as never sent.
- **The caps count failed mails that SES accepted** (`health.counted_q`: failed with `sent_at`).
- **A failed probe is released only by staff.** The customer gets no "Ta bort studsade och
  fortsätt" during the probe (`health.probe_failed`), `state.resume` refuses them, and
  `probe_state` passes only on a staff resume.
- **Domains**: `NotFoundException` from `GetEmailIdentity` fails a verified domain with an alert;
  a verified domain stays verified while SES sends from it (DKIM `TEMPORARY_FAILURE` pauses
  nothing); a new domain still needs DKIM `SUCCESS`. The MX record's priority is its own field
  (`Record.priority`, its own row and Kopiera). On its first verification the account's drafts
  without a sender move to the domain (`domains.adopt_drafts`), and new utskick start with it
  (`domains.verified_for`); the customer may still pick the ADX domain under Från.
- **The tick shares time**: when real mail waits while email is live, the sms loop gets at most
  half of what is left (`tick._email_competes`).
- **Guide and copy**: a new utskick starts as Bara e-post when sms is off and email is live;
  Kanal preselects the first unlocked mode and refuses a locked mode also when it was kept; the
  sms sender is hidden for Bara e-post (`data-ut-show-when="kanal!=email_only"`); Tid speaks of
  the utskick, shows the sms window only when sms is used and the weekly caps per channel;
  Granska links each email row to the page that fixes it (Företaget, Avsändare och svar, Media,
  none for hosts) and checks the mail's links too; the logo buttons show "Ingen logga" without a
  logo; mail wording for the page builder's remaining page texts (`texts` in the profiles); the
  page is "Avsändare och svar" everywhere; "Väntar på DNS" keeps its capitals; a near-white
  accent (contrast under 1.25 against white) gets a bordered button; the missing-value check
  points at "Om ett värde saknas" instead of an example with dots; Leveranshälsa says what a soft
  bounce does; test-mail copy mentions the ADX cap only for the ADX domain; Fält marks required
  fields "(krävs)" and asks for a field first.

Checklist notes for the lead (in addition to J S3):

- Steps 1 to 3 need Giovanni's SSO login: ask first. After the first `aws-utskick-role.sh` run,
  commit `server/aws-utskick-identities.txt` (the identities that existed before S3, kept out of
  every later Deny).
- Step 4: the scripts print the `.env` lines; `systemctl restart adx` (reload does not reread
  `.env`). From then on mail carries the configuration set.
- Step 6: `utskick_daily --only ses` fills `ses_max_rate` and the panel's SES line.
- Step 7: "Provmejl till mig" is on `/manage/utskick/#epost-prov`; pick ADX's internal test
  customer. The panel's warnings and `/manage/utskick/koer/` show the queues.
- Acceptance before a real customer: the real-inbox checks of J S3 (Gmail web and app, Apple
  Mail light and dark, Outlook 365 web, classic Outlook) with screenshots in the stage notes,
  and the 375 px pass over the editor, Leveranshälsa, Avsändare och svar, the settings rows and
  the `klick.adx.se` pages in the real browser.

### After S3: Giovanni's three changes (2026-10-10, committed and deployed)

- **Branding on the recipient pages** (`branding.py`, `templates/utskick/public/_base.html`,
  which `links/_base.html` extends, `utskick-public.css`). Every page a recipient can land on
  (signup, thanks, confirm, Mina utskick, privacy on adx.se; `/s/`, `/p/`, `/b/` on `k.adx.se`;
  `/a/`, `/v/` on `klick.adx.se`) shows the account's own logo at the top on a white band: the
  mail PNG of `MediaAsset(is_logo=True)` (`email.images.rendition(..., LOGO)`, built once, never
  purged while it is the logo), at most 40 px high and 220 wide with its proportions, never
  larger than the file (`email.images.display_size`, the same sizes as the mail header; the
  `width`/`height` attributes are the shown size and the CSS only adds `max-width:100%;
  height:auto`), alt the company name, with an absolute `SITE_BASE_URL + /media/...` address (the
  link hosts have no `/media/`). The account comes only from the code or token. The first view of
  a logo without a rendition builds it in the request, one thread at a time per process
  (`images._making`, re-checked under that lock; the save is outside the decode semaphore so two
  threads cannot block each other); a logo that cannot be built is not tried again for
  `branding.BROKEN_SECONDS` (10 min, the cache). Without a logo, or when the file cannot be read,
  the company name stands as text as before. Decision (review 2026-10-10): the logo also shows
  when the account's utskick are turned off, like the company name, since the D.8 pages keep
  working then. The bottom of every page, the generic ones too (link-host home, 404, 429), ends
  with `static/images/adx-logo.png` (18 px high, dimmed to 60 %, a 44 px tap target, 8 px under
  the privacy link) linking to `https://adx.se` with `rel="noopener"`, no target, no parameters
  and `referrerpolicy="no-referrer"` (a token in a `/utskick/val/` address must never reach
  adx.se's own analytics as a referrer). The web view `/w/` is the mail itself (its header
  already has the logo) and has no page chrome, so it is unchanged. The link hosts stay
  cookie-free; `/media/` on adx.se and `/static/` on the link hosts are nginx aliases. Fixed in
  S3's mail header on the way: a wide logo got `height="40"` and `height:40px;max-width:220px`
  and was squeezed; the header now uses `display_size` in the attributes and the inline style.
  Not done (product call): a tall or square logo stays small (a 120 x 400 logo shows 12 x 40),
  in the mail header too. Tests: `test_s3_branding.py`.
- **Instant replies** (`sending/kick.py`, `UTSKICK_KICK`, default on, off in
  `config/test_runner.py`). After the 46elks webhook has committed an inbound sms that queued a
  STOPP/START answer, or a routed reply that may give an owner notice, `transaction.on_commit`
  starts a daemon thread that runs the tick's own phase 3 (`threads.send_due`) at once, with the
  tick's key check, a 15 s budget, one thread per process (a second kick makes the running one
  take another round) and its own connection closed at the end. The owner notice still only
  counts replies older than `NOTICE_SETTLE`, so the kick waits those 5 s when a notice is due and
  runs once more; a kick that arrives during that wait wakes the thread, so its STOPP answer goes
  at once, and a kick that arrives after the budget is spent gets a fresh thread instead of
  waiting for the tick. Never for the demo, never for a duplicate id, never from the reconcile
  (phase 3 follows in the same tick). The tick is the fallback. Accepted gap: `reply_notice_at`
  is saved before the owner notice is sent (that is what keeps a notice from going twice), so a
  notice is lost if the process stops between the two (a deploy restarts gunicorn and the daemon
  thread dies with it); the tick has the same gap. STOPP/START answers are not lost that way. Claim: STOPP/START answers had none (only
  the `x<inbound>` reference in apps/sms); each answer is now taken with `threads._claimed`, a
  session `pg_try_advisory_lock` on `limits.ANSWER_LOCK + message` held through the send and
  re-checked under the lock (a row lock would have to hold a transaction open across apps/sms'
  own transactions and the 46elks call); owner notices already used
  `select_for_update(skip_locked=True)` on `UtskickSettings` with `reply_notice_at`. Inbox replies
  (sms and email) are not kicked: they were already sent synchronously in the request
  (`threads.send_reply`, `send_email_reply`), nothing is queued there. Not kicked either: the
  confirm sms of `/p/`, Mina utskick and the signup page (`optin.send_due_sms`), and inbound email
  (SQS, tick phase 2). Tests: `test_s3_kick.py` (incl. a real two-thread race of tick and kick).
- **Empty fields in the Brev editor** (`templatetags/brev_tags.py` `pb_empty_attrs`, the Brev
  block templates, `flamingo-app-brev.css`). An empty field is now drawn in the same element, with
  the same inline style and class, as the filled field (only in editing), so "Överrubrik",
  "Rubrik" and "Ingress" stand on their own lines in the mail's layout (24 empty fields in 14
  blocks; lists keep their box). The canvas makes only inline elements (`span`, `a`, ...)
  `inline-block` when empty, and an empty panel field (rich text) shows its label. Empty fields
  in a block that is not selected are hidden with `!important` (the canvas rule, and the rule
  before the editor's script runs in `render.render_html`): the offer code's own inline style has
  `display:inline-block`. The page
  builder's `pb_empty`, `flamingo-pb.css` and profile are unchanged. Tests:
  `test_s3_placeholders.py`.

## S4 as built (2026-10-10, not committed, not deployed)

Everything in J S4 exists in code. Nothing in S4 needs AWS, 46elks or DNS: the only server-side
steps are `uv sync` (segno 1.6.6) and the migrations `utskick.0004` and `utskick.0005`, all run by
`./deploy`. Built
by a foundation agent, three builders (A segments and the audience, B the full report, the
timeline and the contact-card sms, C links, QR codes and the own-site snippet) and an
integration pass. `S4-HANDOFF.md` has the file ownership, the helper APIs and the per-builder
notes; delete it when S4 ships.

What exists, by layer:

- **Data**: `utskick.0004_segment_och_skript`: `Segment` and `SiteSnippet` (B.4), on
  `TrackedLink` the check `utskick_link_named_slug` (a link without utskick has a slug), the
  partial index `utskick_link_named` for the Länkar list and the property `is_named`,
  `Event.SITE_VISIT`. The migration ends with `dbfk.apply` for the new FKs and adds no column to
  an older table (B.0). `utskick.0005_gamla_adresser` (review fix): `OldPublicSlug` (account
  `SET_NULL` also in the database, slug unique), a public address an account had before. `segno>=1.6` in `pyproject.toml`; `qr.py` makes SVG and PNG (always a
  full QR code, level M, quiet zone 4). No new settings.
- **Segments** (`segments.py`, `app_views/segments.py`, `kontakter/segment.html` with
  `_segment_row.html`, `_segment_opval.html`, `_segment_group.html`, `flamingo-app-segment.css`
  and `.js`): the rule vocabulary is the module docstring. The compiler always starts from
  `Contact.objects.filter(account=...)`, every subquery carries the account, ids from a body go
  through `owned_ids` (a foreign list, tag, utskick or segment id is 400 in the builder, the
  count, Mottagare, `app_utskick_count` and the contact filter). 20 rules, 5 groups (ELLER), 50
  ids per rule, 100 segments per account. The live count ("388 kan få sms · 301 kan få
  e-post") runs about 0.4 s after the last change, 60 per minute per account (Counter
  `segment_count`), skips rules not filled in with a note and shows the segment in plain text.
  Every button works without JavaScript. "Öppnade" is locked with the I.11 text until open
  tracking has been on. Wired into `audience.py` (include and exclude), so Mottagare, Granska
  and the freeze count segments like lists; Listor lists them ("Segment · 3 regler", the cached
  count); the contact list has a Segment filter; the card has segment chips;
  `retention.daily` recounts the oldest counts.
- **Report** (`reports.py` S4 section, `app_views/report.py`, `utskick/_report_full.html`,
  `utskick/export.html`, `flamingo-app-utskick-report.css`): one funnel per channel
  (Skickade, Levererade, Klickade, Stannade 30 s+, Förfrågan), clicks per hour as a
  server-rendered SVG (from the start hour, at most 48 hours, at least 12 shown), the per-link
  table, "Vad de gjorde på sidan" (median time, share on mobile, visited, called, form), and every
  number opens its recipient list counted with the same condition (`reports.view_q`). The
  recipient list has the new views and Alla kanaler / Sms / E-post chips. "Följ upp de som inte
  klickade" creates the segment through `segments.create_follow_up` (or reuses one with exactly
  `follow_up_rules`) and a draft "Uppföljning: <namn>" with that segment, then opens Mottagare;
  a second press opens the same draft. Export: a confirmation page, the CSV only on POST,
  `safe_cell`, deleted contacts without name or address, `ExportLog` with the actor (staff marked
  as ADX), the contacts export's 10-a-day limit.
- **Contact card** (`timeline.py` S4 section, `contact_sms.py`, `app_views/contact_sms.py`,
  `checks.contact_sms_checks`): "Skicka sms" with the thread counter and every send-time check
  (suppression, window, cost cap, breaker, Switchboard, blocked account, SmsAccount, reply-number
  collision over 30 days); staff tick "Jag skickar det här som ADX åt <företaget>." and the
  button reads "Skicka som ADX"; the demo never sends; a double submit sends once; the reply lands
  in the same Inkorg thread. The timeline shows the customer's own sms to the contact and
  "Besökte webbplatsen" (and "Gjorde på webbplatsen: <mål>" for `adxFlamingo.track`);
  "Svarar oftast på sms, kvällstid" from 3 replies.
- **Links, QR and the snippet** (`link_views.py` S4 section, `app_views/links.py`,
  `app_views/snippet.py`, `site_snippet.py`, `static/utskick/s.js`, `utskick/links.html`,
  `link_form.html`, `_link_fields.html`, `link.html`, `snippet.html`,
  `flamingo-app-utskick-links.css`, `flamingo-app-links.js`): named links
  `klick.adx.se/<public_slug>/<slug>` (a Flamingo page gets `ut`, an external address is checked
  again at every click; clicks with channel `named` and no recipient; bots counted only; HEAD
  saves nothing; 20 rows per visitor and hour; a lead through the link is credited to the link),
  Länkar with named links (chips, live clicks and leads, Kopiera, QR) and one row per utskick for
  the personal links, Ny länk and a link's page (description and target editable, never the
  slug; delete), QR codes for named links and the signup page (`?ladda=1` downloads). The script
  is 1.9 kB, sets no cookie and stores nothing, does nothing without `adx=` in the address, reads
  it once, removes it and reports to `/v`. The file route serves the version with SRI, a one-year
  cache and CORS; the beacon checks the Origin (the domain or a subdomain), the key and the
  account of the click token, has a size limit and a per-visitor limit (Counter `site_beacon`),
  always answers 204 and ignores the demo. `adx=` is appended only to links whose host has a
  snippet that has been seen (sms, email and named links alike). Spårningsskript (at most 5
  domains, a new domain goes to ADX's host review), the settings row, and a line on the
  generated privacy page naming the snippet domains.
- **Integration** (this pass): `klick.adx.se/Exempelror/Vinter` gets a 301 to the lower-case
  address (`link_views.named_folded`, a second pattern after `named` in `config/urls_links.py`;
  never for the link hosts' own first segments or the reserved prefixes, and only when the named
  link exists, so `/S/Ab12Cd` is still the plain 404); the customer card says how many named links
  hang on the address and refuses a new `public_slug` without the box "Byt adressen ändå. Den gamla
  följer med kunden." (`manage_views.named_links_line`, `slug_change_refused`; the old address is
  kept as `OldPublicSlug`, see "Review fixes" below);
  the Inkorg channel of a lead through a named link is "Länk: <beskrivning>"
  (`apps/flamingo/app_views/inbox.channel`); such leads are not Google's on the overview
  (`overview.numbers_for` counts them under "Varav via utskick", like utskick leads, D11); the
  S4 Counter scopes are in the `limits.py` docstring; `test_s3_branding` follows Giovanni's
  centred logo (`3c86d8a`).
- **Demo**: `demo.seed` adds the segment "Service i höst" (the J S4 acceptance rules, through
  `segments.clean`; Sofia and Brf Exempelgården match, so the field Senaste service is set on
  them), the follow-up segment "Klickade inte: Spolning inför vintern" (through
  `segments.create_follow_up`), the named links "Affisch i verkstaden" (`/vinter`, to the
  Flamingo page, four clicks and a lead from Lisa Ekholm through `attribution.attach`) and "Länk i
  Instagram" (`/instagram`, to `https://exempelror.example/vinterservice/`, two clicks with
  visits), the snippet on `exempelror.example` (seen at the Instagram click yesterday), and in
  Höstbrevet a text link to the same page with Lena's click and the visit the snippet reported
  (`attribution.record_site_visit`), so her card shows "Besökte webbplatsen". `demo.reset` also
  removes the account's segments, snippets and named links. Nothing reaches the beacon, 46elks or
  SES.
- **Tests**: `test_s4_{foundation,segments,report,contact_sms,links,snippet,flow}.py`, plus the S4
  extensions in `apps/flamingo/test_demo.py` (counts, the page walk over every S4 page incl. the
  QR codes, the sms box on every card and the export confirmation, `DemoUtskickS4Tests`),
  `test_s1_guards.py` (the S4 templates are walked), `apps/common/test_sentry.py` (the S4 pages
  and the klick host are not traced, `adx=` and named links are masked), `test_s1_views.py`,
  `test_s2_ui.py`, `test_s3_foundation.py`. `test_s4_flow.py` walks the acceptance with the real
  views, the tick and apps/sms: the segment builder (form and live count), Mottagare with the
  segment and with an excluded segment, Granska, the freeze (the same count all the way); the
  report's funnel and link table against their lists and "Följ upp de som inte klickade"; Ny
  länk, the click (also with capitals), the landing page with `ut`, the lead with the trail in
  the Inkorg, the link page, the rollup and the overview; Spårningsskript, the test link that
  makes it Installerat, `adx=` only after that, the beacon from a subdomain, the visit on the card
  and in the segment rule, refused Origins and another account's snippet; the customer card's
  slug guard.

Deviations from this contract, decided while building S4 (the builders' notes in
`S4-HANDOFF.md` have the detail):

- **Data.** Named links keep `kind` lp or external, never `named`: every rule keys on the kind
  (`destination_ok` re-checks only `external`, `build_destination` adds `ut` only for `lp`), so a
  named link is `utskick IS NULL` plus a slug (`TrackedLink.is_named`); clicks on them use
  `Click.Channel.NAMED`. No new settings: the `adx` parameter is `tokens.adx_token(click.pk)`, the
  shape of `ut` with its own signature purpose `adx` (review fix: it was the `ut` token itself).
- **Snippet domains are not free from review** (E.8 lists them). The customer types the domain
  and the beacon's Origin can be forged outside a browser, so a snippet domain proves nothing:
  `links.own_domains` reads only `Customer.website` and verified sender domains (also for
  information utskick, H.5). The `SiteSnippet` branch was dead code before 0004, so nothing that
  ran before changes. **Giovanni to confirm.**
- **Reserved public slugs.** `a`, `b`, `c`, `m`, `o`, `p`, `s`, `v`, `w` join
  `RESERVED_PUBLIC_SLUGS`, and a slug may not start with `mcp`, `authorize`, `token`, `register`
  or `revoke` (nginx and `asgi_app` 404 every link-host path that merely starts with those words);
  `suggest_public_slug` puts `kund-` in front. A tighter nginx and ASGI match was not done (it
  changes the MCP routing on adx.se).
- **Routes.** The account part of a named link allows `_` (as `validate_public_slug` does);
  `app_segment_count` is `kontakter/segment/antal/` without a pk (it counts an unsaved segment)
  and takes JSON `{"rules"}` or the form fields; an extra `app_link` page for a named link
  (I.11's "Rapport"); the QR routes take the format (`qr.svg`, `qr.png`); follow-up and export
  live in `app_views/report.py`, the card sms in `app_views/contact_sms.py`. Old script versions
  keep loading from `static/utskick/s.<ver>.js` when their own hash is that version: **copy the
  old `s.js` there whenever it changes.**
- **Segments.** Wider rule set: `contact:<field>` rules for the contact's own fields, consent as
  "kan få erbjudanden" (eligible) or not, date fields also "inom kommande". The form picks one
  value per rule (several values are an ELLER group; the stored format still holds lists). "Kan
  få" uses the Kontakter header's check (consent and suppression); the weekly cap and the allowed
  countries apply only in the utskick. A segment used by an utskick that is not sent or cancelled
  cannot be deleted. A saved segment whose list or tag is gone is counted as stored and the row is
  marked (no 400).
- **Report.** The recipient list shows all channels by default (S2 showed sms only, so an
  email-only utskick's Förfrågningar tile opened an empty list). The funnel's and the link
  table's Förfrågan count recipients with a lead, not leads, so the number equals its list (the
  S2 tile Förfrågningar still counts leads). After retention the funnel comes from `stats`
  without links, the chart is gone, the link table shows the rolled-up sums, and Följ upp and
  Exportera are hidden. The SVG numbers are strings with a dot (the Swedish locale would render a
  comma).
- **Card sms.** No consent check and no weekly cap (it is not marketing); a suppression always
  stops it. A number another customer texted from the reply number in the last 30 days is refused
  when the sms is sent (the routing window: a reply would be held as ambiguous for both
  customers); the text never mentions another customer and the check never runs on a GET. A new thread is
  kind `direct` and its Inkorg lead starts "Klar" without `Lead.contact`, so the customer's own
  sms is never "1 förfrågan" on the card.
- **Links and the snippet.** The first `last_seen_at` comes from a test link on the
  Spårningsskript page (`https://<domän>/?adx=<install token>`, the shape of `ut` with its own
  signature purpose `adxsite`, never a click). The beacon adds `"v": 1` for the landing and
  `"e"` for `adxFlamingo.track(namn)` (a `site_visit` event with `"mal"`). A site that sends
  `Referrer-Policy: no-referrer` makes the browser send `Origin: null`, and its beacons are
  ignored (not worked around). The script is served as `text/javascript`. The install text says
  "Uteslut parametern adx ..." (imperative). Named links can be deleted, at most 200 per account;
  their numbers on Länkar are live (click rows and leads), and `links.rollup` also counts leads
  whose `attribution.channel` is `named`. `links.snippet_tag` is escaped as text on the settings
  page (it returned a safe string that rendered a live tag).
- **Integration.** The capitals route answers 301 only for a named link that exists (else the
  same 404, without a hop). Changing `public_slug` keeps the old address as an alias of the same
  account (`OldPublicSlug`, review fix); the card still warns and needs the box. A lead through a named link counts under "Varav via utskick" on the overview and is never
  Google's; its owner sms goes at once like any form lead (only utskick leads are batched with the
  replies). The demo's acceptance segment uses Sofia and the housing association, since every
  other demo customer with a service date has a lead in the last 30 days.

Review fixes (2026-10-10, security, correctness and UX reviews of the S4 working tree):

- **Old public addresses** (`OldPublicSlug`, `utskick.0005`). `customer_update` keeps the old
  `public_slug` for the account (`access.retire_public_slug`); `validate_public_slug` and
  `suggest_public_slug` treat another account's old address as taken (also a deleted account's:
  the row stays with `account` null); `named`, `named_folded`, the signup, thanks and privacy pages
  try the old address after the current one (`access.account_for_public_slug`), so printed posters
  and QR codes keep reaching the same customer. An account may take back its own old address (the
  row goes). `demo._settings` skips another account's old address.
- **`adx` is its own token** (`tokens.adx_token` / `read_adx`, purpose `adx`): `snippet_beacon`
  accepts only it (or the install token), a landing page's `ut` is never a beacon token and the
  reverse. `adx` is in `links.STRIP_PARAMS` (a pasted recipient's `adx` would otherwise follow every
  visit without a saved click). `ut` is not stripped: it only means something on Flamingo pages, and
  another site may have its own `ut` parameter.
- **Beacon**: `attribution.record_beacon` catches `OverflowError` (`"s": 1e999`, `Infinity`, `"inf"`
  also on the S2 landing-page beacon), and `snippet_beacon` answers 204 whatever the body holds.
- **Card sms**: the reply-number collision runs only on the POST (`contact_sms_checks(...,
  sending=True)`), says "Sms från kortet går inte till numret just nu, så sms:et skickades inte."
  and logs the reason with pks. The demo gets the form with "Demokontot skickar aldrig." (the POST
  refuses it). The cost cap says how to lift it ("Höj taket under Utskick, Inställningar." when
  `customer_manages_api`, else "Be ADX höja taket.").
- **"Svarar oftast"** skips STOPP, START and unsubscribe in Python (the inbound's keyword, else
  `inbound.stop.classify` on the kept body, so it also holds after the inbound rows are purged); the
  SQL exclude dropped every real reply.
- **Segments**: `consent.eligible_q(channel, purpose)` is the eligibility as a condition on the
  contact; "Kan få erbjudanden" and `segments.count` use it (one query, no account-wide subquery
  per segment in `for_contact`). "Fick X" (`got_utskick in`) never includes a contact who reported X
  as spam (a complaint suppresses email only, and the follow-up would reach them by sms);
  `reports.follow_up_count` agrees. "Fick inte X" does not include them either.
- **Segment builder**: the live count shows its 400 and 429 notes, writes "-" instead of stale
  numbers and retries once after a 429; every row shows "Villkor N" (a CSS counter, the same
  numbers as the notes); "1 kontakt", also while counting (final verification: the word after the
  number stayed as the page drew it, so a live 1 read "1 kontakter"; now `data-sg-unit`).
- **Report**: an utskick without links has no Klickade, Stannade, chart or "Följ upp de som inte
  klickade"; the funnel says "Procenten räknas av Skickade." (and " av skickade" for screen
  readers); funnel numbers look like links; the chart bars are violet with the peak in ink, the
  sentence under the chart is visible, the axis has a middle mark.
- **Länkar**: "+ N" counts recipients, not codes; an email-only utskick reads "klick.adx.se/m/..."
  and "personlig länk i mejlet till N mottagare"; cut destinations end in "..."; a link's page shows
  the whole target. Copy: "Lägg till fler webbplatser", "skapas automatiskt", "ADX:s godkännande",
  "Ladda ner". Spårningsskript documents `adxFlamingo.track('bokning')` (same page only) and has a
  "Står det Inte sett än" box (a redirect that drops `?adx=`, `Referrer-Policy` no-referrer or
  same-origin, CSP). The overview says "Varav via utskick och dina länkar" when named-link leads
  are counted. The generated privacy page mentions goals ("om du gjorde något som webbplatsen
  markerar, till exempel en bokning").

Checklist for the lead (in addition to J S4):

- After an automatic rollback to S3 code: pause scheduled utskick whose audience includes or
  excludes a segment. The S3 `audience.stored()` drops `segments`, so such an utskick would freeze
  without its exclusion, or with 0 recipients when it only had a segment.

- Before deploy: no production `UtskickSettings.public_slug` may be one of the reserved letters
  or start with a reserved prefix (expected none); read-only query, ask before running anything
  on the box.
- Giovanni: confirm "snippet domains are not free from review" above, and that "Fick X" (and so
  "Följ upp de som inte klickade") leaves out contacts who reported X as spam.
- J S4: ADX's own privacy policy line for the snippet (cookieless, the `adx` parameter, only
  visits from a link, and goals marked with `adxFlamingo.track`, for example a booking); ask
  Giovanni before changing the published page. The generated fallback
  `/utskick/<public_slug>/integritet/` already names the snippet domains.
- The 375 px pass in the real browser, logged in, over the segment builder, Listor, the full
  report, the recipient list with the channel chips, the export confirmation, Länkar, Ny länk, a
  link's page, Spårningsskript, the contact card with chips, "Svarar oftast" and the sms box, and
  the customer card's slug warning. The builders and the integration checked server-rendered
  pages at 375 px (no sideways overflow), not a logged-in session.
- Acceptance on a real test site: install the snippet on a page you own, open the test link
  (Installerat), then a klick link from a test mail, and see the visit on the contact card.

---

## A. Goals and non-goals

### Goals

1. A per-customer contact register inside Flamingo with per-channel consent tied to the exact
   address, exact consent proof, an append-only consent log and a suppression list that survives
   deletion and that no import can flip (D1, D6).
2. Sms and email utskick from the customer's own register, billed and capped like today's sms API
   (D4, D5), with an email builder in the Brev style only (D9).
3. Tracking from send to inquiry without cookies on `/lp/` (D10), reported in Flamingo and never
   pushed to Google (D11).
4. Replies and STOPP in the existing Inkorg, via the shared reply number (D4) and SES inbound in
   eu-west-1 (D8).
5. Safe sending: background-only, idempotent, paced under 46elks and SES limits, gated by global
   readiness switches and per-account kill switches, with automatic pauses on cost cap, delivery
   health and opt-out spikes (D13).
6. Every view usable at 375 px, all copy in plain Swedish, every locked option explained (D12).

### Non-goals (do not build)

- A/B tests, guessed age or gender, exit-intent popups, send-time optimisation, resend to
  non-openers (D7). Shop triggers (abandoned cart, back in stock, loyalty). Litmus or inbox preview
  services, surveys, social posting.
- Sms keyword signup ("SERVICE" to the reply number). Not scheduled and never mentioned in the UI.
- Plan fee lines on statements (D5). Pricing tiers are displayed nowhere.
- Uploading utskick leads or deals to Google (D11).
- Cookies or browser storage on `/lp/`, on the link hosts and in the own-site snippet (D10).
- Automatic emails to ADX's customers, including "your utskick was paused" mails. Those notices live
  in the app (I.5); agency alerts go to `INQUIRY_NOTIFICATION_EMAIL` (D12).
- An MCP tool that sends anything (guarded in `apps/assistant/tests.py`).
- Per-customer reply numbers, own click domains, MMS.
- Rendering any unbuilt feature, "Senare" chip or "kommer senare" text in the product.

---

## B. Data model

All models live in `apps/utskick/models.py` (one file, like `apps/flamingo`). Schemas are compact
pseudo-Django. `FK(X, CASCADE)` means `ForeignKey(X, on_delete=CASCADE)`. Every row belongs to one
`flamingo.FlamingoAccount` directly or through its parent, and every query filters on it (H.1).
Times are aware (`USE_TZ`); Swedish-day logic uses `apps.sms.pricing.STOCKHOLM`.

### B.0 Migration rule (every stage)

The previous release keeps running between `migrate` and `reload` in `server/deploy.sh`, and for
good after an automatic rollback (`rollback()` resets the code, runs `migrate || true` and restarts;
it never unapplies migrations). So **every AddField on a table that an earlier release writes must
be `null=True` or carry `db_default`**. Concretely: `SmsMessage.source db_default="api"`,
`Lead.attribution db_default=Value({}, output_field=JSONField())`, `Lead.activity_at
db_default=Now()`, `MonthlyStatement.by_source` like attribution; every later column on an utskick
table that an earlier stage already writes (S3 email columns on `Utskick`, S5 `Recipient.flow_run`)
the same way. New tables are free. Guard: `test_s1_guards.MigrationRuleTests` loads the migration
graph (`MigrationLoader`) and fails on any `AddField` to a model created in an earlier migration
file without `null=True` or `db_default` (allowlist: none).

Follow-up (S1 correctness review, before S2): the guard covers AddField only. The new tables'
foreign keys are created `DEFERRABLE INITIALLY DEFERRED` with no `ON DELETE`, so a release that
does not know a table (after a rollback) gets an `IntegrityError` when it deletes a row the table
points at (for example a `flamingo.Lead` an `utskick.Event` references, an account or a user).
Negligible for S1 (a rollback to pre-S1 code), real from S2 (S1 code against S2's recipient
rows). Before S2 ships: use Django 6's database-level `on_delete` (`DB_CASCADE`, `DB_SET_NULL`)
on links to tables an older release deletes from, or extend the guard to require it.

### B.1 Stage S1

```python
class Switchboard(Model):                         # S1, single row pk=1 ("nödstopp" and readiness)
    sms_enabled = Bool(default=False)             # global sms send switch; staff only, needs both ready_at below
    email_enabled = Bool(default=False)           # global email utskick switch; needs email_ready_at
    sms_paused_until = DateTime(null)             # circuit breaker (D.4)
    doi_ready_at = DateTime(null)                 # S1 checklist done: SES eu-west-1 identity + production access
    links_ready_at = DateTime(null)               # S2 checklist: k./klick. DNS, TLS, curls green
    sms_inbound_ready_at = DateTime(null)         # S2 checklist: 46elks sms_url set, real-phone STOPP test passed
    email_ready_at = DateTime(null)               # S3 checklist: config set, queues, inbound, live checks
    ses_max_rate = PositiveSmallInt(default=0)    # eu-west-1 GetAccount, daily; 0 = unknown -> setting
    ses_daily_quota = PositiveInt(default=0)
    hash_fingerprint = Char(64, blank)            # HMAC(UTSKICK_HASH_KEY, "utskick-fingerprint"), H.7
    link_fingerprint = Char(64, blank)            # same for UTSKICK_LINK_KEY
    last_tick_at = DateTime(null); last_tick_summary = JSON(default=dict)
    last_queue_poll_at = DateTime(null); last_elks_reconcile_at = DateTime(null)
    changed_by = FK(User, SET_NULL, null); changed_at = DateTime(null); note = Char(200, blank)
```

The ready fields are set only from `/manage/utskick/nodstopp/` by staff, each with a required note
("curl k.adx.se ok, cert till 2027-01-07"). Turning `sms_enabled` on is refused unless
`links_ready_at` and `sms_inbound_ready_at` are set; `email_enabled` unless `email_ready_at` is set
and `UTSKICK_EMAIL_LIVE` is true. The last step of the S2 and S3 checklists is "turn on sending".

```python
class UtskickSettings(Model):                     # S1, one per account
    account = O2O(FlamingoAccount, CASCADE, related_name="utskick")
    is_enabled = Bool(default=False)              # D2, set only by the agency
    enabled_at = DateTime(null); enabled_by = FK(User, SET_NULL, null); disabled_at = DateTime(null)
    public_slug = Slug(40, unique)                # agency-set from company_slug; reserved list below
    display_name = Char(80)                       # agency-set: From name on the ADX domain, consent texts,
                                                  # DOI, STOPP sms, footer. Customer sees it read-only.
    consent_text_sms = Char(200)                  # "Ja, jag vill få erbjudanden från Exempelrör via sms."
    consent_text_email = Char(200)
    lp_consent = Bool(default=True)               # checkboxes on /lp/ forms (effective only when collect_ok, H.5)
    privacy_url = URL(500, blank)                 # https only; empty = generated page /utskick/<slug>/integritet/
    pref_email_note = Char(120, blank)            # customer text under "E-post med erbjudanden" (no default)
    sms_window = JSON(default={"weekday": [9, 20], "weekend": [10, 18]})   # hours, bounds 8..21
    weekly_cap_sms = PositiveSmallInt(default=2)  # reklam per contact per ISO week
    weekly_cap_email = PositiveSmallInt(default=4)
    open_tracking = Bool(default=False)           # D9; pixel only for tracking_ok recipients (H.5)
    email_reply_mode = Char(8, choices=inbox|own, default inbox)
    own_reply_to = Email(blank); own_reply_to_confirmed_at = DateTime(null)  # verified domain or one-time link
    unsubscribe_text = Char(300, blank)           # extra line on the unsubscribe page
    contact_limit = PositiveInt(default=25000)    # agency can raise on the card
    notify_on_reply = Bool(default=True)          # batched owner sms (G.1)
    reply_notice_at = DateTime(null)              # last batched owner notice
    sending_blocked = Bool(default=False)         # staff kill flag (D.9); blocked_reason Char(200, blank)
    email_daily_cap = PositiveInt(default=0)      # 0 = ramp rule (D.9); agency can raise
    email_first_sent_at = DateTime(null); email_probe_passed_at = DateTime(null)
    first_utskick_alerted = Bool(default=False)
    created_at, updated_at
```

`UtskickSettings` is its own row for the same reason `FlamingoAccount` is: the customer card form
saves unchecked boxes as false (`flamingo/models.py` lines 3 to 7). `access.settings_for(account)`
returns the row or an unsaved default. **Reserved `public_slug` values**: adx, admin, abuse,
postmaster, bounce, bekrafta, noreply, info, support, security, svar, utskick, k, klick, www, mail,
integritet, val, tack, plus anything in `RESERVED_SLUGS`. The customer sees `display_name` and the
slug read-only ("Be ADX ändra det.").

```python
class DpaVersion(Model):                          # S1, published by staff from the bitradesavtal page
    version = Char(20, unique)                    # "2026-10"
    text = Text()                                 # plain-text snapshot of the BlockPage at publish time
    sha256 = Char(64)
    published_at = DateTime(default=now); published_by = FK(User, SET_NULL, null)
    is_current = Bool(default=False)              # exactly one (partial unique where is_current)

class DpaAcceptance(Model):                       # S1, append-only
    account = FK(FlamingoAccount, CASCADE, related_name="utskick_dpa")
    version = FK(DpaVersion, PROTECT)
    accepted_at = DateTime(default=now)
    accepted_by = FK(User, SET_NULL, null); accepted_as_staff = Bool(default=False)
    staff_statement = Char(300, blank)            # required when staff: who at the customer approved, and how
    ip_hash = Char(64, blank)                     # flamingo.limits.ip_hash
    indexes: (account, -accepted_at)
```

`dpa_ok(account)` is true when the latest acceptance references the current version (always true
for the demo account). No current version means acceptance is refused with "Avtalssidan saknas.
Kontakta ADX." and the agency is alerted once. On a version bump existing data stays usable and
sending continues, but new imports, new contacts from any path, new confirmations and API writes
need re-acceptance (`access.can_collect`, H.1).

```python
class Contact(Model):                             # S1
    account = FK(FlamingoAccount, CASCADE, related_name="utskick_contacts")
    kind = Char(10, choices=person|company, default person)
    first_name = Char(60, blank); last_name = Char(80, blank)
    company_name = Char(120, blank)
    org_number = Char(10, blank)                  # 10 digits, legal persons only (normalize.org_number)
    phone = Char(16, blank)                       # E.164 from apps.sms.numbers.parse, "" if none/invalid
    phone_country = Char(2, blank)
    email = Char(254, blank)                      # stripped, lower-cased, validate_email
    email_state = Char(10, choices=ok|bounced, default ok)
    email_soft_bounces = PositiveSmallInt(default=0)   # 5 in a row -> bounced
    email_bounced_at = DateTime(null)
    fields = JSON(default=dict)                   # FieldDef.key -> str (dates ISO yyyy-mm-dd)
    tags = M2M(Tag, related_name="contacts", blank)
    source = Char(10, choices=import|form|signup|manual|api|reply|lead)
    source_detail = Char(200, blank)              # "/lp/varmepump", "import 12"
    search_text = Text(blank)                     # lower-cased name, phone digits, email, field values
    last_activity_at = DateTime(null); last_activity_kind = Char(20, blank)
    inactive_flagged_at = DateTime(null)          # E.7: no basis and 24 months without activity
    created_at = DateTime(default=now); updated_at = DateTime(auto_now)
    constraints:
      Unique(account, phone) where phone != ""    name utskick_contact_phone
      Unique(account, email) where email != ""    name utskick_contact_email
    indexes: (account, -last_activity_at), (account, -created_at), (account, kind)
```

Normalisation (`apps/utskick/normalize.py`): phone via `apps.sms.numbers.parse` (region SE default,
mobile or fixed-or-mobile only; a landline goes to `fields["telefon"]` as text, not `phone`); never
`flamingo.sms.normalize_phone` (the two disagree). Org number: digits only, 10 digits (12 with
century stripped); kept only when the third digit is 2 or higher (legal person). A value that looks
like a personnummer (third digit 0 or 1, i.e. enskild firma) is **not stored** and the row is
treated as a person. **Personnummer anywhere**: `normalize.looks_like_personnummer(v)` is
`^(19|20)?\d{6}[-+]?\d{4}$` plus the Luhn check; a field value that matches is refused ("Personnummer
sparas inte i Kontakter."), on manual edit, import and API alike. Search uses
`search_text__contains` on the account's rows (no pg_trgm: needs superuser).

**Address changes**: `contacts.change_address(contact, channel, new_value, actor)` is the only
writer of `phone` and `email` after creation. A change sets that channel's consent to `missing` (or
`unsubscribed` if the new value hash is suppressed), writes a ConsentLog row "Adressen ändrades",
and re-derives `company` (H.5). An import never overwrites a non-empty phone or email: it reports a
conflict row instead. Unique violations are caught (`IntegrityError` -> form error "Numret finns
redan på en annan kontakt.") and logged with pks only.

```python
class Consent(Model):                             # S1, current state per channel
    contact = FK(Contact, CASCADE, related_name="consents")
    channel = Char(5, choices=sms|email)
    status = Char(12, choices=yes|existing|company|pending|missing|declined|unsubscribed, default missing)
    basis = Char(12, choices=consent|existing_customer|company|none, default none)
    value_hash = Char(64, blank)                  # hash of the address the consent was given for
    text_shown = Text(blank)                      # exact text the person saw, company name filled in
    tracking_ok = Bool(default=False)             # the text shown included the open-tracking sentence
    evidence = Char(300, blank)                   # customer's note, e.g. "kassan, från 2024"
    source = Char(16, choices=import|lp_form|signup|preference|doi|confirm|manual|api|stop|start|link|list_unsub|complaint|reply|address)
    source_detail = Char(200, blank)              # page path, import id, utskick id
    collected_at = DateTime(null)                 # when the person said yes, as known
    confirmed_at = DateTime(null)                 # DOI click (email) or confirm-link click (sms)
    confirm_sent_at = DateTime(null); confirm_count = PositiveSmallInt(default=0)
    changed_at = DateTime(default=now)
    changed_by = FK(User, SET_NULL, null); changed_by_label = Char(120, blank)
    constraints: Unique(contact, channel)
    indexes: (channel, status), partial (status="pending", confirm_sent_at null) for the tick
```

Status meaning and who may set it:

| Status | UI chip | Set by | Reklam | Information |
|---|---|---|---|---|
| yes | "Sms: ja" / "E-post: ja" | the person (LP checkbox, signup after confirmation, DOI click, preference page after confirmation, START after confirmation), import or manual with evidence | yes | yes |
| existing | "Sms: befintlig kund" | import option "befintliga kunder", manual with evidence (MFL 19 § second paragraph) | yes | yes |
| company | "E-post (företag)" | derived (H.5): kind company, legal org number, non-freemail domain, email only, only from `missing` | email yes (opt-out basis) | yes |
| pending | "Väntar på bekräftelse" | form or signup until the DOI or confirm click | no | yes |
| missing | "E-post saknas" (no address) / "Inget samtycke" | default, import "Vet inte", address change | no | yes |
| declined | "Vill inte ha erbjudanden" | the person on the preference page, manual on request | no | yes |
| unsubscribed | "Avregistrerad (STOPP)" / "Avregistrerad" | STOPP, `/s/`, `/a/`, List-Unsubscribe, complaint, "Avregistrera mig från allt", manual; always with a Suppression | no | no |

Rules enforced in `consent.set_status()` (the only writer):
- Import, manual and API move `missing -> yes|existing` only. They never touch `pending`,
  `declined`, `unsubscribed`, or a channel whose value hash is in `Suppression`.
- `eligible(contact, channel, purpose)` for reklam needs `status in (yes, existing, company)`, no
  suppression, `email_state == ok` for email, and for yes/existing `value_hash ==
  hash(current address)`. For information: no suppression and `email_state == ok` (H.6).
- Only the person, proved by a confirmation (DOI click, sms confirm link) or a fresh personal link
  within its limits (E.5), can leave `declined`/`unsubscribed` or lift a suppression.
- Anyone with access can set `declined` or `unsubscribed`.

```python
class ConsentLog(Model):                          # S1, append-only proof
    account = FK(FlamingoAccount, CASCADE)
    contact = FK(Contact, SET_NULL, null)         # survives contact deletion as pseudonymous proof
    channel = Char(5); value_hash = Char(64)      # same hash as Suppression
    old_status = Char(12); new_status = Char(12); basis = Char(12)
    text_shown = Text(blank); evidence = Char(300, blank)   # evidence blanked on GDPR delete (H.4)
    source = Char(16); source_detail = Char(200, blank)
    utskick = FK("Utskick", SET_NULL, null)       # e.g. STOPP after utskick 41 (S2 migration adds the FK)
    by_user = FK(User, SET_NULL, null); by_label = Char(120, blank); by_staff = Bool(default=False)
    ip_hash = Char(64, blank)                     # public forms only
    at = DateTime(default=now)
    indexes: (contact, -at), (account, value_hash), (value_hash, -at)
```

`save()` refuses updates (`if self.pk: raise`) except the GDPR blanking in `contacts.delete_contact`
(an explicit `QuerySet.update`); deletion only through `retention.purge_consent_logs()`.

```python
class Suppression(Model):                         # S1
    account = FK(FlamingoAccount, CASCADE, related_name="utskick_suppressions")
    channel = Char(5, choices=sms|email)
    value_hash = Char(64)                         # tokens.value_hash(channel, normalized value)
    reason = Char(12, choices=stop|link|list_unsub|preference|bounce|complaint|manual|import|erasure|reply)
    utskick = FK("Utskick", SET_NULL, null)       # added in S2
    created_at = DateTime(default=now)
    note = Char(200, blank)
    constraints: Unique(account, channel, value_hash)
```

`value_hash = HMAC-SHA256(UTSKICK_HASH_KEY, f"{channel}:{value}")` with value E.164 or lower-cased
email (plus-tags kept). `UTSKICK_HASH_KEY` is a stable env secret, **not** `SECRET_KEY`, required in
production and fingerprinted (H.7). Import rows marked "avregistrerad" in the customer's file create
suppressions with reason `import` (one-way, allowed).

```python
class FieldDef(Model):                            # S1, extra fields ("extrafält")
    account = FK(FlamingoAccount, CASCADE, related_name="utskick_fields")
    key = Slug(40); label = Char(60)
    kind = Char(8, choices=text|date|number|choice)
    choices = JSON(default=list); order = PositiveSmallInt(default=0)
    show_in_list = Bool(default=False)            # at most one per account: the list sub-line ("Regnr ABC 123")
    created_at
    constraints: Unique(account, key); partial Unique(account) where show_in_list   # max 30 per account (view)

class Tag(Model):                                 # S1
    account = FK(FlamingoAccount, CASCADE, related_name="utskick_tags")
    name = Char(40)
    constraints: Unique(account, name)

class ContactList(Model):                         # S1 ("Lista")
    account = FK(FlamingoAccount, CASCADE, related_name="utskick_lists")
    name = Char(80); description = Char(200, blank)
    created_at; created_by = FK(User, SET_NULL, null)
    constraints: Unique(account, name)

class ListMembership(Model):                      # S1
    list = FK(ContactList, CASCADE, related_name="memberships")
    contact = FK(Contact, CASCADE, related_name="memberships")
    added_at = DateTime(default=now)
    source = Char(10, choices=import|manual|signup|flow|api|report)
    constraints: Unique(list, contact); indexes: (contact)
```

```python
class ImportJob(Model):                           # S1
    account = FK(FlamingoAccount, CASCADE, related_name="utskick_imports")
    created_by = FK(User, SET_NULL, null); created_as_staff = Bool
    file = FileField(storage=private_storage, upload_to="utskick-import/%Y/%m/", blank)  # PRIVATE_MEDIA_ROOT
    csv_path = Char(300, blank)                   # normalised UTF-8 CSV written by convert (S1 import)
    original_name = Char(200); kind = Char(5, choices=csv|xlsx|paste)
    size = PositiveInt; delimiter = Char(1, blank); encoding = Char(20, blank)
    header = JSON(default=list)                   # column names
    sample = JSON(default=list)                   # first 5 rows; cleared on done, cancel, failure, abandon
    row_count = PositiveInt(default=0)
    mapping = JSON(default=dict)                  # {"0": "full_name", "1": "phone", "3": "field:regnummer", "6": "skip"}
    consent = JSON(default=dict)                  # {"choice": "consent"|"existing"|"unknown", "sms": bool, "email": bool, "where": "kassan, 2024"}
    target_list = FK(ContactList, SET_NULL, null); target_tag = FK(Tag, SET_NULL, null)
    status = Char(10, choices=uploaded|converting|mapping|consent|analysing|review|importing|done|failed|cancelled)
    in_request = Bool(default=False)              # small files (<= 2 000 rows) run inside the request
    byte_offset = PositiveBigInt(default=0)       # resume point in csv_path
    progress = PositiveInt(default=0)             # rows processed
    counts = JSON(default=dict)                   # new, updated, suppressed, errors, conflicts
    errors = JSON(default=list)                   # [{"row": 17, "column": "Mobil", "reason": "Ogiltigt nummer"}], max 1000, no values
    started_at, finished_at, file_deleted_at = DateTime(null); created_at
    indexes: (status, created_at)
```

```python
class SignupForm(Model):                          # S1, the public signup page (one per account)
    account = O2O(FlamingoAccount, CASCADE, related_name="utskick_signup")
    title = Char(80); intro = Char(400, blank)
    channels = JSON(default=["email"])            # "sms" offered only when Switchboard.links_ready_at (S2)
    add_to_list = FK(ContactList, SET_NULL, null); add_tags = M2M(Tag, blank)
    is_active = Bool(default=False)               # created inactive; the customer turns it on
    created_at, updated_at
```

```python
class Event(Model):                               # S1 (kinds grow per stage)
    account = FK(FlamingoAccount, CASCADE)
    contact = FK(Contact, CASCADE, related_name="events")
    kind = Char(20)                               # see list below
    at = DateTime(default=now)
    utskick = FK("Utskick", SET_NULL, null)       # S2
    recipient = FK("Recipient", SET_NULL, null)   # S2
    lead = FK("flamingo.Lead", SET_NULL, null)
    data = JSON(default=dict)                     # small, no free text from third parties
    idempotency_key = Char(64, blank)             # S5 API events
    constraints: Unique(account, idempotency_key) where idempotency_key != ""  (S5)
    indexes: (contact, -at), (account, kind, -at)
```

Event kinds: `imported`, `signup`, `lead`, `test_send` (S1, test_send used from S2); `lp_visit`,
`call_click`, `reply`, `stop`, `start` (S2); `opened` (S3); `site_visit` (S4); `api_event`,
`flow_entered`, `flow_exited` (S5). Things that already have their own row (recipient
sent/delivered/failed, clicks, consent changes, thread messages) are **not** copied into `Event`:
the timeline merges sources at read time (`timeline.for_contact`, S1). This keeps the table small
on a box whose disk is 76% full.

```python
class ExportLog(Model):                           # S1
    account = FK(FlamingoAccount, CASCADE); user = FK(User, SET_NULL, null); as_staff = Bool
    kind = Char(12, choices=contacts|contact|utskick)   # full register, one person (GDPR), recipients
    rows = PositiveInt; at = DateTime(default=now)
    indexes: (account, -at)

class Counter(Model):                             # S1, exact DB rate limits (LocMem is per worker)
    scope = Char(20)                              # "link_miss", "signup_ip", "optin_addr", "optin_addr_all",
                                                  # "optin_hour", "link_check", "export", "inbound_mail", "test_send"
    key = Char(80)                                # ip_hash, f"{account}:{value_hash}", "" for global
    window = DateTime()                           # start of a fixed window (hour or Stockholm day)
    count = PositiveInt(default=0)
    constraints: Unique(scope, key, window)
```

`limits.hit(scope, key, window, limit)` is one `INSERT ... ON CONFLICT (scope, key, window) DO
UPDATE SET count = utskick_counter.count + 1 RETURNING count` and returns whether the limit is
exceeded. Fixed windows, not rolling. `utskick_daily` deletes rows older than 2 days.

### B.2 Stage S2

```python
class Utskick(Model):                             # S2 (email columns added in S3 with db_default, B.0)
    account = FK(FlamingoAccount, CASCADE, related_name="utskick_set")
    name = Char(120)                              # "syns bara för dig"
    purpose = Char(12, choices=reklam|information)
    info_reason = Char(14, blank, choices=bokning|arende|oppettider|driftstorning|annat)  # required for information
    info_reason_text = Char(200, blank)           # required for "annat"
    content_override = JSON(default=dict)         # staff override of an information content block: {by, at, reason}
    channel_mode = Char(16, choices=sms_only|email_only|sms_then_email|both)
    audience = JSON(default=dict)                 # {"lists":[], "tags":[], "segments":[], "contacts":[],
                                                  #  "exclude": {"lists":[], "tags":[], "segments":[], "recent_days": 14}}
    # sms
    sms_body = Text(blank)                        # max 1000 chars before merge
    sms_sender_kind = Char(6, choices=reply|name, default reply)
    sms_sender_name = Char(11, blank)             # one of SmsAccount.senders when kind=name
    # email (S3)
    subject = Char(150, blank); preheader = Char(150, blank)
    email_doc = JSON(default=dict)                # {"blocks": [...]} Brev registry, same block JSON as pagebuilder
    email_rev = PositiveInt(default=0)            # optimistic lock, like LandingPage.rev
    accent = Char(7, blank)                       # ^#[0-9A-Fa-f]{6}$
    logo_position = Char(6, choices=left|center|none, default left)
    sender_domain = FK(SenderDomain, PROTECT, null)  # null = ADX domain utskick.adx.se
    from_name = Char(80, blank)                   # editable only with a verified own domain; else display_name
    merge_fallbacks = JSON(default=dict)          # {"förnamn": ""}
    confirmed_terms = JSON(default=list)          # [{"label": "Erbjudandet gäller", "value": "till 31 oktober"}]
    terms_confirmed_by = FK(User, SET_NULL, null); terms_confirmed_at = DateTime(null)
    open_tracking = Bool(default=False)           # copied from settings at creation, editable
    text_override = Text(blank)                   # customer-edited plain-text version
    # time and state
    send_mode = Char(5, choices=now|at, default at); scheduled_at = DateTime(null)
    status = Char(14, choices=draft|scheduled|freezing|sending|paused_cap|paused_health|paused|sent|cancelled)
    pause_reason = Char(20, blank)                # see I.5 for every value and its copy
    hold_until = DateTime(null)                   # email probe wait (D.9); not a pause
    status_changed_at = DateTime(default=now)
    # confirmation (D12)
    confirm_nonce = Char(32, blank)               # one-time value in the review form
    confirmed_at = DateTime(null); confirmed_by = FK(User, SET_NULL, null)
    confirmed_as_staff = Bool(default=False)
    confirm_summary = JSON(default=dict)          # exactly the I.6 numbers at confirm time
    # freeze and progress
    freeze_cursor = PositiveBigInt(default=0)     # last contact pk frozen (chunked freeze)
    frozen_at = DateTime(null); frozen_counts = JSON(default=dict)
    started_at, finished_at = DateTime(null)
    stats = JSON(default=dict)                    # cached aggregates, final at finish, kept after retention
    flow_step = O2O("FlowStep", CASCADE, null)    # S5: flow send steps own a hidden Utskick
    created_by = FK(User, SET_NULL, null); created_at; updated_at
    indexes: (account, status, -created_at), (status, scheduled_at), partial (account, -created_at) where flow_step null
```

State machine (only `sending.transition(utskick, to, reason)` writes it, with a conditional `UPDATE
... WHERE status = <expected>` like the offer system; hidden flow-step utskick are excluded, B.5):

```
draft --confirm--> scheduled --due (tick)--> freezing --frozen--> sending --no queued/sending left--> sent
  ^                   |                         |                 |  ^
  |                   +--edit (unconfirm)--+    |                 |  +--resume (re-confirm, re-check)--+
  +-------------------<--------------------+    |                 +--> paused_cap | paused_health | paused
draft|scheduled|freezing|paused_* --cancel--> cancelled (queued recipients -> cancelled)
scheduled|freezing|sending --account disabled / Flamingo off / customer inactive--> paused (account_disabled)
scheduled --due more than 3 h late or on a later Stockholm date--> paused (late)
```

Editing a scheduled utskick returns it to draft and clears the confirmation. A paused utskick keeps
its frozen recipients; resume re-runs the pre-checks and, for `late`, `audience_grew` and
`account_disabled`, needs a new confirmation.

```python
class Recipient(Model):                           # S2
    utskick = FK(Utskick, CASCADE, related_name="recipients")
    contact = FK(Contact, SET_NULL, null)         # null after GDPR deletion (address blanked too)
    flow_run = FK("FlowRun", SET_NULL, null)      # S5, db_default null
    channel = Char(5, choices=sms|email)
    address = Char(254)                           # frozen E.164 or email
    merge = JSON(default=dict)                    # frozen merge values {"förnamn": "Anna"}
    basis = Char(12, blank)                       # consent basis at freeze (footer reason line)
    tracking_ok = Bool(default=False)             # copied from Consent at freeze (pixel, H.5)
    status = Char(10, choices=queued|skipped|sending|sent|delivered|failed|bounced|complained|unknown|cancelled)
    skip_reason = Char(16, blank)                 # see I.8 "Hoppades över" for the full list and copy
    not_before = DateTime(null)                   # window, rate-limit or breaker requeue
    attempts = PositiveSmallInt(default=0); claimed_at = DateTime(null)
    sent_at, delivered_at = DateTime(null)
    sms_message = FK("sms.SmsMessage", SET_NULL, null, related_name="utskick_recipients")
    sms_sender = Char(16, blank)                  # actual sender used (reply number or name, D.4 collision)
    parts = PositiveSmallInt(default=0)           # sms parts, copied after send
    ses_message_id = Char(100, blank, db_index)   # S3
    error = Char(200, blank)
    first_clicked_at = DateTime(null); click_count = PositiveSmallInt(default=0)  # human clicks only
    bot_hits = PositiveSmallInt(default=0)        # bots and scanners, counted not stored
    opened_at = DateTime(null)                    # S3, indication
    replied_at = DateTime(null); stopped_at = DateTime(null)
    simulated = Bool(default=False)               # demo account
    created_at
    constraints:
      Unique(utskick, contact, channel) where contact not null and flow_run null
      Unique(flow_run, utskick, channel) where flow_run not null                  (S5)
    indexes:
      partial (channel, not_before, id) where status = "queued"   # the tick's work queue
      (utskick, status)
      (contact, channel, sent_at)                                  # weekly cap
      partial (status, claimed_at) where status = "sending"        # crash recovery
```

```python
class AllowedHost(Model):                         # S2, external link hosts approved by the agency (E.8)
    account = FK(FlamingoAccount, CASCADE, related_name="utskick_hosts")
    host = Char(253)                              # lower-case, IDNA
    status = Char(8, choices=pending|approved|refused)
    requested_by = FK(User, SET_NULL, null); requested_at = DateTime(default=now)
    decided_by = FK(User, SET_NULL, null); decided_at = DateTime(null); note = Char(200, blank)
    constraints: Unique(account, host)
```

```python
class TrackedLink(Model):                         # S2 (named-link fields in S4)
    account = FK(FlamingoAccount, CASCADE, related_name="utskick_links")
    utskick = FK(Utskick, CASCADE, null, related_name="links")   # null for named links (S4)
    kind = Char(8, choices=lp|external|named)
    key = Char(40, blank)                         # sms placeholder name: {länk:varmepump}
    campaign = FK("flamingo.Campaign", SET_NULL, null)  # lp kind; campaign.account_id must equal account_id
    destination = Char(500)                       # absolute http(s) URL (E.8) or campaign.landing_url
    label = Char(120, blank)                      # "Boka tid (knapp)"
    add_utm = Bool(default=True)
    block_id = Char(16, blank); position = PositiveSmallInt(default=0)  # email block, S6 click map
    slug = Slug(40, blank)                        # S4 named: klick.adx.se/<public_slug>/<slug>
    human_clicks = PositiveInt(default=0); bot_hits = PositiveInt(default=0); leads = PositiveInt(default=0)
    created_at
    constraints: Unique(utskick, key) where utskick not null and key != ""
                 Unique(account, slug) where slug != ""
```

```python
class LinkCode(Model):                            # S2, sms only
    code = Char(8, unique)                        # 6 chars [A-Za-z0-9]; case-sensitive (deterministic collation)
    kind = Char(8, choices=link|person|confirm)   # click link | /s/ and /p/ | confirm link (/b/)
    account = FK(FlamingoAccount, CASCADE)        # copied at freeze, survives recipient retention
    channel = Char(5, default="sms")
    value_hash = Char(64)                         # hash of the address the code was sent to
    recipient = FK(Recipient, SET_NULL, null, related_name="codes")
    contact = FK(Contact, SET_NULL, null)         # confirm codes
    link = FK(TrackedLink, CASCADE, null)         # kind link only
    purpose = Char(12, blank)                     # confirm: signup|start|pref_on
    expires_at = DateTime(null)                   # confirm codes: 24 h
    used_at = DateTime(null)
    created_at
    constraints: Unique(recipient, link) where kind = "link"
```

Email links do not use `LinkCode`: they are stateless signed paths (E.2), which saves one row per
recipient per link.

```python
class Click(Model):                               # S2, human and scanner clicks only (bots are counted, E.3)
    account = FK(FlamingoAccount, CASCADE)
    utskick = FK(Utskick, SET_NULL, null); recipient = FK(Recipient, SET_NULL, null)
    link = FK(TrackedLink, SET_NULL, null); contact = FK(Contact, SET_NULL, null)
    channel = Char(5, choices=sms|email|named)
    kind = Char(8, choices=human|scanner)         # scanner rows kept 14 days (may be upgraded by a beacon)
    at = DateTime(default=now)
    repeat_count = PositiveSmallInt(default=0)    # hits after the 20-rows-per-hour cap (E.3)
    device = Char(8, blank); os = Char(20, blank); browser = Char(20, blank)  # analytics.utils.parse_user_agent
    ip_hash = Char(64, blank)                     # flamingo.limits.ip_hash, never the IP
    lp_visits = PositiveSmallInt(default=0); first_visit_at = DateTime(null)
    engaged_seconds = PositiveSmallInt(default=0) # max of beacons, capped 1800
    beacon_at = DateTime(null)                    # 10 s write throttle
    called = Bool(default=False)                  # call_click with this token
    indexes: (utskick, at), (recipient, link, at), (account, -at)
```

```python
class Thread(Model):                              # S2, a reply conversation
    account = FK(FlamingoAccount, CASCADE, related_name="utskick_threads")
    contact = FK(Contact, SET_NULL, null)         # null: no contact (DPA missing) ; deleted explicitly on GDPR delete
    channel = Char(5, choices=sms|email)
    kind = Char(6, choices=reply|stop|direct)
    lead = O2O("flamingo.Lead", CASCADE, null, related_name="reply_thread")  # the Inkorg item
    utskick = FK(Utskick, SET_NULL, null)         # the utskick being answered
    address = Char(254)                           # the other party's phone or email
    unread = Bool(default=True)
    looks_like_stop = Bool(default=False)         # G.1 phrase flag
    last_in_at, last_out_at = DateTime(null); created_at
    indexes: (account, contact, channel, -last_in_at)

class ThreadMessage(Model):                       # S2
    thread = FK(Thread, CASCADE, related_name="messages")
    direction = Char(3, choices=in|out)
    body = Text()                                 # inbound: max 20 000 chars, quoted history stripped
    subject = Char(200, blank)                    # email
    at = DateTime(default=now)
    sms_message = FK("sms.SmsMessage", SET_NULL, null)   # outbound sms
    inbound = FK("InboundMessage", SET_NULL, null)
    email_message_id = Char(200, blank)           # outbound SES id (S3)
    attachments = JSON(default=list)              # [{"name", "size"}], files are never stored
    sent_by = FK(User, SET_NULL, null); sent_as_staff = Bool(default=False)
    status = Char(8, choices=received|sending|sent|failed, default received)
    indexes: (thread, at)

class InboundMessage(Model):                      # S2 (sms), S3 (email)
    channel = Char(5, choices=sms|email)
    provider_id = Char(120)                       # 46elks id or SES mail.messageId
    from_address = Char(254); to_address = Char(254)
    body = Text(blank)                            # cleared after routing (lives in ThreadMessage)
    subject = Char(200, blank)
    received_at = DateTime()
    account = FK(FlamingoAccount, SET_NULL, null); contact = FK(Contact, SET_NULL, null)
    routed_via = Char(30, blank)                  # "sms:1234", "token:r:567", "thread:89"
    status = Char(10, choices=pending|routed|ambiguous|unroutable|stop|start|autoreply|spam|ignored|counted)
    meta = JSON(default=dict)                     # verdicts, sizes, s3 key, accounts affected by STOPP; no bodies
    created_at
    constraints: Unique(channel, provider_id)     # 46elks retries, reconcile and SQS redelivery are idempotent
    indexes: (status, -received_at), (from_address, -received_at)
```

Why replies are `Lead` rows plus a thread, not a parallel inbox model: the Inkorg
(`app_views/inbox.py`: `lead_list`, `lead_detail`, `FILTERS`, the `new_lead_count` badge,
pagination, `notify_new_lead`, demo seed, guard tests) is built entirely on `Lead`. A second model
would force a union queryset into the list, the badge and the pagination. So each thread gets one
`Lead` with `source="reply"` (`Lead.source` is `max_length=10`; `"email_reply"` would not fit and
is not needed: the channel lives on `Thread`). At thread creation the lead copies name and phone
(or email) from the Contact; on each inbound `Lead.message` becomes the first 200 characters of the
reply and `Lead.activity_at = now`. `Lead.status` doubles as the handling state for replies: `new`
is "Ny", `contacted` is shown as "Klar"; `quote`, `won`, `lost` stay available. A STOPP creates a
thread of kind `stop` whose lead starts as `contacted` ("Avregistrerad automatiskt"), so it never
raises the badge. A new inbound on a thread whose lead is `contacted` moves it back to `new`.

### B.3 Stage S3

```python
class SenderDomain(Model):                        # S3, customer's own domain
    account = FK(FlamingoAccount, CASCADE, related_name="utskick_domains")
    domain = Char(253)                            # lower-case, IDNA, registrable domain or subdomain of it
    from_local = Char(64, default="hej")          # hej@exempelror.example
    from_name = Char(80)
    mail_from_sub = Char(40, default="studs")     # studs.exempelror.example (MAIL FROM)
    status = Char(8, choices=pending|verified|failed|expired|removed)
    ses_created = Bool(default=False)             # we created the SES identity; only then may we delete it
    dkim_tokens = JSON(default=list)              # 3 tokens from CreateEmailIdentity
    checks = JSON(default=dict)                   # per record: ok|missing|wrong + seen value (dnspython)
    ses_snapshot = JSON(default=dict)             # last GetEmailIdentity
    checked_at, verified_at, probe_passed_at = DateTime(null)
    created_by = FK(User, SET_NULL, null); created_at
    constraints: Unique(domain) where status = "verified"
```

Domain claims (`email/domains.py`): refused for `adx.se` and every subdomain, freemail and ISP
domains (`utskick/freemail.py`), public suffixes (`publicsuffix` list shipped in the module, no
dependency), and any domain that is the parent or child of another account's pending or verified
domain ("Domänen används redan. Kontakta ADX."). A pending row expires after 14 days; another
account's verification cancels it; a second claim alerts the agency. On `AlreadyExistsException`
from `CreateEmailIdentity` the claim is refused with the same text and an agency alert; we never
adopt an existing identity. `DeleteEmailIdentity` is called only when `ses_created` is true.

```python
class EmailImage(Model):                          # S3, email rendition of a MediaAsset
    account = FK(FlamingoAccount, CASCADE)
    asset = FK("flamingo.MediaAsset", SET_NULL, null, related_name="email_images")
    purpose = Char(6, choices=content|logo|video|avatar)  # video: thumbnail with a drawn play button
    file = ImageField(upload_to=random path under MEDIA_ROOT/utskick-img/)  # public, absolute URL in mail
    format = Char(4, choices=jpeg|png); width, height, bytes = PositiveInt
    created_at
    constraints: Unique(asset, purpose, width) where asset not null

class EventReceipt(Model):                        # S3, idempotency for SES events read from SQS
    key = Char(140, unique)                       # f"{mail.messageId}:{eventType}"
    at = DateTime(default=now)                    # kept 3 days
```

S3 migration also adds the email columns on `Utskick` listed in B.2 (with `db_default`, B.0).

### B.4 Stage S4

```python
class Segment(Model):                             # S4
    account = FK(FlamingoAccount, CASCADE, related_name="utskick_segments")
    name = Char(80)
    rules = JSON()            # {"all": [rule | {"any": [rule, ...]}]}
                              # rule = {"f": "list"|"tag"|"field:<key>"|"consent"|"kind"|"got_utskick"|"opened"|
                              #         "clicked"|"visited_lp"|"lead"|"replied"|"source"|"created",
                              #         "op": "in"|"not_in"|"eq"|"before_days"|"within_days"|"before_months"|..., "v": ...}
    cached_count, cached_sms, cached_email = PositiveInt(default=0); counted_at = DateTime(null)
    created_by; created_at; updated_at
    constraints: Unique(account, name)

class SiteSnippet(Model):                         # S4, own-site script
    account = FK(FlamingoAccount, CASCADE, related_name="utskick_sites")
    domain = Char(253)                            # exempelror.example (exact host or registrable domain)
    key = Char(16, unique)                        # public identifier, not a secret
    last_seen_at = DateTime(null); created_at
    constraints: Unique(account, domain)
```

### B.5 Stage S5

```python
class Flow(Model):                                # S5 ("Automatiska flöden")
    account = FK(FlamingoAccount, CASCADE, related_name="utskick_flows")
    name = Char(120); purpose = Char(12, choices=reklam|information)
    info_reason = Char(14, blank); info_reason_text = Char(200, blank)   # as on Utskick
    trigger = Char(12, choices=list_added|new_contact|date_field|fixed_dates|api_event)
    trigger_config = JSON()       # {"list": 3} | {"source": "form"} | {"field": "senaste_service", "offset_days": 150, "hour": 9}
                                  # | {"dates": ["10-01", "03-15"], "hour": 9} | {"event": "offert_skickad"}
    audience_list = FK(ContactList, SET_NULL, null)  # optional restriction
    status = Char(8, choices=draft|active|paused|archived)
    reentry_days = PositiveSmallInt(default=0)    # 0 = once per cycle
    activated_at; activated_by; activated_as_staff = Bool; rev = PositiveInt
    created_at; updated_at

class FlowStep(Model):                            # S5
    flow = FK(Flow, CASCADE, related_name="steps")
    key = Char(8)                                 # stable id "s_ab12cd"
    kind = Char(9, choices=sms|email|wait|condition|tag|goal|exit)
    config = JSON(default=dict)   # wait {"hours": 2} | {"days": 3} (1 hour to 365 days) | condition {"test": "clicked_or_lead"}
                                  # | tag {"add": 4} | {"remove": 4}
    next_key, yes_key, no_key = Char(8, blank)
    order = PositiveSmallInt
    constraints: Unique(flow, key)

class FlowRun(Model):                             # S5
    flow = FK(Flow, CASCADE, related_name="runs"); contact = FK(Contact, CASCADE)
    cycle = Char(20, blank)                       # "2026-10-01" for dated triggers, "" otherwise
    state = Char(8, choices=waiting|done|exited|failed)
    step_key = Char(8); wake_at = DateTime(null)
    started_at; finished_at; exit_reason = Char(20, blank)
    history = JSON(default=list)                  # [[step_key, iso, outcome]], max 50
    is_test = Bool(default=False)                 # S6 test mode
    constraints: Unique(flow, contact, cycle) where is_test = false   # re-entry (reentry_days > 0) uses cycle "r<n>"
    indexes: partial (wake_at) where state = "waiting"

class UtskickApiKey(Model):                       # S5, same pattern as sms.SmsApiKey
    account = FK(FlamingoAccount, CASCADE); name = Char(60)
    prefix = Char(12); key_hash = Char(64, unique)   # raw "adxut_" + 32 random, only SHA-256 stored
    created_at; created_by; last_used_at; revoked_at; revoked_by
```

Hidden flow-step utskick: one per send step, never listed (`flow_step__isnull=True` on every list,
count and header query), never moved by `finish()` or `transition`: their status is `sending` while
the flow is active and `paused` otherwise (set by the flow state view). Their recipients carry
`flow_run`, so a second cycle or a re-entry gets a new row under the second constraint. Retention
for flow recipients is by `created_at` (13 months).

### B.6 Stage S6

No new tables. `FlowRun.is_test` (test mode), `TrackedLink.position` (click map),
`Utskick.converted_to_flow = FK(Flow, SET_NULL, null)` (null, B.0).

### B.7 Changes to existing tables

| Table | Change | Stage |
|---|---|---|
| `flamingo.Lead` | `contact = FK("utskick.Contact", SET_NULL, null, related_name="leads")` | S1 |
| `flamingo.Lead` | `utskick = FK("utskick.Utskick", SET_NULL, null)`, `utskick_recipient = FK("utskick.Recipient", SET_NULL, null)`, `attribution = JSON(db_default={})` (snapshot: click id, utskick id and name, channel, link label, clicked_at, contact_matched, late), `activity_at = DateTime(db_default=Now())` + index `(account, -activity_at, -id)` and a `RunSQL("UPDATE flamingo_lead SET activity_at = created_at")` in the same migration, source choice `("reply", "Svar på utskick")` | S2 |
| `sms.SmsMessage` | `source = Char(10, choices=api|utskick|flow|reply|system|test, db_default="api")`, `sender` max_length 11 -> 16, index `(account, source, created_at)`, partial index `(to, created_at) WHERE sender = '+46766860046'` (reply routing; the literal comes from the setting at migration time) | S2 |
| `sms.MonthlyStatement` | `by_source = JSON(db_default={})` ({source: {sms, parts, total}}), informational only | S2 |

Migration order: `utskick.0001` depends on `flamingo.0015` and `sms.0005`; `flamingo.0016`
(Lead.contact) depends on `utskick.0001`; `utskick.0002` (S2) then `flamingo.0017` and `sms.0006`.
No cycle.

---
## C. Changes to existing code

### C.1 apps/sms (S2)

- `service.send_for_account(account, data, *, source, api_key=None, allow_reply_number=False,
  headroom=0, global_headroom=0, part_cost_hint=None)` holds today's body of `send()`; it always
  uses the real time (no `now` parameter from the tick: `created_at` and the rate window must be
  real). `send(api_key, data, now)` becomes `return send_for_account(api_key.account, data,
  api_key=api_key, source="api")` (keeping `now` for its existing tests only).
- Sender check: `sender == settings.UTSKICK_REPLY_NUMBER and allow_reply_number` is allowed without
  `SENDER_RE`; otherwise `sender in account.senders` as today. `validate_sender` and the card form
  stay alphanumeric-only, so an API key can never send from the shared number.
- `_record(...)` takes `source` and truncates `sender[:16]`.
- Flamingo's own references (`u<utskick>:<recipient>`, `t<msg>`, `x<inbound>`, `b<code>`,
  `p<probe>`) are stored with a `~` prefix through `send_for_account(..., internal_reference=)`
  (`sms_wrapper.internal_reference`). `REFERENCE_RE` never accepts `~` from the API, so an API
  key can neither take over a recipient's send nor get a 409 for a reference Flamingo used
  (S2 security and sending reviews).
- `ratelimit.check_account_minute(account, now, *, headroom=0, global_headroom=0)` compares against
  `per_minute - headroom` and `global - global_headroom`. The API passes 0. Utskick passes `headroom
  = max(0, SMS_RATE_PER_MINUTE - UTSKICK_SMS_ACCOUNT_PER_MINUTE)` and `global_headroom = max(0,
  SMS_GLOBAL_PER_MINUTE - UTSKICK_SMS_GLOBAL_PER_MINUTE)`; the utskick global budget also subtracts
  Flamingo `SmsLog` rows from the last 60 s (D.4).
- `estimate_cost` gets `part_cost_hint`: when `recent_part_cost` is empty the tick passes the
  per-country part cost it got from the first dryrun of the batch, so a 2 000-recipient send does
  not double its 46elks calls.
- **46elks 429** (`elks.py`): becomes `ElksError(status=429, ambiguous=False, throttled=True)`.
  `_deliver` then, inside one transaction, turns the RESERVED row into `REJECTED` with
  `error_code="rate_limited"`, price 0 (a stopped status releases the reference and costs nothing;
  the row stays, per the sms-api "kept forever" decision) and returns `fail("rate_limited",
  retry_after=60)` without an alert. The API path gets the same change (documented in
  `apps/sms/README.md`): an API caller now gets 429 instead of a failed message. Existing 4xx tests
  stay; a new test proves that a recipient requeued after a 429 really reaches FakeElks.
- `apply_delivery_report` and `resolve_check` call `hooks.status_changed(message)`; `apps/sms/hooks.py`
  holds a module-level list of callbacks that `UtskickConfig.ready()` appends
  `sending.sms.sync_from_message` to. apps/sms imports nothing from utskick for this.
- `api.message_detail` (`api.py:151-158`) and every future list endpoint filter `source="api"`: the
  integrator holding an API key must never read subscribers' numbers, merged names, personal codes
  or reply texts. Test: an utskick row returns 404 through the API.
- New `GET /api/sms/v1/suppressions/?to=+46...` (`sms_api:suppressions`) returns `{"to": "+46...",
  "suppressed": true|false}` from the account's utskick suppression list (lazy import of
  `apps.utskick.suppression.is_suppressed`), so the customer's own marketing over the API can honour
  a STOPP (under MFL a STOPP to Exempelrör covers all of Exempelrör's marketing). Documented on
  `/smsz/` and in the sms terms; 404 when utskick is not enabled.
- `pricing.build_statement` fills `by_source`; totals, lines per country and the CSV are unchanged
  (D5). `usage()` adds `by_source` counts.
- Portal: `portal_views._message_list` gets a source filter (Alla, API, Utskick, Svar);
  `templates/sms/portal/dashboard.html` shows a source label ("Utskick: Höstservice värmepump")
  instead of the raw reference for `source != "api"`. `api.message_json` adds `"source"`. `/smsz/`
  (`ai_docs.py`) says the cap is shared with Flamingo utskick.
- `alerts.cap_reached` is reused unchanged (once per account per month).
- README: document `source`, the reply number, the shared cap, the 429 change, the hook and the
  suppression endpoint.

### C.2 apps/flamingo

- `app_views/__init__.py`: `app_context` sets `"app_nav": utskick.nav.nav_for(account)`, which
  inserts `("contacts", "flamingo:app_contacts", "Kontakter")` (S1) and `("utskick",
  "flamingo:app_utskick_list", "Utskick")` (S2) after `campaigns` when
  `settings_for(account).is_enabled`. `APP_NAV` stays the default tuple. Every utskick child route
  sets `app_active` to `contacts` or `utskick` (I.1a).
- `urls.py`: `path("app/", include("apps.utskick.app_urls"))` placed **before**
  `path("<slug:slug>/", ...)`. The included module has no `app_name`, so names become
  `flamingo:app_contacts` etc.
- `middleware.FlamingoGateMiddleware` (line 45): return `self.get_response(request)` at once when
  `getattr(request, "is_link_host", False)`, so nothing under `/flamingo/` (and no
  `config.urls_public` fallback) answers on k./klick. Test with `HTTP_HOST="k.adx.se"`.
- `models.Lead`: fields in B.7. `can_send_to_google` returns `bool(self.gclid) and not
  self.utskick_id` (D11 belt and braces). `display_name` falls back to "Svar på utskick" for
  `source="reply"`.
- `leads.py`: `UT_KEY = "ut"`, `ut_from(*sources)` validating `[A-Za-z0-9]{1,12}\.[A-Za-z0-9]{10}`;
  `ut` is **not** in `TRACKING_KEYS` (it must never land in `Lead.utm`). `create_lead(...,
  click=None)` and `create_call_click_lead(..., click=None)` call `utskick.attribution.attach(lead,
  click)`.
- `limits.create_form_lead` / `create_call_click_lead`: only when `attribution.resolve(ut,
  campaign)` returns a click **of the same account** (E.4): `UTSKICK_LEADS_PER_CAMPAIGN = 200` per
  hour instead of `LEADS_PER_CAMPAIGN = 30`, at most 3 leads per click per hour, and the click id in
  the call-click dedupe key.
- `public_views.py`:
  - `landing()` GET with a resolved `ut` (not agency, not preview, not demo) calls
    `attribution.record_lp_visit(click, campaign)`; passes `ut` to the template.
  - `LeadForm` gets `consent_sms` and `consent_email` BooleanFields when
    `capture.lp_consent_channels(account, spec)` returns channels: `access.can_collect(account)`
    (enabled, DPA current), `lp_consent` on, a privacy notice available (H.5); sms only if the form
    has a phone field, email only if it has an email field and `Switchboard.doi_ready_at` is set.
    Never initial True. Field errors: "Fyll i din e-post för att få erbjudanden via e-post." and
    "Det här numret kan inte få sms. Skriv ett mobilnummer, eller kryssa ur rutan för sms." (a
    landline), shown at the address field, with the failing box marked as an error too; no
    consent is stored for the failing channel.
  - Under the checkboxes: "Så hanterar Exempelrör dina uppgifter" linking the privacy notice.
  - After `limits.create_form_lead` succeeds: `capture.from_lead_form(lead, cleaned, texts,
    page_path, ip_hash, click)` (gated, E.4 and H.5). The thanks page adds "Vi har skickat ett mejl
    till dig. Klicka på länken i mejlet för att börja få e-post." when email consent was ticked.
  - `_live_campaign(slug, click=None)`: with a same-account click the call click counts when the
    account is enabled, not demo and the customer active, even if the Google campaign is paused.
  - New view `visit_beacon` (`lp/<slug>/besok/`, POST, `csrf_exempt`, 204, `Cache-Control:
    no-store`, `X-Robots-Tag`): body `ut`, `s` (seconds).
- `templates/flamingo/lp/ren/blocks/form.html`: hidden `ut` field; the two checkboxes with the exact
  text, below the fields, unchecked, in `.rn-check` (no `style=`, guard in `test_inbox.py`).
- `static/js/flamingo-lp.js`: add `"ut"` to `TRACKING` (sent with the call click); after reading
  `ut` into the form and the call link, remove it from the address bar with `history.replaceState`
  (a copied page link must not carry the recipient's token); engagement beacon only when `ut` was
  present and `data-fl-visit` is set: visible time from `document.visibilityState` and
  `performance.now()`, sent with `navigator.sendBeacon` on `visibilitychange` (hidden) and
  `pagehide`, at most 3 beacons per page load. No cookie, no storage (the guard in
  `test_google_measure.py:322` keeps passing).
- `app_views/inbox.py` (S2):
  - `channel(lead)`: first branch `if lead.source == "reply"` -> "Sms-svar" / "E-postsvar" /
    "STOPP"; then `if lead.utskick_id` -> `"Utskick: " + lead.attribution.get("name", "")` (before
    the "Länk från {source}" branch, which would otherwise say "Länk från flamingo").
  - `lead_list`: order by `("-activity_at", "-id")`. When utskick is enabled the existing status
    chips are replaced by **one** chip row with counts: "Alla 9 · Förfrågningar 4 · Sms-svar 4 ·
    E-postsvar 1 · Avregistreringar" (`?typ=`), and the status filter becomes a `<select>` (with a
    Visa button, no JS needed) placed under the chips; under 560 px the chip row wraps. Without
    utskick the inbox is unchanged.
  - `_card.html` and `detail.html`: for reply leads `contacted` reads "Klar" and the card time is
    `activity_at`; the meta line is the first 200 characters of the latest reply.
  - `lead_detail`: when `lead.reply_thread` exists, include `flamingo/app/utskick/_thread.html`
    (messages, reply form, "Avregistrera från sms" button, "Kontaktkort" and "Klar" buttons) and
    `select_related("reply_thread")`; new POST view `lead_reply` (`app/inkorg/<pk>/svara/`, name
    `app_lead_reply`).
  - Fix the stale docstring (line 16: staff in view-as is no longer read-only).
- `sms.py`: `owner_reply_text(account, n_replies, n_leads, url)` -> "3 nya svar på utskick. Se
  Inkorgen: <url>" or "2 nya svar och 1 förfrågan via utskick. Se Inkorgen: <url>"; single reply:
  "Svar på utskick från Johan Berg, 073-555 12 34: <svar>. Se mer: <url>" (name and text through
  `visitor_text`). Public `notify_owner_text(account, text)` sends it as the existing owner-notice
  kind (counts toward `SMS_DAILY_MAX` like every `SmsLog` row). Batching lives in utskick (G.1).
  `notify_new_lead` for a lead with `utskick_id` skips the per-lead owner sms (it is batched with the
  replies); the visitor autoreply is unchanged.
- `app_views/overview.py` `numbers_for`: `leads` excludes `source="reply"`; new `google_leads` /
  `google_deals` exclude `utskick__isnull=False` and `source="reply"`; `kr_per_lead` and
  `kr_per_deal` use them (D11); new `utskick_leads`, `utskick_deals` for a line "Varav via utskick:
  31 förfrågningar, 4 affärer".
- `rules.three_things`: an item per paused utskick (I.5) and per pending AllowedHost, before the
  existing items, still capped at `MAX_THINGS`.
- `management/commands/flamingo_demo.py`: `_reset` first calls `utskick.demo.reset(account)` (which
  also deletes the account's `Suppression` and `ConsentLog` rows); the content builders call
  `utskick.demo.seed(account, staff, now)`: `UtskickSettings` enabled, contacts with PTS fictional
  numbers `+4670174xxxx`, `.example` emails, lists, one sent and simulated utskick with clicks and
  leads, one scheduled, one draft, two reply threads, one STOPP; flows in S5. `dpa_ok` is true for
  the demo. Idempotent under `test_demo.counts()`.
- `templates/flamingo/_customer_panel.html`: `{% load utskick_tags %}{% utskick_card customer %}`
  inside the Flamingo panel (decoupled like `sms_card`).
- `templates/flamingo/app/onboarding/settings.html` line 66: "Kontakter hos" -> "Inloggningar hos".
- `media.py` (S3): `delete_asset` also asks `utskick.email.images.uses(account_id)` (imported inside
  the function) and raises `MediaInUse` when the asset is in a draft, scheduled, sending or paused
  utskick. Assets used only in sent mails can be deleted; their `EmailImage` files stay (`asset`
  SET_NULL) so sent mails keep their pictures until retention removes them.
- `flamingo/README.md`: a pointer to this file and the Inkorg changes.

### C.3 Cross-cutting

- `config/settings/base.py`: `INSTALLED_APPS += ["apps.utskick"]` after `apps.sms`; middleware
  `apps.utskick.links.LinkHostMiddleware` directly after `SecurityMiddleware` (S2); the settings in
  C.4; `TEST_RUNNER = "config.test_runner.NoNetworkRunner"` (S1).
- `config/test_runner.py` (S1): a `DiscoverRunner` subclass that patches `socket.socket.connect` to
  refuse anything but AF_UNIX and loopback addresses, and overrides `ADX_AWS_PROFILE="__test__"`,
  `SMS_SEND_LIVE=False`, `UTSKICK_EMAIL_LIVE=False` for the whole suite (tests read the developer's
  `.env`, which holds real 46elks credentials). FakeElks and FakeSes are opt-in on top. Any existing
  test that fails under it is fixed in the same commit.
- `config/settings/production.py`: raise `ImproperlyConfigured` when `UTSKICK_HASH_KEY` or
  `UTSKICK_LINK_KEY` is unset (test in `test_s1_guards.py`).
- `config/urls.py`: before the site catch-alls add `path("utskick/",
  include("apps.utskick.public_urls"))` (S1) and `path("api/utskick/",
  include("apps.utskick.webhook_urls"))` (S2).
- `config/urls_links.py` (S2): the whole URL table for the link hosts (E.1). Nothing else of the
  site answers there.
- `apps/manage/urls.py`: `path("", include("apps.utskick.manage_urls"))`, names `manage:utskick_*`
  (like `apps.sms.manage_urls`).
- `apps/manage/forms.py` `RESERVED_SLUGS[DESIGN_ADX]` += `"utskick"`, `"bitradesavtal"`.
- `apps/core/views.py robots_txt`: `Disallow: /utskick/` and `Disallow: /api/`. Link hosts serve
  their own `robots.txt` (`Disallow: /`).
- `apps/analytics/middleware.py`: `_SKIP_PREFIXES += ("/utskick/",)`; early return when
  `getattr(request, "is_link_host", False)`.
- `apps/common/sentry.py`:
  - `_drop_locals`: when **any** frame of the event has a module starting with `apps.utskick.`,
    `apps.sms.`, `apps.flamingo.public_views` or `apps.flamingo.app_views.inbox`, empty `vars` in
    **every** frame of the event (the frames in `django.db.backends.utils` carry `params`, and
    `django.forms` frames carry cleaned data).
  - `SECRET_NAMES += ["address", "phone", "email", "first_name", "last_name", "merge", "text_shown",
    "evidence", "message", "raw", "params"]`.
  - `_PATTERNS +=` E.164 `\+\d{8,15}`, Swedish mobile `\b07\d[\d -]{6,10}\d\b`, email addresses
    `[\w.+-]+@[\w-]+(\.[\w-]+)+`, query secrets `(?:^|[?&\s"'])(?:ut|adx|q)=[^&\s"']+` (Sentry
    stores `query_string` without the leading "?"), `(/utskick/(?:bekrafta|val)/)[A-Za-z0-9._-]{16,}`,
    `(/api/utskick/46elks/inkommande/)[A-Za-z0-9_-]{24,}`, `(https?://(?:k|klick)\.adx\.se/)[^\s"'<>]+`,
    `\bs\+[a-z0-9.]+@svar\.utskick\.adx\.se\b`, `adxut_[A-Za-z0-9_\-]{8,}`.
  - `_UNTRACED_PREFIXES += ("/api/utskick/", "/utskick/")`; `traces_sampler` also returns 0.0 for
    `/flamingo/app/kontakter/`, `/flamingo/app/utskick/`, paths ending in `/besok/` (the LP beacon)
    and when the ASGI `Host` header is a link host.
  - Leak tests in `apps/common/test_sentry.py` for each pattern, for a `params` frame under an
    utskick frame, and for a contact search `?q=`.
- `apps/assistant/asgi_app.build_application`: drop `UTSKICK_LINK_HOSTS` from the MCP
  `allowed_hosts`, and return 404 for MCP and OAuth paths when the `Host` header is a link host.
  nginx also refuses them (C.5).
- `apps/manage/context_processors.py` static_version list: add every new CSS and JS file (and the
  missing `sms.css`, `sms.js`).
- `apps/assistant/tests.py` `test_no_forbidden_tools_exist`: add `"skicka_utskick"`,
  `"utskick_skicka"`, `"skicka_sms"`.
- `apps/monitor/management/commands/monitor_check.py` (every 5 min): if `Switchboard.last_tick_at`
  is older than 5 minutes while `utskick.sending.tick.work_exists()` is true, alert the agency at
  most once per hour (lazy import, no-op when utskick is not installed).
- `pyproject.toml`: `openpyxl` and `defusedxml` (S1; openpyxl uses defusedxml when it is installed),
  `boto3` as a direct dependency (S1, DOI over the SES API; today transitive via
  `anthropic[bedrock]`), `segno` (S4, QR as SVG and PNG, pure Python). `uv lock` each time.

### C.4 Settings and `.env.example`

New keys, each with a Swedish comment in `base.py` and a line in `.env.example` (empty means the
documented default):

| Key | Default | Stage | Notes |
|---|---|---|---|
| `UTSKICK_HASH_KEY` | dev: derived from SECRET_KEY | S1 | Required in production (`ImproperlyConfigured`). Stable forever; backed up with the DB and in the password manager. Fingerprinted (H.7). |
| `UTSKICK_LINK_KEY` | dev: derived from SECRET_KEY | S1 | Same rules. Signs unsubscribe, preference, DOI, Reply-To, `ut`, email-link and form-nonce tokens (E.2). |
| `UTSKICK_DOI_FROM` | `bekrafta@utskick.adx.se` | S1 | DOI sender; display name = `display_name`. |
| `UTSKICK_TICK_SECONDS` | 50 | S1 | Time budget per tick. |
| `UTSKICK_TICK_MAX_MB` | 700 | S1 | `RLIMIT_AS` for the tick (D.1); checked against the measured peak in S1 QA. |
| `UTSKICK_EMAIL_LIVE` | false | S1 | Off in production: no mail at all (DOI and email channel refused, D.8). Off with DEBUG: `.eml` files to `PRIVATE_MEDIA_ROOT/utskick-mail/`. |
| `UTSKICK_AWS_ROLE_ARN` | empty = no AWS (dev uses `ADX_AWS_PROFILE`) | S1 | `arn:aws:iam::500841883756:role/adx-utskick` (H.8). |
| `UTSKICK_AWS_EXTERNAL_ID` | empty | S1 | 32 random chars. |
| `UTSKICK_SES_REGION` | `eu-west-1` | S1 | Sending and receiving (D8, H.8). |
| `UTSKICK_ADX_MAIL_DOMAIN` | `utskick.adx.se` | S1 | |
| `UTSKICK_LINK_HOSTS` | `k.adx.se,klick.adx.se` | S2 | Local: `k.localhost,klick.localhost`. |
| `UTSKICK_SMS_LINK_BASE` | `https://k.adx.se` | S2 | Shown without scheme in sms. |
| `UTSKICK_EMAIL_LINK_BASE` | `https://klick.adx.se` | S2 | |
| `UTSKICK_REPLY_NUMBER` | `+46766860046` | S2 | |
| `UTSKICK_ELKS_INBOUND_TOKEN` | empty = inbound off | S2 | 32+ random chars, part of the `sms_url`. In production the endpoint also refuses to run while `SMS_DLR_ALLOWED_IPS` is empty. |
| `UTSKICK_SMS_ACCOUNT_PER_MINUTE` | 45 | S2 | Leaves `SMS_RATE_PER_MINUTE - 45` (15) for the customer's API. |
| `UTSKICK_SMS_GLOBAL_PER_MINUTE` | 60 | S2 | Leaves `SMS_GLOBAL_PER_MINUTE - 60` (20); `SmsLog` rows are subtracted too. |
| `UTSKICK_SES_CONFIGURATION_SET` | `adx-utskick` | S3 | |
| `UTSKICK_ADX_MONTHLY_MAIL_CAP` | 2000 | S3 | Per customer per Stockholm month. |
| `UTSKICK_EMAIL_PER_SECOND` | 10 | S3 | Capped at 80% of SES MaxSendRate when known. |
| `UTSKICK_REPLY_DOMAIN` | `svar.utskick.adx.se` | S3 | Reply-To `s+<token>@...`. |
| `UTSKICK_SES_INBOUND_BUCKET` | empty = inbound mail off | S3 | Pinned: the handler refuses any other bucket. |
| `UTSKICK_SQS_EVENTS_URL` | empty = events off | S3 | eu-west-1 queue fed by the SES events topic. |
| `UTSKICK_SQS_INBOUND_URL` | empty = inbound off | S3 | Queue fed by the receipt-rule topic. |

The DPA version is no setting: it is the current `DpaVersion` row (B.1).

`ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` in production `.env`: append `k.adx.se,klick.adx.se`
(comma-separated, no spaces) at the **end**: `lankrapport` crawls the first entry and `./deploy`
fails on its result (`apps/website/management/commands/lankrapport.py:44-48`). `reload` does not
reread `.env`; run `systemctl restart adx`.

### C.5 Server

- **Link hosts (S2)**: `server/sites.d/adx.conf`: `LINK_DOMAINS=("k.adx.se" "klick.adx.se")`,
  `LINK_CERT_NAME="adx-links"`. Do **not** add them to `DOMAINS` (every non-primary domain 301s to
  adx.se).
- `server/templates/nginx.conf.template`: a `${LINK_BLOCK}` rendered by `lib.sh` when `LINK_DOMAINS`
  is set: port 80 `return 301 https://$host$request_uri;` and a 443 server with `server_name
  ${LINK_DOMAINS}`, certificate `/etc/letsencrypt/live/adx-links/`, `access_log off;` (sms codes and
  email tokens must not sit next to client IPs), `location ~
  ^/(mcp|authorize|token|register|revoke|\.well-known/oauth) { return 404; }`, `location /static/ {
  alias ${STATIC_DIR}/; }` (the preference page CSS and botcheck JS), `location / { proxy_pass
  http://unix:${SOCKET}; proxy_set_header Host $host; ... }` (same headers as the primary block), no
  `/media/`.
- Primary block: `location /api/utskick/ { access_log off; proxy_pass ...; }` (path secrets).
- `lib.sh`: build `LINK_BLOCK`, add `$LINK_DOMAINS $LINK_BLOCK` to the envsubst whitelist (line
  173), keep the port-80 primary block limited to `DOMAINS`.
- `certs.sh`: a **separate** lineage `certbot certonly --nginx --cert-name adx-links -d k.adx.se -d
  klick.adx.se`, so a failed HTTP-01 on a link host never blocks the renewal of adx.se's own
  certificate.
- `deploy.sh` never renders nginx. A small `server/nginx-only.sh adx` (runs `load_site adx;
  install_nginx`) is executed by the lead as `ubuntu`, not `provision-site.sh` (which runs git and
  uv directly and would leave files owned by the wrong user).
- `deploy.sh` takes the tick lock: `flock -w 90 /home/djangouser/sites/adx/run/utskick.lock` around
  pull, `uv sync`, migrate, reload and the health check (a rollback runs under the lock too), so
  the tick never runs from a half-updated checkout (S1).
- Follow-up for the S2 nginx work: `access_log off` also for `/utskick/val/` and
  `/utskick/bekrafta/` in the primary block (the preference token never expires; S1 keeps it out
  of the application logs only).
- `server/crontab.d/adx-utskick` (S1, committed): the cron lines of D.1 and an install line in
  `server/README.md` (`crontab -u djangouser` merge). `server/logrotate.d/adx-utskick`: weekly, 8
  rotations, `backups/utskick*.log`.
- HSTS `includeSubDomains` + preload is on (`production.py:24-26`): the hosts must have valid TLS
  before the first link is sent (`Switchboard.links_ready_at`).
- IMDSv2 (S1 checklist): `aws ec2 modify-instance-metadata-options --instance-id
  i-00366e8f91f9ebc65 --http-tokens required --http-put-response-hop-limit 1`.

---

## D. Sending engine

### D.1 Commands and cron

- `utskick_tick` (every minute). Options: `--budget N`, `--only <utskick_pk>` (manual),
  `--verbose`. First thing: `resource.setrlimit(RLIMIT_AS, UTSKICK_TICK_MAX_MB)`. Writes one summary
  line to `backups/utskick.log` only when it did work (counts only, never addresses).
- `utskick_daily` (02:45, before `sms_close_month` at 03:10 on the 1st): retention (E.7), import
  file cleanup, inactive-contact flags, segment recount (S4), SES `GetAccount` and DNS rechecks
  (S3), DLQ check (D.7), date-trigger bookkeeping (S5), health rollups, disk check, month-end
  RESERVED check (D.5).
- Cron lines (`server/crontab.d/adx-utskick`):

  ```
  * * * * *  /usr/bin/flock -n /home/djangouser/sites/adx/run/utskick.lock /usr/bin/choom -n 800 -- /bin/sh -c 'cd /home/djangouser/sites/adx/app && MALLOC_ARENA_MAX=2 DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py utskick_tick >> /home/djangouser/sites/adx/backups/utskick.log 2>&1'
  45 2 * * *  /usr/bin/flock -w 300 /home/djangouser/sites/adx/run/utskick-daily.lock /usr/bin/choom -n 800 -- /bin/sh -c 'cd /home/djangouser/sites/adx/app && MALLOC_ARENA_MAX=2 DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py utskick_daily >> /home/djangouser/sites/adx/backups/utskick-daily.log 2>&1'
  ```

  No `env $(grep ...)` key list: `base.py`'s `read_env` loads `../.env` itself (l.21-24), so the tick
  sees every key, including the agency-alert mail settings, and a new key can never be forgotten in
  the cron line. `DJANGO_SETTINGS_MODULE` must be explicit (`manage.py` defaults to development).

  `flock -n` means a second tick never starts a Python process while one runs; `choom -n 800`
  makes the tick the OOM killer's first choice (the box has no swap; Postgres serves 8 sites).
  `deploy.sh` holds the same lock (C.5). The Postgres advisory lock stays as a second guard.

### D.2 Tick algorithm

```
handle():
  setrlimit; deadline = monotonic() + UTSKICK_TICK_SECONDS
  if not pg_try_advisory_lock(UTSKICK_TICK_LOCK): return       # second guard behind flock
  keys.check_fingerprints() or (alert; return)                  # H.7
  if not work_exists(now): heartbeat(); return                  # one cheap EXISTS per queue, no boto3 import
  phases (each returns early when monotonic() > deadline; budgets are caps, not reservations):
    1 recover_stale(now)                          # D.5
    2 inbound(now, 8 s)                           # sms rows left "received", SQS events + inbound mail (S3),
                                                  # 46elks reconcile every 10 min (G.1)
    3 confirmations(now, 5 s)                     # DOI mails (S1), confirm sms and STOPP/START answers (S2), owner notices
    4 start_due(now) and freeze chunks (10 s)     # D.3
    5 send_sms(now, deadline - 12 s)              # D.4 (S2)
    6 send_email(now, deadline - 12 s)            # D.6 (S3)
    7 flows(now, 5 s)                             # S5
    8 import_chunk(now, 15 s)                     # S1, only with time left
    9 finish(now)                                 # sending utskick (flow_step null) with nothing queued -> sent
  heartbeat(summary)                              # Switchboard.last_tick_at
```

Lock keys, listed in a comment next to the existing two (`limits._LOCK_SPACE = 0x464C << 32`,
`media._LOCK_MEDIA = 0x464D << 32`): `UTSKICK_TICK_LOCK = 0x5554 << 32`, ADX mail cap lock
`(0x5555 << 32) + account_pk`, contact-limit lock `(0x5556 << 32) + account_pk`.

`work_exists(now)` and every `accounts_with_due_*` query filter on sendable accounts:
`utskick__is_enabled=True`, `is_enabled=True` (FlamingoAccount), `customer__is_active=True`,
`utskick__sending_blocked=False`. Inbound, STOPP/START and suppression handling do **not** filter
on these (an opt-out must work for a disabled account). `select_for_update(skip_locked=True,
of=("self",))` on every claim keeps the manual `--only` run and the daily command safe (precedent
`flamingo/manage_review.py:1271`). Imports of boto3 and the email renderer are lazy.

### D.3 Freeze (`sending/freeze.py`)

0. `start_due`: utskick with `status=scheduled` and `scheduled_at <= now`. If `now - scheduled_at
   > 3 h` or the Stockholm date changed since `scheduled_at`, move to `paused` reason `late` (the
   customer resumes after reviewing, which re-confirms). If a link host is pending or refused, or
   the information rules (H.5) stop the text (`state.content_problems`), move to `paused` reason
   `content` (re-confirm). If free disk space is below 8% (`shutil.disk_usage`), refuse to
   freeze, keep `scheduled`, alert the agency hourly.
1. `audience.contacts(utskick)`: always starts from `Contact.objects.filter(account=utskick.account)`
   and joins lists, tags and segments with `list__account=` / `tag__account=` filters (a tampered
   id in `audience` yields nothing); union of lists, tags, segments (S4) and explicit contacts,
   minus excludes (`recent_days` = had a recipient on any channel in the last N days), ordered by
   pk, keyset from `freeze_cursor`, chunks of 2 000.
2. Eligibility is set-based per chunk: prefetch consents, one `value_hash IN (...)` suppression
   query per channel, one weekly-cap aggregate. Channel per contact by `channel_mode` and
   `consent.eligible(contact, channel, purpose)`. `sms_then_email`: sms if eligible, else email.
   `both`: every eligible channel.
3. One `transaction.atomic()` per chunk covers: `bulk_create(ignore_conflicts=True)` of `Recipient`
   rows (`queued` or `skipped` with `skip_reason`, frozen `merge`, `basis`, `tracking_ok`); a
   re-select of their pks; `LinkCode` inserts **without** ignore_conflicts inside a savepoint (a
   code collision redraws that batch, at most 3 times, then the chunk fails and is retried next
   tick); the `freeze_cursor` update. After the chunk, assert that every sms recipient that needs
   codes has them (one per `{länk:x}` placeholder; a `person` code when the body needs `/s/`).
4. When the cursor reaches the end: `frozen_counts`, then pre-checks:
   - links and information rules again (`state.content_problems`, with the frozen field values for
     information) -> `paused` reason `content` (needs a new confirmation);
   - sms cost: `sum(parts) x part cost + markup_for` vs `pricing.usage(sms_account)["remaining"]` ->
     `paused_cap` reason `sms_cost_cap` before the first send if it does not fit;
   - ADX-domain mail cap and the daily email cap (S3, D.9) -> `paused_cap` reason `adx_mail_cap` /
     `email_daily_cap`;
   - audience grew more than 20% over `confirm_summary` -> `paused` reason `audience_grew` (needs a
     new confirmation);
   - email channel not live in production (`UTSKICK_EMAIL_LIVE` false or `email_enabled` false) ->
     `paused` reason `email_disabled`;
   - account health block (S3) -> `paused_health`; `sending_blocked` -> `paused` reason `blocked`.
   Otherwise `sending`.

### D.4 Sms loop (`sending/sms.py`)

```
if Switchboard.sms_enabled is False or sms_paused_until > now: return
while time_left() > SAFETY:
  accounts = accounts_with_due_sms(now)           # sendable accounts with queued sms recipients, not_before passed
  progressed = False
  for account in round_robin(accounts):           # oldest utskick first inside an account
    if account.is_demo: simulate(account); continue
    if not timing.sms_window_open(settings, now):
        UPDATE its queued sms recipients SET not_before = next_window_start (bulk); continue
    budget = min(acct_limit - sms_rows_last_60s(account),
                 global_limit - sms_rows_last_60s(all) - smslog_rows_last_60s(all))   # recomputed from the DB
    if budget <= 0: continue
    claim (atomic, select_for_update skip_locked of self):
        Recipient.filter(utskick__account=account, utskick__status="sending", channel="sms",
                         status="queued", not_before__lte=now | null).order_by("id")[:min(budget, 10)]
        -> status="sending", claimed_at=now, attempts += 1
    for r in claimed:
      check = checks.send_time_checks(r)          # fresh reads, two classes below
      if check.defer: requeue(r, not_before=check.not_before, attempts-1); continue
      if check.skip:  mark skipped(check.reason); continue
      sender = checks.sender_for(r)               # reply number, or the name sender on reply collision
      body = composer.render_sms(utskick, r, sender)   # merge, codes, opt-out line for that sender
      out = sending.sms_wrapper.send(account, r, body, sender)   # assert_not_demo, then send_for_account(
              # {"to": r.address, "message": body, "from": sender, "reference": f"u{u.pk}:{r.pk}"},
              # source="flow" if u.flow_step_id else "utskick", allow_reply_number=True, headroom=...)
      map(out) per the table below
      progressed = True
      if time_left() <= SAFETY: requeue the rest; return
  if not progressed: break
```

Send-time checks (`sending/checks.py`), run per recipient with fresh reads, in two classes:
- **Deferring** (row back to `queued`, `attempts - 1`, `not_before` set): utskick not `sending`;
  window closed (`not_before` = next window start); `Switchboard.sms_enabled` off or
  `sms_paused_until` in the future; account no longer sendable (disabled, Flamingo off, customer
  inactive, `sending_blocked`); rate-limited (`not_before = now + retry_after`).
- **Per person** (`skipped` with the reason): suppression hash present; contact deleted; contact
  phone no longer equals `r.address` (`address_changed`); for reklam `consent.eligible` false
  (`no_consent`, `declined`, `pending_doi`); weekly cap reached (`weekly_cap`); reply collision with
  no name sender (`reply_collision`).

**Reply collision** (`checks.sender_for`, D4): if the chosen sender is the reply number and another
account sent a reply-number sms to the same E.164 in the last 14 days (partial index B.7), the
account's first approved name sender is used instead (the composer appends the `/s/` link instead
of "Svara STOPP"); without a name sender the recipient is skipped `reply_collision`. Granska warns
when the utskick asks for replies. Inbox replies and STOPP/START answers skip this check (the person
just wrote to us, and our answer correctly becomes the latest outbound).

Outcome mapping:

| Outcome | Recipient | Utskick and account |
|---|---|---|
| ok, duplicate | `sent`, `sms_message`, `parts`, `sent_at`, `sms_sender` | |
| unknown (RESERVED, needs_check) | `unknown`, `sms_message` set | counts toward the breaker |
| rate_limited | requeue, `not_before = now + retry_after` | next account |
| monthly_cap_reached | requeue with the rest | one statement pauses **all** the account's sending utskick and active flow steps: `paused_cap` `sms_cost_cap` |
| sms_not_enabled | requeue | `paused` `sms_disabled` |
| sender_not_allowed, message_too_long | `failed` (REJECTED row, cost 0) | `paused` `provider` after the first occurrence |
| invalid_number, country_not_allowed | `failed` with reason | |
| provider_error | `failed` | counts toward the breaker; 5 in a row for the utskick -> `paused` `provider` |

**Circuit breaker**: 3 ambiguous or `provider_error` outcomes ADX-wide within 2 minutes set
`Switchboard.sms_paused_until = now + 10 min` and send one agency alert; all sms sending (utskick,
flows, confirmations) defers until then. Inbox replies (synchronous, G.2) show "Sms kan inte
skickas just nu. Försök igen om en stund." while it is set.

Quiet hours (`timing.py`): window per account (default weekdays 09 to 20, weekends 10 to 18, hard
bounds 08 to 21); Swedish public holidays count as weekend days (fixed dates plus Easter-based ones,
computed in code). DST-safe: windows are computed in `STOCKHOLM` local time. Only utskick and flows
are windowed; inbox replies, confirmations and STOPP/START answers are not. The API is never
windowed.

Billing and cap: every sms goes through `send_for_account`, so the cap lock, statements and portal
behave as today (D5). After the first `monthly_cap_reached` the whole account is paused, so at most
one `BLOCKED_CAP` row per account per tick is written. STOPP/START answers and confirm sms check
`pricing.usage()["remaining"]` first and skip without calling the service when the cap is reached
(the STOPP itself is still applied; the thread shows "Bekräftelsen skickades inte: kostnadstaket är
nått.").

Delivery status: `hooks.status_changed` (C.1) runs `sync_from_message(msg)`: `Recipient` rows with
that `sms_message` move forward only, by rank (`sent|unknown -> delivered|failed`), and
`ThreadMessage.status` likewise.

Month end: reklam sms stop at 20.00, so no batch crosses midnight. A RESERVED row left by a crash
blocks `sms_close_month` for that account exactly as an API one would.

Demo (`simulate`): no provider call, no `SmsMessage`; recipients go straight to `delivered` with
`simulated=True`. The confirm step says "Demokontot skickar aldrig. Utskicket visas som skickat."
`assert_not_demo(account)` sits in the lowest layers (`sending/sms_wrapper.send`,
`email/transport.send`), and a test proves that every path to them refuses the demo.

### D.5 Idempotency and crash recovery

- Sms: the reference `u<utskick>:<recipient>` is unique per account while the message is not
  stopped. `recover_stale` takes recipients in `sending` whose `claimed_at` is more than 5 minutes
  old and adopts **only** the row matching the constraint predicate (`status NOT IN (rejected,
  blocked_cap) AND error_code != 'provider_error'`): a sent row -> `sent`, a RESERVED row ->
  `unknown` (reconciled by the existing `needs_check` flow, never resent). No such row: back to
  `queued`.
- Email: the claim is committed before the SES call. A stale `sending` email becomes `unknown` and
  is **never resent**; an SES `Send` or `Delivery` event carrying the recipient tag adopts it
  (`ses_message_id`, `sent`). After 24 h `unknown` becomes `failed` with error "Oklart om mejlet
  skickades".
- Webhooks and queues are idempotent by provider id (`InboundMessage`, `EventReceipt`).
- Status writes: the loops write `ses_message_id`/`sms_message` and `sent_at` unconditionally and
  `status='sent'` only `WHERE status IN ('sending', 'unknown')`; events and hooks move status
  forward only, by rank, so a Delivery event that lands before the loop's write is never undone.
- Month end: `utskick_daily` on the 1st (02:45, before `sms_close_month` at 03:10) lists
  utskick-source RESERVED/needs_check rows on `/manage/sms/#kontrollera` and alerts the agency.

### D.6 Email loop (`sending/email.py`, S3)

Same claim pattern with batches of 25. Pacing `min(UTSKICK_EMAIL_PER_SECOND, 0.8 x
Switchboard.ses_max_rate)`, or the setting alone while `ses_max_rate == 0` (populated by
`utskick_daily` and the S3 checklist). Two SES clients per process from `utskick.aws.session()`
(H.8) in `UTSKICK_SES_REGION`, keep-alive pool reused:
- `send_client`: `Config(retries={"max_attempts": 1, "mode": "standard"}, connect_timeout=5,
  read_timeout=10)`. `SendEmail` has no idempotency token, so the send call is never retried by
  botocore.
- `api_client`: standard retries, for identities, `GetAccount`, SQS and S3.

Per recipient: deferring and per-person checks as for sms (plus `email_state != bounced`, pending
DOI excluded, sender domain re-checked: `sender_domain.account_id == account.id and status ==
verified`, else the utskick pauses `provider`); for the ADX domain, the account's month count of
sent-like email recipients (plus test sends) against `UTSKICK_ADX_MONTHLY_MAIL_CAP`, under the
per-account advisory lock -> `paused_cap` `adx_mail_cap`; the daily and probe rules (D.9). Render
(`email.render`, F.4) and send:

```
send_client.send_email(
  FromEmailAddress=from_addr, Destination={"ToAddresses": [r.address]},
  Content={"Raw": {"Data": mime_bytes}},
  ConfigurationSetName=UTSKICK_SES_CONFIGURATION_SET,
  EmailTags=[{"Name": "a", "Value": str(account.pk)}, {"Name": "u", "Value": str(u.pk)},
             {"Name": "r", "Value": str(r.pk)}])
```

Raw MIME (`email/mime.py`, stdlib `email.message.EmailMessage` with `policy.SMTP`): `From`
(display name encoded: `display_name <slug@utskick.adx.se>` on the ADX domain, `from_name
<hej@exempelror.example>` on a verified own domain), `To`, `Reply-To`
(`s+r<account36>.<recipient36>x<sig>@svar.utskick.adx.se`, or `own_reply_to` when
`email_reply_mode=own` and confirmed), `Subject`, `Date`, `List-Unsubscribe:
<https://klick.adx.se/a/<token>>, <mailto:s+u<account36>.<recipient36>x<sig>@svar.utskick.adx.se?subject=avregistrera>`,
`List-Unsubscribe-Post: List-Unsubscribe=One-Click`, `multipart/alternative` (text/plain then
text/html, UTF-8, quoted-printable). No attachments, images by absolute URL. Routing uses the
Reply-To token, not `Message-ID`.

Error mapping:

| SES result | Recipient | Other |
|---|---|---|
| success | `ses_message_id`, `sent_at`, `sent` (conditional, D.5) | |
| Throttling, `TooManyRequestsException`, 429 | requeue | sleep 1 s, halve the rate for the rest of the tick |
| read timeout, connection reset after the request was sent, any 5xx | `unknown` (never resent) | adopted by the Send event (D.5) |
| `AccountSendingPausedException` | requeue | `Switchboard.email_enabled=False` + agency alert |
| `MailFromDomainNotVerified` | requeue | utskick `paused` `provider`, agency alert |
| `MessageRejected` and other 4xx validation errors | `failed` with the error | 5 in a row pause the utskick `provider` |

### D.7 SES events and inbound notifications via SQS (S3)

Topics `adx-utskick-events` (configuration set event destination: SEND, REJECT, BOUNCE, COMPLAINT,
DELIVERY, DELIVERY_DELAY, RENDERING_FAILURE, plus OPEN only for the pixel) and
`adx-utskick-inbound` (receipt-rule S3 action notification) each deliver to an SQS queue in
eu-west-1 (`RawMessageDelivery=true`, retention 14 days, DLQ after 5 receives). No HTTPS
subscription and no SNS signature code: SQS access is IAM-authenticated (H.8), nothing is lost while
adx.se is down or during a deploy rollback, and the two web workers never handle per-mail events.

Tick phase 2 (`inbound/queues.py`), only when `UTSKICK_SQS_*` is set and `email_ready_at` or
`doi_ready_at` is set: poll each queue with `ReceiveMessage(MaxNumberOfMessages=10,
WaitTimeSeconds=0, VisibilityTimeout=60)`, every tick while any email was sent in the last 72 h or
a pending inbound row exists, otherwise every 5 minutes (`Switchboard.last_queue_poll_at`). Per
message, inside one `transaction.atomic()`: insert `EventReceipt(key)` (a duplicate means done),
apply the effects, commit, then `DeleteMessage`. An exception leaves the message; after 5 receives
it moves to the DLQ. `utskick_daily` alerts when either DLQ is non-empty; `manage:utskick_dlq`
shows counts and a "Skicka tillbaka" button (`StartMessageMoveTask`).

`events.apply` finds the recipient by `mail.tags.r` (verified against `mail.tags.a` and, when set,
`ses_message_id`):
- `Send`: adopt `unknown` (`ses_message_id`, `sent`).
- `Delivery`: `delivered`, soft counter reset.
- `Bounce` Permanent: recipient `bounced`, `Contact.email_state=bounced`,
  `Suppression(reason="bounce")`, counts in health. **Subtype `OnAccountSuppressionList`**: recipient
  `failed` with `skip_reason="ses_suppressed"` only; no contact change, no Suppression, excluded from
  health (the address was suppressed because of another sender).
- `Bounce` Transient: `email_soft_bounces += 1`, 5 in a row -> bounced.
- `Complaint`: recipient `complained`, consent `unsubscribed` (source complaint),
  `Suppression(reason="complaint")`.
- `Reject`, `RenderingFailure`: `failed`. `DeliveryDelay`: ignored. `Open`: `opened_at` (only
  recipients that carried a pixel).
Then the health check (D.9).

The configuration set uses `SuppressionOptions.SuppressedReasons=["BOUNCE"]`: hard bounces stay
suppressed account-wide (an address that does not exist), but a complaint against one customer never
blocks another customer's mail to that person; our own per-account Suppression handles complaints.

### D.8 Global readiness and per-account kill switches

- Nothing sends sms unless `Switchboard.sms_enabled` (which requires `links_ready_at` and
  `sms_inbound_ready_at`). The confirm view, test send and the tick refuse sms otherwise, and the UI
  says "Sms-utskick är inte påslagna än." on the Kanal step and in Granska. Kontakter (S1) works
  regardless.
- Nothing sends email utskick unless `Switchboard.email_enabled` (requires `email_ready_at` and
  `UTSKICK_EMAIL_LIVE`). Email modes say "E-post är inte påslaget än." DOI mail additionally needs
  `doi_ready_at`; without it the signup page and the LP offer no email checkbox.
- In production with `UTSKICK_EMAIL_LIVE=false` nothing is written to disk: `.eml` files exist only
  with `settings.DEBUG`.
- Per account: the agency's disable endpoint (`manage:utskick_customer_update` with `is_enabled`
  off), Flamingo off, the customer turned inactive, or `sending_blocked` move every `scheduled`,
  `freezing` and `sending` utskick to `paused` reason `account_disabled` (or `blocked`) and pause
  active flows, in one transaction. The send-time checks re-read these flags. Turning utskick back
  on never resumes anything by itself.
- Inbound STOPP/START, suppression and unsubscribe pages keep working for disabled accounts (test).

### D.9 Health, ramp and abuse limits

- **Email per utskick** (`sending/health.py`, checked every 50 sends and on each event): hard
  bounces / (delivered + bounced) >= 4% with at least 200 outcomes -> `paused_health` `bounces`;
  complaints >= 2 and complaints / delivered >= 0.08% -> `paused_health` `complaints`. Agency
  alert. AWS pauses at 5% and 0.1%.
- **Email per account**, rolling 30 days, at least 500 sent: same thresholds block new email sends
  until the agency clicks "Släpp spärren" (`manage:utskick_health_release`).
- **Email probe**: while `email_probe_passed_at` is null (new account) or the sender domain's
  `probe_passed_at` is null (new domain), an utskick sends its first 200 recipients, then sets
  `hold_until = now + 60 min`; after the hold, if outcomes are under the thresholds the probe is
  passed and sending continues, else `paused_health`.
- **Email daily cap**: 2 000 per account per Stockholm day for the first 14 days after
  `email_first_sent_at`, unless the agency set `email_daily_cap`; over it the utskick waits for the
  next day (deferring, not a pause) and the report says so.
- **Sms per utskick**: STOPP replies plus `/s/` unsubscribes >= 2% of delivered (at least 100
  delivered) -> `paused_health` `stops` + agency alert. Operators can block the shared number, which
  would break replies for every customer.
- **Agency alerts**: an import over 1 000 rows with consent choice "consent" or "existing"; an
  account's first utskick over 500 recipients (`first_utskick_alerted`); information utskick rules
  (H.5).
- **ADX-wide**: `utskick_daily` reads SES `GetAccount` in eu-west-1 (enforcement status, quota,
  MaxSendRate) and our own 30-day rates; agency alert at 2.5% bounces or 0.05% complaints.
- `UtskickSettings.sending_blocked`: staff flag on the card ("Stoppa all sändning för kunden") with
  a reason; it pauses per D.8 and the app shows I.5's `blocked` copy.

---
## E. Tracking

### E.1 Link host router (S2)

`apps/utskick/links.LinkHostMiddleware` (right after `SecurityMiddleware`): if `request.get_host()`
without port is in `UTSKICK_LINK_HOSTS`, set `request.urlconf = "config.urls_links"`,
`request.is_link_host = True`, `request.link_host = "k" | "klick"`. Every link-host response gets
`X-Robots-Tag: noindex, nofollow`. Only the 302 click redirects, the open pixel and the `.ics`
response get `Referrer-Policy: no-referrer` and `Cache-Control: private, no-store, max-age=0` (the
code in the path must not leak to the destination). HTML pages keep Django's default `same-origin`.

**CSRF on link hosts**: every POST there (`/s/`, the Ångra button, `/p/`, `/a/`, `/b/`) is
`csrf_exempt` and authorised by the code or token in the path plus a **signed form nonce** rendered
by the GET: `fn = <issued36>.<sig16>` with `sig = HMAC(UTSKICK_LINK_KEY, f"form:{code}:{issued}")`,
valid 2 h. No csrftoken cookie is ever set on k./klick (no `{% csrf_token %}` in
`templates/utskick/links/`; guard). One-click `List-Unsubscribe=One-Click` POSTs need no nonce. Tests
use `Client(enforce_csrf_checks=True)`, `HTTP_ORIGIN="null"` and no Referer, and assert that no
response sets a cookie.

`config/urls_links.py` (namespace `links`, `re_path`, no trailing slash forced):

| Path | Name | Host | Stage |
|---|---|---|---|
| `^$` | `home` | both | S2: neutral page "Den här adressen används för länkar i sms och mejl som skickas med ADX Flamingo." + ADX privacy link |
| `^robots\.txt$` | `robots` | both | S2 |
| `^(?P<code>[A-Za-z0-9]{6})$` | `click` | k | S2 |
| `^s/(?P<code>[A-Za-z0-9]{6})$` | `sms_unsubscribe` (GET page, POST) | k | S2 |
| `^p/(?P<code>[A-Za-z0-9]{6})$` | `sms_preferences` (GET page, POST) | k | S2 |
| `^b/(?P<code>[A-Za-z0-9]{6})$` | `confirm` (GET page with a button, POST confirms) | k | S2 |
| `^m/(?P<token>[A-Za-z0-9.]{12,40})$` | `email_click` | klick | S3 |
| `^a/(?P<token>[A-Za-z0-9._-]{40,120})$` | `email_unsubscribe` (GET page, POST button or one-click) | klick | S3 |
| `^v/(?P<token>[A-Za-z0-9._-]{40,120})$` | `email_preferences` | klick | S3 |
| `^w/(?P<token>...)$` | `web_view` ("Visa i webbläsaren") | klick | S3 |
| `^o/(?P<token>...)\.gif$` | `open_pixel` | klick | S3 |
| `^c/(?P<token>...)\.ics$` | `calendar` (event block) | klick | S3 |
| `^s\.(?P<ver>[0-9a-f]{8})\.js$`, `^v$` | `snippet`, `snippet_beacon` | klick | S4 |
| `^(?P<account>[a-z0-9_-]{1,40})/(?P<slug>[a-z0-9-]{1,40})$` | `named` | klick | S4 |
| the same with capitals (`[A-Za-z0-9_-]`), after `named` | `named_folded` (301 to lower case) | klick | S4 as built |

### E.2 Codes and tokens

- Sms codes: 6 chars from `[A-Za-z0-9]` via `secrets.choice` (GSM-7 basic characters, so a link
  never forces UCS-2). 62^6 = 56.8 billion; at 10 million live codes a random guess hits about 1 in
  5 700. Misses are counted per `ip_hash` with `Counter(scope="link_miss")` and 20 misses per hour
  give 429. Generated in batches, collisions redrawn (D.3).
- In the sms the link is written without scheme: `k.adx.se/a8Kf2X` (15 chars). iOS and Android
  auto-link it (acceptance check in S2).
- **All signatures use `UTSKICK_LINK_KEY`** (stable, required in production, H.7), never
  `SECRET_KEY`, so a `SECRET_KEY` rotation never breaks a link in a delivered message.
- Email click links: stateless `klick.adx.se/m/<r62>.<l62>.<sig8>`. No table rows.
- `ut` token on the LP URL: `<click62>.<sig10>`.
- Email unsubscribe and preference tokens (`/a/`, `/v/`, the S1 `/utskick/val/<token>/`) are
  self-contained: `<account36>.<channel>.<valuehash43>.<sig16>` (hash as base64url). The unsubscribe
  works from (account, channel, value_hash) without the Recipient row, so links in old mails keep
  working after retention and after a GDPR delete.
- Reply-To and mailto tokens must fit a 64-octet local part: `r<account36>.<recipient36>x<sig10>`
  (reply) and `u<account36>.<recipient36>x<sig10>` (mailto unsubscribe). When the recipient row is
  gone, the handler falls back to the account in the token plus the `From` address of the incoming
  mail.
- DOI tokens embed the consent id, the value hash and an issue day, expire after 14 days.
- The form nonce is described in E.1.

### E.3 Click endpoint (`link_views.click`)

```
HEAD -> 302 to the bare destination (no ut, no logging)
lc = LinkCode.select_related("recipient__utskick", "link__campaign").filter(code=code, kind="link").first()
missing -> Counter link_miss, 404 "Länken har gått ut." with no account data
kind = classify(request, recipient)
bot     -> Recipient.bot_hits += 1, TrackedLink.bot_hits += 1 (F() updates), 302 to the destination without ut
human/scanner -> if limits.hit("click", f"{r.pk}:{link.pk}", hour, 20): increment repeat_count on the last row
                 else Click.objects.create(...)
                 if human: Recipient.update(click_count=F()+1, first_clicked_at=Coalesce(...))
return 302 build_destination(link, recipient, click)
```

At most four queries, no outbound calls, so the redirect stays in milliseconds. TrackedLink
counters are rolled up by the tick.

`classify`: `bot` when `analytics.utils.is_bot(UA)` (empty UA counts; catches facebookexternalhit
used by iMessage) or the UA matches an extra list (WhatsApp, TelegramBot, Slackbot, Discordbot,
SkypeUriPreview, LinkedInBot, Twitterbot, Google-PageRenderer, BingPreview, Microsoft Office,
Outlook link preview, Barracuda, Mimecast, Proofpoint, python-requests, curl, Go-http-client);
`scanner` for email when the click comes within 15 s of delivery, or when 3+ distinct links of the
same recipient arrive within 2 s; else `human`. A `scanner` click later followed by an LP
engagement beacon is upgraded to `human`. Only human clicks count anywhere in the UI.

`build_destination`:
- `lp` links: `campaign.landing_url` made absolute with `exports.landing_base_url()`, plus `ut`,
  `utm_source=flamingo`, `utm_medium=sms|email`, `utm_campaign=utskick-<pk>`. Creating an lp
  TrackedLink requires `campaign.account_id == account.id`.
- `external` links: the stored absolute destination (E.8), existing query and fragment kept, utm
  appended when `add_utm`, `adx=<token>` appended only when the destination host is a `SiteSnippet`
  domain (or a subdomain of it) whose `last_seen_at` is set (S4). Codes never carry URLs, so there
  is no open redirect.

### E.4 Landing pages (D10)

- `attribution.resolve(ut, campaign)` returns the click only when the signature is valid **and**
  `click.account_id == campaign.account_id`. Otherwise the token is ignored completely: no visit, no
  lead link, no raised limits.
- `landing()` GET with a resolved click: `click.lp_visits += 1`, `first_visit_at`,
  `Event(kind="lp_visit")` once per click per 30 minutes. Agency users, previews and the demo are
  not logged.
- Engagement beacon `lp/<slug>/besok/`: `engaged_seconds = max(old, s)` capped at 1800; at most one
  write per click per 10 s (`Click.beacon_at`); ignored for unresolved tokens. "Stannade 30 s+" in
  the report is `engaged_seconds >= 30`.
- Form lead: `ut` travels as a hidden field; in `limits.create_form_lead` under the existing
  advisory lock, `attribution.attach(lead, click)` sets `Lead.utskick`, `Lead.utskick_recipient`,
  `Lead.attribution`, and `Lead.contact` = the recipient's contact **only if** the form's phone or
  email matches it (a forwarded link must not merge two people).
- Contact capture (`capture.from_lead_form`) runs only when `access.can_collect(account)`: it
  creates or updates a contact only when a consent box was ticked or a same-account `ut` matched;
  otherwise it only links `Lead.contact` to an existing contact on an exact phone or email match.
  Leads from before activation are never backfilled. Test: a lead on an account that is off, or
  has no current DPA, creates no Contact and leaves `Lead.contact` null.
- Call click with a resolved `ut`: `click.called = True`, the lead gets the same attribution.
- No cookie, no storage, no ADX analytics: `test_security.py:828` is extended with `?ut=` and the
  beacon. A token from account A on account B's page gives no attribution (test).
- Lead-to-utskick window: a click older than 30 days still attributes the lead but sets
  `attribution["late"] = True`; the report counts it separately.

### E.5 Unsubscribe, preference and confirm pages

Semantics (H.6): **the offer toggles change Consent only** (reklam stops, information continues);
**"Avregistrera mig från allt", STOPP and the `/s/` and `/a/` links create a Suppression** (every
utskick and flow message on that channel stops). Pages show masked details only: `070-*** ** 67`,
`a***@e***.example`, never a name.

- `k.adx.se/s/<code>` (person code), GET: "Vill du sluta få sms från Exempelrör?" with the masked
  number and the button "Avregistrera mig". POST: Suppression sms (reason `link`), ConsentLog, page
  "Du får inga fler sms från Exempelrör." plus:
  - an **Ångra** button valid 30 minutes, authorised by a one-time nonce returned by this POST
    (`HMAC(LINK_KEY, f"undo:{suppression.pk}:{issued}")`, single use recorded in the ConsentLog);
  - the email question "Vill du sluta få e-post från Exempelrör också?" (unsubscribing is always
    allowed).
- `k.adx.se/p/<code>` (person code): "Vad vill du få från Exempelrör?" Rows: "Sms med erbjudanden"
  (on/off), "E-post med erbjudanden" (on/off, with `pref_email_note` when set), "Information om
  dina bokningar skickas så länge du inte avregistrerar dig från allt." (hidden once suppressed),
  and the button "Avregistrera mig från allt". Turning a channel **off** works at any time (sets
  `declined`). Turning sms **on** or lifting an sms suppression sends a confirm sms with a
  `k.adx.se/b/<code>` link valid 24 h; turning email on sends a DOI mail. A channel without an
  address shows "Anmäl dig" with an address field (starts the same confirmation).
- `k.adx.se/b/<code>` (confirm code): GET shows "Bekräfta att du vill få sms från Exempelrör." and
  a button; POST sets consent `yes` (source `confirm`, `confirmed_at`), lifts the suppression when
  the purpose is `start` or `pref_on`, marks the code used.
- `klick.adx.se/a/<token>`: GET shows the page with a button; POST with body
  `List-Unsubscribe=One-Click` (mail clients) or from the button (nonce) suppresses email and returns
  200 with an empty body for one-click.
- `klick.adx.se/v/<token>` (S3) and `adx.se/utskick/val/<token>/` (S1, before the link hosts
  exist): the email preference page, same rows as `/p/`.
- Every page links "Så hanterar Exempelrör dina uppgifter" (H.5).
- Templates `templates/utskick/links/*.html`, CSS `static/css/utskick-public.css`, logo from the
  customer's `EmailImage` logo rendition or the display name.

### E.6 Own-site snippet (S4)

`<script src="https://klick.adx.se/s.<ver>.js" integrity="sha384-..." crossorigin="anonymous"
data-k="<SiteSnippet.key>" async></script>` in the customer's `<head>`; `s.<ver>.js` is a static,
versioned file (`static/utskick/s.js`, version = first 8 hex of its sha256) served with an SRI hash
shown in the install text. Script (under 2 kB, no cookies, no storage): reads `adx` from
`location.search`, removes it with `history.replaceState`, sends
`navigator.sendBeacon("https://klick.adx.se/v", text/plain JSON {k, t, p, s})` on load and on
`pagehide` with engaged seconds; exposes `window.adxFlamingo.track(name)` for a conversion on the
same page. The endpoint (`csrf_exempt`) checks that `t` is a valid click token of the key's account
and that the `Origin` host equals or is a subdomain of `SiteSnippet.domain`, updates the click like
the LP beacon and `last_seen_at` (at most hourly). Only the visit from the link is known. The
install text says: "Utesluta parametern adx i din webbanalys (till exempel Google Analytics), så
att den inte syns i dina rapporter." The settings page shows "Installerat, senast sett i går" or
"Inte sett än" (until then no `adx=` is appended, E.3).

### E.7 Retention (server: t3.small, 2 GB RAM, disk about 76% full)

| Data | Kept | Then |
|---|---|---|
| `Click` human rows | 13 months | deleted; counts stay on `Recipient` and `TrackedLink` |
| `Click` scanner rows | 14 days | deleted |
| Bot hits | never stored | counters only |
| `LinkCode` kind link | 13 months after the utskick finished | deleted; old links show "Länken har gått ut." |
| `LinkCode` kind person | 36 months | `recipient` nulled at 13 months, then deleted |
| `LinkCode` kind confirm | 7 days | deleted |
| `Recipient` | 13 months after the utskick finished (flow recipients: 13 months after `created_at`) | deleted in batches of 5 000; `Utskick.stats` keeps totals |
| `Event` | 25 months | deleted |
| `ConsentLog` with a live contact | life of the contact | |
| `ConsentLog` with deleted contact | 36 months | deleted |
| `Suppression` | forever (per account) | |
| `InboundMessage` | 90 days (body cleared at routing) | deleted |
| `Thread`, `ThreadMessage` and reply leads | as long as the inbox lead exists; question K.2.3 | |
| Import file and CSV | done, cancelled or failed + 24 h; abandoned after 7 days | files deleted, `sample` cleared, job row 90 days |
| `EventReceipt` | 3 days | deleted |
| `Counter` | 2 days | deleted |
| `ExportLog` | 25 months | deleted |
| `EmailImage` files | while referenced by a draft or by recipients | deleted with the last reference |
| `sms.SmsMessage` | forever (apps/sms decision, billing record); `to` and `body` blanked on GDPR delete for `source != "api"` (H.4, K.2.6) | |
| Contacts with no basis and 24 months without activity | flagged by `utskick_daily` | the Kontakter page offers "Rensa inaktiva kontakter" (the customer decides) |
| nginx access log | link hosts and `/api/utskick/` not logged (C.5); the rest per the box's logrotate (14 days) | |
| `backups/utskick*.log` | 8 weeks (logrotate) | |

When a customer leaves: utskick off keeps data; the staff action "Avsluta utskick och radera allt"
on the card (typed confirmation of the company name) deletes contacts, lists, tags, fields, imports,
utskick, recipients, clicks, threads and events, and keeps suppressions and the consent logs
(pseudonymous proof). Automatic deletion after deactivation is question K.2.3.

Size guide: one recipient is about 300 bytes with indexes. 5 customers x 3 000 contacts x 4 utskick
per month is 60 000 rows, about 18 MB per month, about 230 MB at steady state; `SmsMessage` grows by
the same count with bodies. The box also keeps 14 local gzip dumps (`server/backup.sh`).
`utskick_daily` logs the table sizes and alerts the agency when filesystem free space is below 15%;
freezes refuse to start below 8% (D.3).

### E.8 External links

External `TrackedLink` destinations and named links must be absolute `http` or `https` URLs:
`validate_url` alone also accepts `/path` and `#x` (`security.py:305-313`), so `links.clean_external`
adds the absolute check and refuses IP literals, ports other than 80 and 443, userinfo
(`user@host`), known URL shorteners (bit.ly, tinyurl.com, t.co, goo.gl, ow.ly, is.gd, buff.ly,
rebrand.ly, cutt.ly, shorturl.at), and the link hosts themselves. gclid/gbraid/wbraid are stripped.

Hosts allowed without review: the account's verified sender domains, its snippet domains (not
as built, see "S4 as built"; pending Giovanni), the website in ADX's customer register
(`Customer.website`, which only staff edit; never `FlamingoAccount.website_url`, which the
customer types in onboarding before anything is fetched), and `links.GLOBAL_HOSTS` (google.com/maps, maps.app.goo.gl, g.page, search.google.com,
facebook.com, instagram.com, linkedin.com, youtube.com, tiktok.com, reco.se, each with
subdomains). A public suffix or shared host (`links.SHARED_HOSTS`: co.uk, org.se, github.io and
the like; `PATH_TENANT_HOSTS`: sites.google.com, facebook.com, linktr.ee and the like) is never
the customer's own. Dot segments in the path are resolved before any prefix check
(`/maps/../url` is `/url`), and redirectors on the free hosts (`l.facebook.com`, `/url`,
`/redirect`, `/redir`, `/l.php`, `/link`, `/away`) are refused with "Länken går till en
omdirigering. Använd adressen till sidan den leder till." Any other host creates
`AllowedHost(status=pending)` and an agency alert; the link editor shows "Väntar på ADX: länkar
till nya webbplatser godkänns av ADX." and Granska blocks until it is approved on the customer
card (`manage:utskick_host_decide`). A refused host shows "ADX har inte godkänt länkar till
example.com." The host is checked again after the confirmation (freeze start and the pre-checks
pause with reason `content`, D.3) and at every click (`links.destination_ok`: a refused, pending
or no longer own host answers "Länken har gått ut").

---

## F. Email builder (S3)

### F.1 Registry (`apps/utskick/email/registry.py`)

Reuses `pagebuilder.registry.Field`, `Variant`, `BlockType` and their `as_dict`. Refactor once:
`blocks.new_block`, `add_version`, `active_fields`, `visible_items`, `validate_blocks`,
`clean_fields` and `media.media_ids_in` take `types=` (default page `TYPES`), so email passes
`EMAIL_TYPES`. Version signing reuses `sign_version` with its own salt `apps.utskick.email.version`
(a version copied from a page must not keep a valid "ai" source). Every media id in `email_doc`
goes through `access.owned_ids(MediaAsset, account, ids)` on save and again at freeze.

The 24 Brev elements: header (1) and footer (24) are document-level and not in the block list; 2 to
23 are movable blocks.

| # | Key | Name | Fields (key: kind) | Notes |
|---|---|---|---|---|
| 1 | header | Sidhuvud med logga | doc: `logo_position` left/center/none | Logo = PNG rendition of `MediaAsset.is_logo`, 40 px high (80 px file), max width 220. "Visa i webbläsaren" sits above the logo and follows its alignment (right for left logo, center for centered, left without logo). No logo uploaded: choice locked to none, "Ladda upp en logotyp under Media." |
| 2 | hero | Rubrik och bild | image: media, kicker: text 40, title: text 90 (req), lead: textarea 300, button_text: text 30, button_url: url | |
| 3 | heading | Rubrik | text: text 90 (req), size: choice h2/h3 | |
| 4 | text | Text | body: rich_basic 3000 (req) | Paragraphs, bold, italic, links, bullet list |
| 5 | button | Knapp (fylld och kantad) | primary_text, primary_url (req), secondary_text, secondary_url, align: choice left/center | |
| 6 | image | Bild med bildtext | image: media (req), caption: text 160, url: url | |
| 7 | image_text | Bild och text | image: media, title: text 80, body: textarea 400, link_text: text 30, link_url: url, side: choice left/right | Stacks on mobile |
| 8 | columns | Kolumner | items 2..3: number text 4, title text 40, text text 120 | Stacks on mobile |
| 9 | divider | Avdelare | none | |
| 10 | offer | Erbjudande med kod | valid_until: date, title: text 60, text: text 160, code: code | Locked for information ("Erbjudanden hör inte hemma i information."). Saving it adds the terms to `confirmed_terms`. |
| 11 | prices | Tjänster och priser | title: text 60, note: text 60, items 1..10: name text 60, price text 30 | Prefilled from confirmed price facts. Locked for information. |
| 12 | reviews | Omdömen | source: choice google/reco, count: choice 1/2/3, show_summary: choice yes/no | Summary line "★★★★★ 4,8 av 5 · 162 omdömen på Google" from the snapshot. Requires a trusted Google profile or Reco (re-checked at freeze, snapshotted into the frozen doc). Stars U+2605 in `#E8A400`. Locked otherwise: "Koppla Google-profilen under Företaget." |
| 13 | steps | Så går det till | title: text 60, items 2..5: rich_basic 160 (bold only) | Numbers in accent outline circles; the bold lead word comes from `**...**` |
| 14 | event | Datum eller händelse | date: date (req), start: time, end: time, title: text 80, place: text 80, calendar: choice yes/no | Month box "OKT / 24" in accent; date line "Fredag 24 oktober kl. 15 till 18" (no end: "kl. 15"; no start: date only); "Lägg till i kalendern" -> `klick.adx.se/c/<token>.ics` |
| 15 | person | Kontaktperson | photo: media, name: text 60 (req), role: text 60, phone: phone, email: email | Initials when no photo |
| 16 | video | Video | thumbnail: media (req), title: text 80, url: url (req) | Rendition with a drawn play button (Pillow); no embedded video |
| 17 | gallery | Bildgalleri | items 2..4: image media, alt text 120 | Two per row, also on mobile (as the mockup) |
| 18 | faq | Vanliga frågor | title: text 60, items 1..6: q text 120, a textarea 400 | Static, no accordion |
| 19 | hours | Öppettider, adress och karta | show_hours, show_address: choice yes/no, map_text: text 30, map_image: media | From confirmed hours/address facts; "Hitta hit" links to a Google Maps search for the address; no Static Maps API |
| 20 | callout | Ruta (framhävd text) | text: rich_basic 300 (bold only, req) | Soft background; "**PS.** ..." renders the bold lead |
| 21 | signature | Underskrift | greeting: text 40, script_name: text 30, photo: media, name: text 60, line: text 120, phone: phone | Avatar (photo, else initials in an accent circle) beside "Johan Lind / Exempelrör AB · 08-123 456 78" with a `tel:` link. Script font stack only (Snell Roundhand, Segoe Script, Bradley Hand, cursive) |
| 22 | spacer | Mellanrum | size: choice s/m/l | |
| 23 | social | Sociala medier | items 1..5: network choice facebook/instagram/linkedin/youtube/tiktok, url | Brev: text links in accent |
| 24 | footer | Sidfot (låst) | none (locked) | Company name, address (required for reklam), phone; reason line from `Recipient.basis` ("Du får det här eftersom du har sagt ja till erbjudanden via e-post." / "... eftersom du är kund hos oss." / "... eftersom ditt företag är kund hos oss." / information: "Det här är information om ditt ärende hos oss."); "Ändra vad du får · Avregistrera dig · Visa i webbläsaren · Så hanterar Exempelrör dina uppgifter" |

New field kinds in `email/blocks.py` (page kinds unchanged):
- `url`: absolute `http`, `https`, `mailto` or `tel` only (relative paths and `#x` refused), max
  500, click ids stripped, host rules of E.8 for http(s), **no merge tags**.
- `date` (ISO, rendered "24 oktober", "Till 31 oktober"), `time` (`HH:MM`), `email`
  (`validate_email`), `phone` (`numbers.parse`, rendered "08-123 456 78"), `code`
  (`[A-Z0-9-]{3,20}`, upper-cased).
- `rich_basic`: plain text with a four-feature syntax (blank line = paragraph, `**fet**`,
  `*kursiv*`, `[text](url)`, lines starting `- ` = list), parsed into an AST and rendered by us, so
  field values stay plain text, never HTML. Link targets follow the `url` rules. A `bold_only`
  option drops links, italics and lists.
Values pass `_clean_value` (HTML stripped, typography normalised).

### F.2 Document and accent

`Utskick.email_doc = {"blocks": [...]}` with the page builder block JSON (`id`, `type`, `variant`,
`active`, `versions`, `sig`). Save is `email/blocks.save(utskick, blocks, rev)` -> validate, sign,
conditional `filter(email_rev=rev).update(...)`, `StaleRevision` gives 409 (same as
`pagebuilder/pages.save_draft`). `MAX_BLOCKS = 30`.

Accent (`email/style.py`): must match `^#[0-9A-Fa-f]{6}$`. `palette_for_accent(hex)` reuses
`render.mix`, `luminance`, `contrast`, `ensure_contrast`: `button_bg = hex`; `button_text =
#111111` if `contrast(hex, #FFFFFF) < 4.5` else `#FFFFFF`; `accent_text = ensure_contrast(hex,
#FFFFFF, 4.5)` for links, numbers, quote lines and the date box. Default accent:
`logo_colors_for_account(account_id)["primary"]`, else `#1A57D6`.

Picker (`flamingo-app-brev.js`): up to 3 logo colours (`logo_colors_for_account(...)["colors"]`),
then the five mockup swatches Blå `#1A57D6`, Grön `#1F7A4D`, Röd `#B42318`, Lila `#6D28D9`, Svart
`#111111`, then "Egen färg" (`input type=color` plus a hex text field). Under the picker the warning
"Ljus färg: knapptexten blir svart och länkarna får en mörkare nyans." when it applies. Demo and
seed data never use a real customer's colours.

### F.3 Merge tags

Allowlist: `{förnamn}`, `{efternamn}`, `{namn}`, `{företag}` (the contact's company),
`{fält:<key>}` (FieldDef keys). Inline fallback `{förnamn|du}`, else `Utskick.merge_fallbacks`.
Unknown tags are a validation error. Same engine for subject, preheader, blocks and sms
(`composer.merge`, following the closed-allowlist regex of `website/templatetags/render_context.py`).
Values are HTML-escaped in HTML, raw in text and sms; in subject, preheader and sms they are made
single-line (CR and LF stripped) and capped at 60 characters each. Merge tags are refused in URL
fields. Checks count recipients missing each used value ("214 saknar förnamn, Hej, används").

### F.4 Renderer (`email/render.py`)

- `render_html(utskick, ctx, mode)` with `mode` in `editor | preview | send`; `render_text(...)`
  from the same blocks (not from HTML).
- Templates `templates/utskick/brev/layout.html` and `templates/utskick/brev/blocks/<key>.html`.
  Inline styles come from one Python dict `S = brev_styles(palette)` passed to every template
  (`style="{{ S.h1 }}"`). These templates are outside `templates/flamingo/`, so the "no `style=`"
  guard does not apply; their own guard checks: no `<script`, no `class=`-dependent layout, no
  external fonts, no relative URLs.
- Geometry from the Brev mockup: content width **560 px**, side padding **28 px** (20 px under 620
  px), **30 px** below each block, header 28 px top. Outer 100% table with white background, inner
  table `width="560"` and `max-width:560px` (`<!--[if mso]>` fixed-width wrapper for Outlook).
- Tokens copied from `.t-brev` into `brev_styles`: ink `#202124`, text `#3C4043`, muted `#5F6368`,
  line `#E3E6EB`, soft `#F5F7FA`, star `#E8A400`, footer ink `#5F6368`, inner radius 6, button
  radius 6, h1 28/700, h2 24/700, h3 17/700, lead 18, body 16/1.65, button 16/600 with 15 x 24
  padding. `test_s3_render.py` parses the mockup's `.t-brev` block and compares.
- Structure: `<!doctype html>`, `<meta name="color-scheme" content="light">`, `<meta
  name="supported-color-schemes" content="light">`, hidden preheader span.
- Mobile: hybrid/fluid. Columns are `display:inline-block` tables with `max-width`, so they stack
  without media queries; a `<style>` block with `@media (max-width:620px)` refines padding and
  heading size (Gmail app ignores it, the layout still holds). The gallery stays two-up.
- Buttons: table-cell buttons with `bgcolor` and padding (no VML; square corners in Outlook).
- Dark mode: white backgrounds declared explicitly on every cell; logos with transparency get a
  white rendition background. Images always have `width`, `height`, `alt`, `display:block`.
- Fonts: system stack `-apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif`.
- `send` mode: every `href` passes `links.email_url(utskick, recipient, link)` (TrackedLink frozen
  at freeze in `Utskick.stats["links"]`), except `mailto:`, `tel:` and our own unsubscribe,
  preference, web-view, privacy and calendar links. The open pixel (`klick.adx.se/o/<token>.gif`)
  is added only when `open_tracking` is on **and** `recipient.tracking_ok` (H.5).
- Images (`email/images.py`): `EmailImage` created when an asset is chosen in the editor (endpoint
  `app_brev_image`), through the existing `_DECODE` semaphore: JPEG quality 82, max 1120 px wide
  (560 displayed); PNG when the source has alpha (logos); absolute URL via
  `exports.landing_base_url()` + `file.url`. Never WebP (classic Outlook).
- Web view `/w/`: `Content-Security-Policy: default-src 'none'; img-src https:; style-src
  'unsafe-inline'; base-uri 'none'; form-action 'none'` and `X-Frame-Options: DENY`.

### F.5 Checks (`email/checks.py`, shared with sms in Granska, I.6)

| Check | Level |
|---|---|
| HTML size for the longest merge values incl. personal links under 102 kB ("Gmail kapar större mejl, och då försvinner avregistreringen") | blocks |
| Footer address present for reklam (Företaget > adress) | blocks |
| Subject present; own domain verified, or ADX domain under the month cap | blocks |
| Unsubscribe in footer and one-click header | always true (locked), shown as a tick |
| Every link host allowed (E.8) | blocks |
| Information content rules (H.5) | blocks (staff override with logged reason) |
| Links answer (shared checker, sms and email) | warning |
| Images without alt text | warning |
| Missing merge values (count per tag) | warning |
| Guard problems in AI-written text (F.7) | warning |

**Link checker** (`links.check_destinations`): only `http`/`https` destinations, through
`apps.tools.analyzer.fetch(url, max_bytes=16384, time_limit=5)` (validates scheme, port and public
IP on every hop and connects to the IP it checked; `SsrfTests` in `test_media.py`), in a thread pool
of 4 with a total budget of 15 s, at most 20 distinct destinations, results cached 10 minutes, at
most 10 runs per account per hour (`Counter(scope="link_check")`). The UI shows only "Svarar" or
"Svarar inte", never the body or the error text.

### F.6 Editor UX: parameterise flamingo-pb.js

Decision: mount the existing `static/js/flamingo-pb.js` with a "brev" profile, not a second editor.
Reasons: drag and drop, versions, autosave with `rev`, the media picker, keyboard moves and the AI
panel (`flamingo-pb-ai.js`, which only uses the `window.FlamingoPB` API) are 4 400 tested lines; a
fork would double every future fix. The hard-wired spots are few and known: canvas root
`main.rn-main > [data-pb-block]` (lines 845, 852, 965, 1143), `.rn-top` / `.rn-foot` chrome (974),
`--rn-primary` detection (1130), `ADD_WORDS` (116), `WIREFRAMES` (129), the hero+form and
`group==="end"` placement rules (568, 576), device widths. They move into `pb-config` (built
server-side): `canvasRoot`, `chromeSelectors`, `wireframes`, `addWords`, `devices` (`{"desktop":
560, "phone": 375}` for brev), `placement` rules, `profile`. Defaults equal today's values, and a
test asserts the page profile config is unchanged. Email-only UI (subject, preheader, sender,
accent, logo position, checks list, preview modes, test send) lives in
`static/js/flamingo-app-brev.js` on top of the public API. `rich_basic` fields open a side-panel
textarea with B, I, Länk, Lista buttons (B only for bold-only fields).

Canvas: srcdoc iframe with `EDITING_CSP` as today; editor mode renders the same table templates with
`{% pb %}` attributes. Previews: "mobil" (375), "dator" (560) and "mörkt läge" via
`app_brev_preview`.

### F.7 AI writing

`apps/utskick/ai.py` reuses `pagebuilder.ai.ask_model` (demo never, AI off, budget,
`limits.reserve_ai` shared quota of 20 per day, template fallback), `HARD_RULES`, and `Guard`. New
tools: `WRITE_SMS_TOOL` (one GSM-7 text, max one part including the placeholders, no links except
`{länk:x}`) and `WRITE_BLOCK_TOOL` (fields of one Brev block). The guard comes from
`make_utskick_guard(account, utskick)`, built like `ai.make_base` but with
`checks.build_context(list(account.confirmed_facts().values()) + terms, extra=[customer.name,
generator.company_name(customer)])` where `terms = [f"{t['label']} {t['value']}" for t in
utskick.confirmed_terms]`. The terms go into `fact_values`, not `extra`: `build_context` only feeds
`extra` into the allowed numbers, while `fact_text` (which lets an `URGENCY` or `SPEED` match
through) is built from `fact_values` alone (`checks.py:119`). `amounts` = confirmed prices plus
amounts found in the terms. Legitimate offer copy ("Erbjudandet gäller till 31 oktober", "VARME26",
"kl. 15") passes only after the customer has ticked "Uppgifterna stämmer" on the terms.
Customer-typed text is never blocked by the guard; it shows warnings. The AI never writes
information utskick content with prices or offers.

### F.8 Test send

`app_utskick_test` (POST, S2 sms, S3 email). The button names the address: "Skicka test till
anna@exempelror.example" or "Skicka test till 070-123 45 67".
- A customer user: to their own address or phone, or to `account.notify_phone`.
- Staff in view-as: only to the staff user's own address or phone by default. Sending a test to a
  customer contact needs the separate button "Skicka test till kunden (kunden får mejlet)" with a
  `<dialog>` and the I.4 checkbox (D12).
- Max 10 per account per Stockholm day (`Counter(scope="test_send")`, `Event(kind="test_send")`
  when the target is a contact), never on the demo, sms billed with `source="test"` and counted in
  the cap, the company-name check applies (H.5), email subject prefixed "Test: " and counted in the
  ADX-domain cap. Rendered with a chosen contact ("Förhandsvisning med Anna Lindqvist · byt
  kontakt"); the contact id goes through `access.owned`.

---
## G. Inbound

### G.1 Sms from 46elks (S2)

Endpoint `POST /api/utskick/46elks/inkommande/<token>/` (`webhook_urls.py`, name
`utskick_api:elks_inbound`, `csrf_exempt`, `never_cache`, nginx `access_log off`).

1. Token by `compare_digest` with `UTSKICK_ELKS_INBOUND_TOKEN`; client IP via `common.net.client_ip`
   must be in `SMS_DLR_ALLOWED_IPS` (reuse `sms.api._dlr_ip_allowed`). In production the view refuses
   to run (404, agency alert once per day) while that list is empty; 46elks' IPs are in
   `.env.example:98`. Failures: 404, empty body.
2. Fields `id`, `from`, `to`, `message`, `created`, `direction`. `to` must be
   `UTSKICK_REPLY_NUMBER`. `from` through `numbers.parse`; non-E.164 senders are stored as `ignored`
   and never answered (loop guard).
3. The whole handler runs in one `transaction.atomic()`: `InboundMessage` insert on `("sms", id)`
   (a duplicate commits nothing new and returns 200), routing, keyword handling, thread and lead
   writes. **Response: HTTP 200 with an empty body, only after commit**; any exception returns 500
   with an empty body (46elks sends any response text back as a reply sms). No outbound call happens
   in the request: confirmations and owner notices are queued for tick phase 3, and sent right
   after commit by `sending/kick.py` (see "After S3"; the tick is the fallback).
4. **Reconcile** (tick phase 2, every 10 minutes): `elks.list_messages(since=now - 48 h)` (GET
   `/a1/sms`, newest first, paged with `end`), keep incoming rows to the reply number and insert any
   missing id through the same handler function. A lost webhook delays a STOPP by at most 10
   minutes.
5. Routing (`inbound/routing.py`): candidate accounts are those whose `SmsMessage(to=from_e164,
   sender=UTSKICK_REPLY_NUMBER, status in ACCEPTED or RESERVED)` exist in the last 30 days (partial
   index, B.7), plus older ones by latest `created_at`.
   - STOPP and START: see 6 and 7 (they apply to every candidate).
   - Other text with exactly one candidate in 30 days (or none in 30 days and a latest older one):
     route to it. The latest message maps to a `ThreadMessage` (an inbox reply: same thread), a
     `Recipient` (an utskick: the contact's open sms thread from the last 30 days, else a new one),
     or a system sms (a confirmation: same thread as the STOPP).
   - Other text with **more than one** candidate in 30 days: `status=ambiguous`, held for the agency
     at `/manage/utskick/#inkommande` with "Koppla till kund" (`manage:utskick_inbound_route`) and
     shown to no customer; agency alert at most hourly.
   - No candidate: `status=unroutable`, same agency list.
   - The account's `can_collect` false (DPA not current): the thread is created without a Contact.
6. **STOPP** (`inbound/stop.py`): normalise (strip, upper-case, keep Å Ä Ö, remove punctuation and
   emoji). Auto-STOPP when the first word is STOPP or STOP (any length), or one of STOPPA, AVSLUTA,
   AVREGISTRERA, AVANMÄL, SLUTA, UNSUBSCRIBE with at most 8 words; never when the second word is
   INTE ("Stoppa inte min bokning"). For each candidate account (30 days, step 5): `Suppression(account, sms, hash,
   reason="stop", utskick=...)`, consent sms -> `unsubscribed` (source `stop`, by_label "Svar
   STOPP"), queued sms recipients of that contact -> `skipped` (`suppressed`), `Recipient.stopped_at`,
   thread kind `stop` (or appended to an open reply thread), lead `contacted`. `meta["accounts"]`
   records the accounts. One confirmation sms **per account** (queued, source `system`,
   reference `x<inbound_pk>`, billed to that account, at most one per number, account and 24 h,
   cap pre-checked): "Du får inga fler sms från Exempelrör. Svara START om du ångrar dig." Each
   names only its own account: a combined text in the latest customer's thread (Inkorg and sms
   portal) told that customer which other ADX customers text the person (S2 security review).
7. **START** (same normalisation, first word START, at most 3 words): applies to the accounts in
   `meta["accounts"]` of that number's latest STOPP within 13 months. It does not lift anything by
   itself: it queues one confirm sms per account with a `k.adx.se/b/<code>` link (purpose `start`,
   24 h): "Klicka för att få sms från Exempelrör igen: k.adx.se/b/Ab12Cd". The click lifts the
   suppression and sets consent `yes` (source `start`).
8. **Looks like an unsubscribe**: a reply to a reklam send that contains "sluta", "ta bort mig",
   "vill inte ha", "inga fler", "avbryt" or "nej tack" (normalised) sets `Thread.looks_like_stop`; the thread
   shows "Ser ut som en avregistrering" with a one-click "Avregistrera från sms". Every reply thread
   has that button; it writes Suppression (reason `reply`) and a ConsentLog row with source `reply`
   and the acting user.
9. Anything else: thread message, `lead.status` back to `new` if it was `contacted`,
   `Recipient.replied_at`, `Lead.message` and `Lead.activity_at` updated. Owner notice
   (`notify_on_reply`): batched, at most one owner sms per account per 30 minutes
   (`UtskickSettings.reply_notice_at`), sent by tick phase 3 with `owner_reply_text` (C.2) covering
   all replies and utskick-attributed leads since the last notice. Never an autoreply.

Test table (`test_s2_inbound.py`): auto-STOPP for "STOPP", "stopp tack", "STOP!", "Stoppa",
"Sluta skicka", "AVANMÄL", "unsubscribe", "STOPP jag har bytt nummer och vill inte ha fler";
not STOPP for "Stoppa inte min bokning", "Stopp inte", "Sluta inte skicka påminnelser",
"Start"; flagged on a reklam send only for "Avbryt", "Ta bort mig", "nej tack", "Jag vill
inte ha fler sms".

### G.2 Answering from the inbox (S2 sms, S3 email)

`lead_reply` (POST, `app/inkorg/<pk>/svara/`): textarea with the live GSM-7 counter (the
`encoding.analyse` rules mirrored in JS, as `flamingo-app-onboarding.js` does), "Skickas från 0766
86 00 46 · 1 del · 0,39 kr". The sms runs `checks.send_time_checks` minus window and collision
(suppressed: "Personen har svarat STOPP."); when `display_name` is not in the text, " /Exempelrör"
is appended and the counter shows it. Then `sms_wrapper.send(source="reply",
sender=REPLY_NUMBER, reference=f"t{thread_message.pk}")` synchronously (one sms, like the API).
Email (S3): through the transport, from the utskick's sender, `Reply-To` with a thread token,
`In-Reply-To` when known. Staff in view-as must tick "Jag svarar som ADX åt Exempelrör."
(`sent_as_staff`). The demo refuses: "Demokontot skickar aldrig." The circuit breaker (D.4) gives
"Sms kan inte skickas just nu. Försök igen om en stund."

The contact card sms (S4, `app_contact_sms`) uses the same path and all send-time checks.

### G.3 Email replies via SES inbound in eu-west-1 (S3, D8)

- MX `svar.utskick.adx.se` -> `inbound-smtp.eu-west-1.amazonaws.com`; receipt rule for that domain
  with spam and virus scanning, action S3 (bucket `UTSKICK_SES_INBOUND_BUCKET`, prefix `in/`) with
  the notification topic `adx-utskick-inbound` -> SQS. The S3 action is used because the SNS action
  bounces mails over 150 kB (photos from phones).
- Tick phase 2 (`inbound/email.py`), at most 20 messages per tick and 500 per hour ADX-wide
  (`Counter(scope="inbound_mail")`; above it messages are only counted, status `counted`, and the
  agency is alerted):
  1. Pin the source: `receipt.action.bucketName == UTSKICK_SES_INBOUND_BUCKET` and the key starts
     with `in/`; anything else is dropped from the queue and logged.
  2. Validate the token from `receipt.recipients` (not the `To` header) first:
     `s+<token>@svar.utskick.adx.se`, kinds `r` (reply), `t` (thread), `u` (mailto unsubscribe)
     (E.2). An unknown or badly signed token gives `ignored` and the S3 object is deleted without
     being read.
  3. Verdicts: virus FAIL -> delete object, `status=spam`; spam FAIL -> `spam`, not in the inbox
     (count only).
  4. Store `InboundMessage(status="pending", meta={"s3_key": ...})` in the transaction with the
     EventReceipt; the fetch happens in the same phase after commit: `s3:GetObject` with the
     `adx-utskick` role, at most 10 MB processed (larger: metadata only, "Svaret var för stort för
     att visas här."), parse with `email.parser.BytesParser(policy=policy.default)`.
  5. Auto-replies dropped from the inbox (logged on the recipient only): `Auto-Submitted` not `no`,
     `X-Autoreply`, `X-Autorespond`, `Precedence: bulk|junk|auto_reply|list`, `multipart/report`,
     subjects starting "Autosvar", "Frånvaro", "Out of office", "Automatic reply".
  6. Body: `text/plain` preferred, else HTML stripped with `nh3` (no tags) and unescaped; quoted
     history cut at the first line starting with `>`, or "Den ... skrev:", "On ... wrote:",
     "-----Original Message-----", a "Från:" header block; max 20 000 chars. Attachments listed by
     name and size, never stored: "Bilagor sparas inte. Be avsändaren skicka dem till din vanliga
     adress."
  7. `u` token: email unsubscribe (source `list_unsub`), from (account, hash of the token's
     recipient address, or of the `From` address when the recipient row is gone). Otherwise thread
     and lead as for sms, with `From` checked against the recipient's address (a mismatch is still
     routed, marked "från en annan adress").
  8. Delete the S3 object after processing (lifecycle rule 7 days as backup). `utskick_daily` lists
     `in/` objects older than 30 minutes with no `InboundMessage` and processes them.

---

## H. Security, privacy and law

### H.1 Tenancy

- Every app view uses `@utskick_view` (= `flamingo.app_views.app_view` + enabled check, `Http404`
  when off, also for staff in view-as) and fetches ids from the path with `access.owned(model,
  account, pk)` (`get_object_or_404(..., account=account)`). Child rows go through their parent
  (`Recipient` via `utskick__account=account`).
- **Every id taken from a request body or JSON** goes through `access.owned_ids(model, account,
  ids)`, and any foreign id gives 400. This covers `Utskick.audience` (lists, tags, segments,
  contacts and the excludes), `Segment.rules` (list and tag ids), `Flow.trigger_config`,
  `FlowStep.config`, `Flow.audience_list`, `SignupForm.add_to_list` and `add_tags`,
  `ImportJob.target_list` and `target_tag`, `Utskick.sender_domain`, `TrackedLink.campaign`, media
  ids in `email_doc`, the F.8 preview contact, bulk-action ids, and the S5 API `contact_id`.
- Second line at use time: the freeze and the segment compiler start from
  `Contact.objects.filter(account=utskick.account)` and join with `list__account=` /
  `tag__account=`; the send loop re-checks `sender_domain.account_id == account.id` and `status ==
  verified`; an lp TrackedLink requires `campaign.account_id == account.id`.
- `access.can_collect(account)` (enabled and `dpa_ok`) guards **every path that creates contacts**:
  manual add, import, the signup page (404 without it), LP capture, contacts from inbound replies
  (threads without a Contact instead), the S5 API (403 `dpa_required`).
- Public and link-host views derive the account only from a signed token or a stored code, never
  from a parameter. Queues and webhooks derive it from routing data, never from the payload.
- Tests: one parametrised test walks every `flamingo:app_*` utskick route with another account's pk
  and expects 404 (pattern `test_inbox.py:608`); one posts a foreign id into each field listed above
  and expects 400; one writes a tampered `audience` straight to the DB and freezes: 0 foreign
  recipients; `timeline.for_contact` filters leads on `account=contact.account`.

### H.2 Tokens, webhooks and CSRF

See E.2. CSRF exemptions, exhaustive: 46elks inbound (path token + IP list), the LP engagement
beacon (signed `ut`), the own-site snippet beacon (signed click token + Origin), the events API
(bearer key, S5), and every POST on the link hosts (`/s/`, Ångra, `/p/`, `/b/`, `/a/` button and
one-click), which use the signed form nonce of E.1 instead. Each is `require_POST`, size-limited
and authenticated. Everything on adx.se (signup, `/utskick/val/`, `/utskick/bekrafta/`) uses
Django's CSRF.

### H.3 PII handling

- Logs carry pks only (`Contact 123`, never a number or address). `__str__` of Contact, Recipient,
  InboundMessage and ThreadMessage contain no PII (as `SmsMessage.__str__`). `IntegrityError` from
  the contact constraints is caught in `contacts` and `importer` and logged with pks only.
- Sentry: C.3 (locals dropped in every frame when an utskick, sms, LP or inbox frame is involved;
  number, email and query patterns; no traces on Kontakter and Utskick pages). Request bodies are
  never sent (`max_request_body_size="never"`).
- Import files live in `PRIVATE_MEDIA_ROOT/utskick-import/` (never `MEDIA_ROOT`), are deleted per
  E.7, and the error report re-reads the file instead of storing values.
- Exports (`app_contacts_export`, `app_utskick_export`) are POSTs with a confirmation step, use
  `flamingo.exports.safe_cell`, write an `ExportLog`, and are limited to 10 full exports per account
  per day. The Kontakter settings page shows "Senast exporterad av Anna Lindqvist 2 okt 2026" (or
  "av ADX").
- `.eml` files are written only with `settings.DEBUG` (D.8).

### H.4 GDPR per contact

- Export (`app_contact_export`, POST, logged): JSON with contact fields, consents and their log,
  list memberships and tags, recipients (utskick name, channel, times, status), clicks (time,
  device), threads and messages, linked leads, and the `SmsMessage` rows sent to the contact (time,
  text, status).
- Delete (`contacts.delete_contact`, POST with a confirmation page), in one transaction:
  - deletes the contact's threads and their messages and the reply leads (`source="reply"`);
  - offers checkboxes, ticked by default, "Ta också bort förfrågningar från personen (3)" for the
    other linked leads;
  - sets `Recipient.contact=null` and blanks `address` and `merge`;
  - blanks `to` (keeping `country`) and `body` on the contact's `SmsMessage` rows with `source !=
    "api"`, keeping parts, prices and reference (K.2.6);
  - clears `InboundMessage.from_address` and `body` for the contact, blanks `ConsentLog.evidence`;
  - keeps `ConsentLog` rows (contact null, hash and text shown) and existing `Suppression`;
  - adds `Suppression(reason="erasure")` per known address unless the customer unticks "Lägg på
    spärrlistan så att personen inte importeras igen" (ticked by default);
  - deletes the Contact (cascades consents, memberships, events, flow runs).
- The customer is controller, ADX processor: the DPA (D6, H.9) covers it.

### H.5 Consent proof, privacy notice and marketing law

- **Proof per consent**: exact `text_shown` as rendered (company name filled in), source and page
  path, time, actor, IP hash for public forms, confirmation time. Checkboxes are never pre-checked
  (test asserts no `checked`). Consent is tied to the address (`Consent.value_hash`, B.1).
- **Privacy notice (Art. 13)**: the signup page, the LP checkboxes, the DOI mail, the preference
  and unsubscribe pages and the email footer link "Så hanterar Exempelrör dina uppgifter" to
  `UtskickSettings.privacy_url` (https) or the generated fallback `/utskick/<public_slug>/integritet/`
  naming the company, org number and contact details (from Företaget facts), the purposes (utskick,
  link and landing-page tracking, replies), the E.7 retention periods, ADX as processor and the
  sub-processors (H.9). Without a privacy URL and with Företaget missing org number or contact
  details, the signup page and the LP checkboxes stay off ("Fyll i organisationsnummer och
  kontaktuppgifter under Företaget, eller ange en länk till din integritetspolicy.").
- **Marknadsföringslagen 19 §**: reklam by sms or email to natural persons needs prior consent
  (`yes`) or the existing-customer exception (`existing`). 20 §: to legal persons it is allowed
  unless they refused, with a valid opt-out in every message: we apply it to **email only**
  (status `company`), and derive `company` only when the email domain is not on the freemail/ISP
  list (`utskick/freemail.py`: gmail.com, googlemail.com, hotmail.com, hotmail.se, outlook.com,
  outlook.se, live.se, live.com, msn.com, icloud.com, me.com, mac.com, yahoo.com, yahoo.se,
  telia.com, telia.se, comhem.se, bredband.net, bahnhof.se, tele2.se, spray.se, passagen.se,
  glocalnet.net, home.se, protonmail.com, proton.me) and only from `missing` (never over
  `declined`, `unsubscribed`, `pending` or a suppressed hash). The import review says "12 företag
  med privat e-postadress får inte e-post utan samtycke." Sms to companies needs `yes` or
  `existing`. Sole traders are natural persons (org number = personnummer, not stored).
- **Identifiable sender**: every reply-number sms (reklam, information, flow, test send) must
  contain `display_name`, else Granska blocks with "Mottagaren ser bara ett nummer. Skriv
  Exempelrör i texten."; inbox replies get " /Exempelrör" appended (G.2); confirmations and STOPP
  answers name the company. Alphanumeric senders identify themselves. Email footer always carries
  name and address.
- **Information utskick** need no consent and ignore the weekly cap, so they are fenced:
  - `info_reason` is required (Bokning, Ärende, Ändrade öppettider, Driftstörning, Annat with a
    text), shown in the report and on the agency overview;
  - no `{länk}` to a Flamingo campaign page; no external links except the customer's own site
    (website fact host, verified sender domains, snippet domains);
  - prices, percentages, codes and offer words (`erbjudande`, `rabatt`, `kampanj`, `rea`, `fynd`,
    `gratis`, `spara`, `kr`, `%`, `kod`) **block** Granska ("Det här ser ut som reklam. Välj Reklam
    eller ta bort erbjudandet."); offer and price blocks are locked. Staff can override with a
    logged reason (`content_override`), never the customer;
  - agency alert when one information utskick goes to more than 200 recipients, or an account sends
    more than 2 information utskick in 30 days;
  - the opt-out line ("Svara STOPP för att inte få fler sms." or the `/s/` link) is added to
    information sms with more than one recipient as well.
- **Opt-out in every reklam message**: sms with the reply number gets "Svara STOPP för att inte få
  fler sms." appended when missing; with a name sender "Avregistrera: k.adx.se/s/a8Kf2X" (D4).
  Email: footer link and one-click header always.
- **Cookies and terminal equipment (LEK 9 kap. 28 §)**: no cookies or storage on `/lp/`, the link
  hosts or in the snippet. The open pixel is added only for recipients whose email Consent is `yes`
  with `tracking_ok` (the consent text shown included the sentence "Mejlen innehåller en bild som
  visar om de öppnas."; the sentence is part of `consent_text_email` while `open_tracking` is on).
  `existing` and `company` recipients never get a pixel. The settings toggle explains this:
  "Öppningar mäts bara hos dem som sagt ja till det när de anmälde sig."
- **Personnummer and special categories**: never stored in fields (B.1); the DPA says so and the
  field form shows "Spara inte personnummer, hälsouppgifter eller liknande i extrafält."

### H.6 Suppression and consent semantics

- Scope: per account (customer) and channel, keyed by value hash.
- Suppression (STOPP, `/s/`, `/a/`, one-click, mailto, complaint, "Avregistrera mig från allt",
  erasure, reply button) stops **every** utskick and flow message on that channel from that
  customer, reklam and information. A hard bounce sets `email_state=bounced` plus reason `bounce`.
- `declined` (the offer toggles on the preference pages) stops reklam only; information continues.
- The customer's API sms (`source="api"`) are not checked by apps/sms; the customer can query
  `GET /api/sms/v1/suppressions/?to=` (C.1), and the sms terms say a STOPP covers all their sms
  marketing.
- Import never lifts a suppression; a suppressed row in a file stays "Avregistrerad" ("Spärrlistan
  ligger per kund och vinner alltid"). Only the person lifts it: START plus the confirm click, a
  DOI click, the Ångra button within 30 minutes, the preference page plus confirmation.
- Every send path to a contact runs `checks.send_time_checks`: utskick, flows, test sends to
  contacts, inbox replies, contact-card sms.

### H.7 Keys and fingerprints

- `UTSKICK_HASH_KEY` and `UTSKICK_LINK_KEY` are required in production (`ImproperlyConfigured`),
  never derived from `SECRET_KEY` there, backed up in the password manager and with the DB backup
  notes.
- On first use `Switchboard.hash_fingerprint = HMAC(key, "utskick-fingerprint")` (and
  `link_fingerprint`) is stored. Every process compares before writing consent or suppression rows
  or sending: the web workers on first such write, the tick at start. On mismatch it refuses those
  writes and all sends, logs, and alerts the agency ("Nyckeln för spärrlistan skiljer sig mellan
  processerna."). This catches web workers that kept an old systemd environment while the cron tick
  reads the new `.env`.
- Order in the S1 checklist: keys into `.env`, `systemctl restart adx`, deploy, then crontab.

### H.8 AWS isolation

- Role `adx-utskick` (account 500841883756) with an inline policy `utskick`:
  - `ses:SendEmail`, `ses:SendRawEmail` on `arn:aws:ses:eu-west-1:500841883756:identity/*` and the
    configuration set;
  - `ses:CreateEmailIdentity`, `ses:GetEmailIdentity`, `ses:PutEmailIdentityMailFromAttributes`,
    `ses:DeleteEmailIdentity` on `identity/*` in eu-west-1;
  - an explicit **Deny** on `ses:DeleteEmailIdentity` and `ses:PutEmailIdentity*` for `adx.se`,
    `utskick.adx.se`, `svar.utskick.adx.se` and every identity that existed before S3 (listed from
    `aws sesv2 list-email-identities` in both regions);
  - an explicit **Deny** on `ses:SendEmail` and `ses:SendRawEmail` when `ses:FromAddress` is on any
    of those identities except `utskick.adx.se` (S3 review);
  - `ses:GetAccount`;
  - `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes`, `sqs:StartMessageMoveTask`
    on the two queues and DLQs;
  - `s3:GetObject`, `s3:DeleteObject`, `s3:ListBucket` (prefix `in/`) scoped to
    `arn:aws:s3:::<bucket>/in/*`.
  Trust: the instance role `django-ec2-instance-role` with `sts:ExternalId`.
- The instance role gets only `sts:AssumeRole` on `adx-utskick`, in a **separate** inline policy
  `utskick-assume` (`server/aws-instance-role.sh` uses `put-role-policy --policy-name
  bedrock-and-backups`, which replaces the whole policy; first run `aws iam list-role-policies` and
  `get-role-policy` and copy anything live but missing from the script, such as `sts:AssumeRole` on
  `ADXReadOnly` for `aws_sync`).
- `utskick.aws.session()` assumes the role from `cloud.aws.base_session()` with
  `UTSKICK_AWS_ROLE_ARN` and `UTSKICK_AWS_EXTERNAL_ID` (pattern of `cloud.aws.session_for`), cached
  until 5 minutes before expiry. Locally (`DEBUG`, no role ARN) it uses `ADX_AWS_PROFILE`.
- IMDSv2 required on the instance (C.5). Note (K.1.2): every site on the box runs as `djangouser`,
  so this limits exposure through the metadata service and SSRF, not through local code execution.

### H.9 DPA content (checklist for the lawyer, S1)

The page `/bitradesavtal/` must contain: the parties and roles (customer controller, ADX
processor); the processing (contact register, sending, tracking, replies); sub-processors and
locations: AWS EC2 in eu-north-1 (Stockholm: the server and its database), AWS SES sending,
SES inbound, SQS and S3 in eu-west-1 (Ireland), AWS Bedrock in the EU (AI writing), 46elks
(Sweden), Sentry (region verified before S1; question if outside the EU); staff access in view-as
and that it is logged; no personnummer or special categories in fields; retention per E.7;
deletion at the end of the service; breach notice to the customer; assistance with data-subject
requests (export and delete exist in the app). `DpaVersion` keeps the accepted text.

---
## I. UI

### I.1 Views

App views live under `/flamingo/app/` (namespace `flamingo`), templates under
`templates/flamingo/app/kontakter/` and `templates/flamingo/app/utskick/` (extending
`flamingo/app/base.html`, CSS loaded via `{% block app_head %}` with `?v={{ static_version }}`), CSS
classes from the `flamingo-app.css` catalogue plus part prefixes `.fl-kt-` (kontakter), `.fl-ut-`
(utskick), `.fl-br-` (brev), `.fl-fl-` (flöden). POSTs dispatch on an `action` field mapped through
a dict of handlers (`onboarding._BUSINESS_ACTIONS` pattern).

| View | Path | URL name | Template | JS | Stage |
|---|---|---|---|---|---|
| Kontakter: listan | `kontakter/` | `app_contacts` | `kontakter/list.html` | `flamingo-app-kontakter.js` (select, bulk bar) | S1 |
| Massändring | `kontakter/massandring/` (POST) | `app_contacts_bulk` | | | S1 |
| Lägg till kontakt | `kontakter/ny/` | `app_contact_new` | `kontakter/form.html` | | S1 |
| Kontaktkortet | `kontakter/<pk>/` (`?sida=`) | `app_contact` | `kontakter/detail.html`, `_timeline.html` | | S1 (timeline grows per stage) |
| Redigera | `kontakter/<pk>/andra/` | `app_contact_edit` | `kontakter/form.html` | | S1 |
| Samtycke (dialog) | `kontakter/<pk>/samtycke/` | `app_contact_consent` | in `detail.html` | | S1 |
| Exportera / ta bort kontakt | `kontakter/<pk>/export/`, `kontakter/<pk>/ta-bort/` | `app_contact_export`, `app_contact_delete` | `kontakter/delete.html` | | S1 |
| Exportera alla (POST, bekräftelse) | `kontakter/export/` | `app_contacts_export` | `kontakter/export.html`, CSV | | S1 |
| Rensa inaktiva | `kontakter/rensa/` | `app_contacts_prune` | `kontakter/prune.html` | | S1 |
| Import steg 1 | `kontakter/import/` | `app_import` | `kontakter/import_upload.html` | | S1 |
| Import steg 2 till 4 | `kontakter/import/<pk>/` | `app_import_job` | `kontakter/import_{map,consent,wait,review,done}.html` by status | polls `?status=json` | S1 |
| Importfel | `kontakter/import/<pk>/fel.csv` | `app_import_errors` | CSV | | S1 |
| Listor och taggar | `kontakter/listor/` | `app_lists` | `kontakter/lists.html` | | S1 |
| En lista | `kontakter/listor/<pk>/` | `app_list` | `kontakter/list_detail.html` | | S1 |
| Fält | `kontakter/falt/` | `app_fields` | `kontakter/fields.html` | | S1 |
| Anmälan | `kontakter/anmalan/` (+ `qr.svg`, `qr.png` S4) | `app_signup`, `app_signup_qr` | `kontakter/signup.html` | | S1, S4 |
| Inställningar för kontakter | `kontakter/installningar/` | `app_contacts_settings` | `kontakter/settings.html` | | S1 |
| Biträdesavtal | `kontakter/avtal/` | `app_dpa` | `kontakter/dpa.html` | | S1 |
| Segmentbyggaren | `kontakter/segment/ny/`, `kontakter/segment/<pk>/`, `.../antal/` | `app_segment_new`, `app_segment`, `app_segment_count` | `kontakter/segment.html` | `flamingo-app-segment.js` | S4 |
| Utskick: listan | `utskick/` (`?visa=`, `?sida=`) | `app_utskick_list` | `utskick/list.html` | | S2 |
| Nytt utskick | `utskick/ny/` -> draft | `app_utskick_new` | | | S2 |
| Guide steg 1 till 5 | `utskick/<pk>/steg/<mottagare|kanal|innehall|tid|granska>/` | `app_utskick_step` | `utskick/step_*.html` | `flamingo-app-utskick.js` (counts, sms counter, link picker) | S2 (email content S3) |
| Antal mottagare (JSON) | `utskick/<pk>/antal/` | `app_utskick_count` | JSON | | S2 |
| Sms-förhandsvisning (JSON) | `utskick/<pk>/sms/` | `app_utskick_sms_preview` | JSON | | S2 |
| Länkkontroll (JSON) | `utskick/<pk>/lankkontroll/` | `app_utskick_link_check` | JSON | | S2 |
| Skicka test | `utskick/<pk>/test/` | `app_utskick_test` | POST | | S2 |
| Bekräfta och schemalägg | `utskick/<pk>/skicka/` | `app_utskick_confirm` | POST from `step_review.html` | `<dialog>` confirm | S2 |
| Pausa, fortsätt, avbryt | `utskick/<pk>/lage/` | `app_utskick_state` | POST `action=` | | S2 |
| Rapporten | `utskick/<pk>/` | `app_utskick` | `utskick/report.html` | | S2 (I.8), S3 email, S4 full |
| Mottagare per siffra | `utskick/<pk>/mottagare/?visa=` | `app_utskick_recipients` | `utskick/recipients.html` | | S2 |
| Spara som lista | `utskick/<pk>/mottagare/lista/` (POST) | `app_utskick_save_list` | | | S2 |
| Följ upp, exportera | `utskick/<pk>/folj-upp/`, `utskick/<pk>/export/` (POST) | `app_utskick_follow_up`, `app_utskick_export` | | | S4 |
| E-postredigeraren | `utskick/<pk>/brev/` (+ `spara/`, `rita/`, `bild/`, `kontroller/`, `ai/`, `forhandsvisning/`) | `app_brev`, `app_brev_save`, `app_brev_render_block`, `app_brev_image`, `app_brev_checks`, `app_brev_ai`, `app_brev_preview` | `utskick/brev_editor.html` | `flamingo-pb.js` (profile brev), `flamingo-app-brev.js`, `flamingo-pb-ai.js` | S3 |
| Leveranshälsa | `utskick/halsa/` | `app_utskick_health` | `utskick/health.html` | | S3 |
| Inställningar för utskick | `utskick/installningar/` | `app_utskick_settings` | `utskick/settings.html` | | S2 (rows per stage, I.9) |
| Egen domän | `utskick/installningar/doman/` | `app_utskick_domain` | `utskick/domain.html` | | S3 |
| Spårningsskript | `utskick/installningar/skript/` | `app_utskick_snippet` | `utskick/snippet.html` | | S4 |
| Länkar | `utskick/lankar/`, `lankar/ny/`, `lankar/<pk>/` (S4 as built), `lankar/<pk>/qr.svg`, `qr.png` | `app_links`, `app_link_new`, `app_link`, `app_link_qr` | `utskick/links.html`, `link_form.html`, `link.html` | `flamingo-app-links.js` (Kopiera) | S4 |
| Automatiska flöden | `utskick/floden/`, `floden/ny/`, `floden/<pk>/`, `floden/<pk>/lage/` | `app_flows`, `app_flow_new`, `app_flow`, `app_flow_state` | `utskick/flows.html`, `utskick/flow.html` | `flamingo-app-flows.js` | S5 |
| API-nycklar | `utskick/installningar/api/` | `app_utskick_api` | in `settings.html#api` | | S5 |
| Inkorgen | existing `inkorg/`, `inkorg/<pk>/`, new `inkorg/<pk>/svara/`, `inkorg/<pk>/avregistrera/` | `app_inbox`, `app_lead`, `app_lead_reply`, `app_lead_unsubscribe` | existing + `utskick/_thread.html` | `flamingo-app-utskick.js` (counter) | S2 |
| Skicka sms från kontaktkortet | `kontakter/<pk>/sms/` | `app_contact_sms` | in `detail.html` | | S4 |

Public views (adx.se):

| View | Path | Name | Template | Stage |
|---|---|---|---|---|
| Anmälningssida | `/utskick/<public_slug>/` | `utskick_public:signup` | `utskick/public/signup.html` | S1 |
| Tack | `/utskick/<public_slug>/tack/` | `utskick_public:signup_thanks` | `utskick/public/thanks.html` | S1 |
| Integritet (fallback) | `/utskick/<public_slug>/integritet/` | `utskick_public:privacy` | `utskick/public/privacy.html` | S1 |
| Bekräfta e-post | `/utskick/bekrafta/<token>/` | `utskick_public:confirm` | `utskick/public/confirm.html` (GET button, POST confirms) | S1 |
| Mina utskick | `/utskick/val/<token>/` | `utskick_public:preferences` | `utskick/public/preferences.html` | S1 |
| Sms and mail pages on link hosts | E.1 | `links:*` | `utskick/links/*.html` | S2, S3 |
| DOI mail | | | `utskick/mail/doi.html`, `doi.txt` (inline styles, Brev look) | S1 |

All public pages use `static/css/utskick-public.css` and `X-Robots-Tag: noindex`. The signup and
preference forms on adx.se carry `apps.common.botcheck` plus a honeypot and load
`static/js/utskick-public.js` (only the botcheck proof filler from `site.js:52`, no inline script),
so a real signup carries `bc_proof`; test: a POST with the proof creates a contact, one without gets
the silent fake success. `/utskick/<public_slug>/` returns 404 unless `can_collect(account)` and the
form `is_active`.

Manage views (`manage:` namespace, templates `templates/manage/utskick/`, panel skin: `manage.css`
+ `manage-skin.css` tokens, no inline colours, every table with `<thead>`):

| View | Path | Name | Stage |
|---|---|---|---|
| Card section in the Flamingo panel | `{% utskick_card customer %}` -> `templates/manage/utskick/_customer_card.html` | | S1 |
| Toggle, limits, display name, slug, sending block | `/manage/kunder/<pk>/utskick/` (POST) | `utskick_customer_update` | S1 |
| Avsluta och radera allt | `/manage/kunder/<pk>/utskick/radera/` (typed confirmation) | `utskick_customer_end` | S1 |
| Overview (accounts, queue, heartbeat; S2 inbound, switches, info utskick; S3 health, domains, SES status, DLQ) | `/manage/utskick/` (linked from `/manage/flamingo/`) | `utskick_overview` | S1+ |
| Nödstopp and readiness | `/manage/utskick/nodstopp/` (POST) | `utskick_switch` | S1 |
| Publish DPA version | `/manage/utskick/avtal/` (POST) | `utskick_dpa_publish` | S1 |
| Route held or unroutable inbound | `/manage/utskick/inkommande/<pk>/` (POST) | `utskick_inbound_route` | S2 |
| Approve or refuse link host | `/manage/utskick/vardar/<pk>/` (POST) | `utskick_host_decide` | S2 |
| Information override | `/manage/utskick/utskick/<pk>/undantag/` (POST, reason) | `utskick_info_override` | S2 |
| Release account health block | `/manage/utskick/konto/<pk>/halsa/` (POST) | `utskick_health_release` | S3 |
| Domain help | `/manage/utskick/doman/<pk>/` | `utskick_domain_admin` | S3 |
| Queues and DLQ | `/manage/utskick/koer/` | `utskick_dlq` | S3 |
| Provsms / provmejl till mig (before the switches are on) | `/manage/utskick/prov/` (POST, number or address typed by staff) | `utskick_probe` | S2, S3 |

The card toggle message follows the Flamingo pattern: "Utskick är aktiverat för Exempelrör. Kunden
har inte mejlats." Turning it on requires Flamingo on; the card shows dependencies ("Sms: aktiverat
(Exempelror, ExempelrorS)" or "Sms är inte aktiverat: kanalen sms är spärrad i utskick.") and the
fields display name, public slug, contact limit, email daily cap, "Stoppa all sändning för kunden"
with a reason, pending link hosts, and "Senast exporterad".

### I.1a Sub-navigation

Each part has a tab row (`.fl-tabs`) under the page title; every child route sets `app_active` to
`contacts` or `utskick`.

- Kontakter: Kontakter, Listor (segments join it in S4), Import, Fält, Anmälan, Inställningar.
- Utskick: Utskick, Flöden (S5), Länkar (S4), Leveranshälsa (S3), Inställningar. Tabs appear only
  in the stage that builds them.

Under 560 px the row collapses into `<details class="fl-subnav"><summary>Kontakter: Import</summary>`
with the links as a list (no JS, nothing clipped); QA asserts no tab is clipped at 375 px and
`scrollWidth - innerWidth == 0`.

### I.2 375 px strategy

- **Tables are `.fl-table` with `data-label` on every `<td>`** (empty for button columns), the name
  cell `is-primary`, numbers `is-num`: Flamingo's card mode reads `data-label`
  (`flamingo-app.css` lines 140-146), `manage-tables.js` only runs in `/manage/`. Applies to:
  contacts, utskick list, recipients, per-link table, health events, links, flows, import errors
  preview. DNS records are `.fl-kv` blocks with a left-aligned monospace value and one Kopiera per
  value, not a table. The guard (`test_s1_guards.py`) asserts every `<td` under
  `templates/flamingo/app/{kontakter,utskick}` and the new inbox templates has `data-label`; the
  `<thead>` rule applies to `templates/manage/utskick/`.
- Bulk actions sit in a sticky bottom bar (`.fl-kt-bulkbar`) that wraps to two rows under 560 px.
- Wizards: at 560 px and below the step chips become "Steg 3 av 5: Innehåll" with Tillbaka and
  Nästa; the primary action is full width at the bottom. Import uses the same pattern (Fil,
  Kolumner, Samtycke, Granska).
- Import mapping: one card per file column (name, example, select "Blir").
- Sms composer: textarea full width; counter line wraps ("GSM-7 · 134 av 160 · 1 del · 151 kr").
  The phone preview (mockup 280 px) uses `max-width:100%` and sits below the editor.
- Confirm `<dialog>` width `min(100vw - 32px, 480px)`.
- Email editor on phone: canvas full width at the phone preview width, block panel as a bottom
  sheet; hint "Enklast på dator."
- Report: KPI tiles `fl-kpis` (2 columns under 560), the funnel as stacked bars, the clicks-per-hour
  chart as a server-rendered SVG with `viewBox` and `width:100%`.
- Segment rule rows and flow yes/no branches stack below 760 px.
- App nav: 9 links in S1 and 10 in S2 (the tab row wraps under 900 px). Measured in the S1 and S2
  QA; accepted when nothing is hidden and nothing overflows.
- Every stage's QA follows `webapp/CLAUDE.md`: `preview_start adx`, log in as `claude-qa`,
  `resize_window mobile`, `scrollWidth - innerWidth == 0` on every touched view, and look at the
  page.

### I.3 Swedish copy rules

Plain hyphen and "till" for ranges, time format "09.00 till 20.00", straight quotes, no ellipsis
character, no `[ ]`, no exclamation marks in UI, du-form ("för dig", "din vanliga adress"), numbers
via the `tal` and `kr` filters ("2 418", "151 kr") and a new `procent` filter ("27 %", "4,6 %"), no
time promises anywhere (no "svarar inom", "arbetsdagar", "nästa vardag", "inom 24 timmar"; wait
steps read "Vänta 3 dagar"). Locked options say why and how to unlock, never what ADX will do:
- "Sms är inte aktiverat för dig. Be ADX slå på det."
- "Bara e-post: över 2 000 mejl i månaden kräver egen domän. Verifiera din domän under
  Inställningar."
- "Datum i ett fält: du har inget datumfält än. Skapa fältet Senaste service eller importera det med
  dina kontakter."
- "Omdömen: koppla Google-profilen under Företaget."
- "Lägg till kontakter: godkänn biträdesavtalet först."
- "Avtalssidan saknas. Kontakta ADX."

### I.4 Staff in view-as

Allowed to act for real (decision 2026-10-03). Every action that sends (confirm and schedule, Skicka
nu, resume, test send to a customer contact, inbox reply, contact-card sms, flow activation) adds a
required checkbox "Jag skickar det här som ADX åt Exempelrör." and the button reads "Skicka som
ADX". The row stores `confirmed_by` / `sent_by` and `*_as_staff`; the report and the thread show
"Skickat av ADX (Giovanni) åt kunden". DPA acceptance by staff requires the `staff_statement`. Skicka
nu always opens a `<dialog>` repeating the numbers ("388 sms och 24 mejl skickas nu. Kostnad cirka
151 kr.") for everyone.

### I.5 Pause and wait messages

Customers are never mailed, so the app is the only signal: each pause shows as the status label on
the list row, a banner on the report, and an item in `rules.three_things` ("Utskicket Höstservice
värmepump är pausat vid taket."). "Fortsätt" always re-runs the D.3 pre-checks.

| Reason | List label | Report banner | Buttons |
|---|---|---|---|
| `sms_cost_cap` | Pausat vid taket | "Pausat vid taket: 388 sms kostar cirka 151 kr och 92 kr är kvar av taket 500 kr." | "Höj taket" (when `customer_manages_api`, to `sms:cap_update`) or "Be ADX höja taket" (agency alert, then "ADX har fått din fråga."); "Fortsätt"; "Avbryt utskicket" |
| `adx_mail_cap` | Pausat vid taket | "Pausat: 1 240 av 2 000 mejl från ADX-domänen är skickade i oktober och utskicket behöver 820 till. Verifiera din egen domän under Inställningar, eller skicka till färre mottagare." | "Verifiera domän", "Avbryt utskicket" |
| `bounces` | Pausat: studsar | "4,6 % av de första 500 mejlen studsade. Vi pausar vid 4 %, före AWS gräns på 5 %. ADX har fått ett larm. De studsade adresserna är redan markerade." | "Ta bort studsade och fortsätt", "Avbryt utskicket" |
| `complaints` | Pausat: klagomål | "3 mottagare har markerat mejlet som skräppost. ADX går igenom det innan utskicket kan fortsätta." | "Avbryt utskicket" (staff resumes) |
| `stops` | Pausat: avregistreringar | "9 av 380 mottagare (2,4 %) har avregistrerat sig. ADX går igenom utskicket innan det kan fortsätta." | "Avbryt utskicket" (staff resumes) |
| `account_health` | Pausat: studsar | "E-postutskick är spärrade tills ADX har gått igenom studsarna." | "Avbryt utskicket" |
| `audience_grew` | Pausat | "Mottagarna har blivit fler sedan du bekräftade: 512 i stället för 388. Granska och bekräfta igen." | "Granska igen" |
| `late` | Pausat | "Utskicket skulle ha gått i väg 09.00 i går men kunde inte skickas då. Granska och bekräfta igen om det fortfarande stämmer." | "Granska igen", "Avbryt utskicket" |
| `sms_disabled` | Pausat | "Sms är inte aktiverat för dig. Be ADX slå på det." | "Be ADX slå på sms", "Fortsätt" |
| `email_disabled` | Pausat | "E-post är inte påslaget än." | "Avbryt utskicket" |
| `provider` | Pausat | "Sms-leverantören eller e-posttjänsten svarade med fel. ADX har fått ett larm." | "Fortsätt", "Avbryt utskicket" |
| `account_disabled` | Pausat | "Utskick var avstängt för kontot. Granska och bekräfta igen." | "Granska igen" |
| `blocked` | Pausat | "ADX har stoppat sändningen för kontot. Kontakta ADX." | none |
| `staff` | Pausat av ADX | "ADX har pausat utskicket." plus the staff note | "Avbryt utskicket" |
| `customer` | Pausat | "Du har pausat utskicket." | "Fortsätt", "Avbryt utskicket" |

Waits that are not pauses (status stays `sending` or `scheduled`; shown on the list row and the
report progress line): window ("Fortsätter 09.00 i morgon: utanför tidsfönstret"), email probe
("Väntar på de första svaren från mottagarnas e-postservrar: fortsätter 10.42"), email daily cap
("Fortsätter i morgon: nya konton skickar högst 2 000 mejl per dag de första två veckorna"),
global stop or breaker ("Väntar: sändningen är tillfälligt stoppad av ADX"), low disk (scheduled
stays: "Väntar på ADX").

The cap check also runs in Granska (I.6).

### I.6 Granska (`step_review.html`)

`confirm_summary` stores exactly these numbers.

| Check | Level | Copy (example) | Function | Stage |
|---|---|---|---|---|
| Recipients and skips | info | "388 får sms. 24 hoppas över: 12 utan samtycke, 3 veckotaket, 9 avregistrerade." | `audience.count` | S2 |
| Consent basis (reklam) | tick | "Alla mottagare har samtycke eller är befintliga kunder." | by construction | S2 |
| Opt-out in sms | tick | "Svara STOPP läggs till sist i sms:et." or "Avregistreringslänk läggs till." | `composer` | S2 |
| Company name (reply number) | blocks | "Mottagaren ser bara ett nummer. Skriv Exempelrör i texten." | `checks.sender_identified` | S2 |
| Window | warns | "Inom tidsfönstret 09.00 till 20.00." / "Utanför tidsfönstret: sms:en går i väg 09.00 i morgon." | `timing` | S2 |
| Cost cap | blocks "Nu", warns scheduled | "151 kr av 1 814 kr kvar." / "Ryms inte i taket: 388 sms kostar cirka 151 kr och 92 kr är kvar av taket 500 kr." | `pricing.usage` | S2 |
| GSM-7 | warns | "Texten innehåller tecken som gör sms:et dyrare." with "Byt automatiskt" | `encoding.analyse` | S2 |
| Parts | warns | "6 mottagare får 2 sms-delar (långa namn)." | `composer` | S2 |
| Links answer | warns | "3 av 3 länkar svarar." | `links.check_destinations` | S2 |
| Link hosts | blocks | "Väntar på ADX: länkar till nya webbplatser godkänns av ADX." | E.8 | S2 |
| Information rules | blocks | "Det här ser ut som reklam. Välj Reklam eller ta bort erbjudandet." | H.5 | S2 |
| Reply collision (asks for replies) | warns | "Några mottagare fick nyss sms från en annan ADX-kund via svarsnumret. De får sms:et från Exempelror och kan inte svara." | `checks.sender_for` | S2 |
| Channel ready | blocks | "Sms-utskick är inte påslagna än." / "E-post är inte påslaget än." | D.8 | S2, S3 |
| ADX mail cap | blocks | "Ryms inte i taket: 820 mejl, 760 kvar av 2 000 i oktober." | D.6 | S3 |
| Email checks | per F.5 | F.5 | `email.checks` | S3 |
| Demo | info | "Demokontot skickar aldrig. Utskicket visas som skickat." | | S2 |
| Staff | required checkbox | I.4 | | S2 |

Summary box: "388 sms · 24 mejl" and "Uppskattad kostnad 151 kr. E-post ingår." Buttons "Schemalägg
utskicket" (or "Skicka nu", which opens the dialog) and "Spara utkast".

### I.7 Kontakter

**List** (`kontakter/`):
- Header: "2 418 kontakter · 1 902 kan få sms · 2 131 kan få e-post" ("kan få" = eligible for
  reklam). Buttons Exportera, Importera, Lägg till kontakt.
- Search placeholder "Sök namn, telefon, e-post, regnummer" (`?q=`, `search_text__contains`).
- Filter chips: Lista, Tagg, Samtycke (Ja, Befintlig kund, Företag, Väntar, Saknas, Vill inte,
  Avregistrerad, Studsad), Typ (Person, Företag), Segment (S4).
- Row: name (`is-primary`), sub-line "Privatperson · Regnr ABC 123" or "Företag · Kontakt: Lisa
  Berg" (the `show_in_list` field), consent chips per channel ("Sms: befintlig kund", "E-post
  (företag)", "E-post saknas", "Väntar på bekräftelse", "Avregistrerad (STOPP)", "E-post: studsad"),
  "Senast" ("Klickade i går", "Svarade STOPP 2 okt", "Adressen finns inte, 12 sep", "Importerad 2
  okt").
- Pagination 50 with `?sida=`. Bulk actions (Lägg i lista, Tagga, Ta bort tagg, Exportera, Ta bort)
  take up to 200 checked ids, or "Markera alla 2 418 som matchar", which posts the filter query
  instead of ids (Django's `DATA_UPLOAD_MAX_NUMBER_FIELDS=1000` stays).
- Empty states: without a current DPA "Godkänn biträdesavtalet för att lägga till kontakter." with
  a button; with a DPA and no contacts, links to Importera, Lägg till kontakt and Anmälan.
- When `utskick_daily` has flagged inactive contacts: notice "312 kontakter saknar samtycke och har
  inte hörts av på två år." with "Rensa inaktiva kontakter".

**Contact card** (`kontakter/<pk>/`):
- Header line "Privatperson · kontakt sedan mars 2024 · källa: import".
- Side cards (above the timeline at 900 px and below, consent first): Kontaktvägar with consent
  chips and "Ändra samtycke", Extrafält, Listor och taggar (segment chips from S4), Sammanfattning
  "6 utskick · 2 klick · 1 förfrågan", and from S4 "Svarar oftast på sms, kvällstid" when at least 3
  replies exist (most common channel and Stockholm time band).
- Timeline (`timeline.for_contact`, 30 per page): S1 consent log, imported, signup and lead events
  (lead rows link "Öppna i Inkorgen"); S2 sms sent and delivered, clicked, landing-page visit and
  time on page, reply, STOPP; S3 email delivered, opened (shown as "Öppnade (indikation)"),
  bounced; S4 site visit; S5 flow entered and exited.
- Buttons: Redigera, Exportera, Ta bort, and "Skicka sms" (S4).

**Import** (`kontakter/import/`), step chips Fil, Kolumner, Samtycke, Granska:
- Fil: upload (csv, xlsx, max 10 MB) or paste (max 2 000 rows), "Ladda ner exempelfil".
- Kolumner: one card per column with targets: Fullständigt namn, Förnamn, Efternamn, Mobilnummer
  (+46), E-post, Organisationsnummer (typ Företag), Nytt extrafält: <rubrik> (kind guessed from the
  values), an existing field, Avregistrerad, Hoppa över. Unknown columns default to a new extra
  field; at the 30-field limit they default to Hoppa över with "Du har 30 extrafält. Fler kolumner
  hoppas över." A column that looks like personnummer is forced to Hoppa över: "Personnummer
  sparas inte i Kontakter."
- Samtycke: three choices, none preselected: "De har sagt ja" (checkboxes Sms and E-post, none
  pre-checked, and a required "Var och när?"), "De är befintliga kunder" (same checkboxes and
  field), "Vet inte". Help text: "Gäller privatpersoner och sms till företag. Företag med
  organisationsnummer får e-post utan samtycke, alltid med avregistreringslänk." The choice is
  stored per channel (`ImportJob.consent`).
- Granska: tiles Nya, Uppdateras, Avregistrerade ("Stannar avregistrerade"), Fel (conflicts
  included); "12 företag med privat e-postadress får inte e-post utan samtycke."; "Ladda ner de 22
  felen"; "Lägg alla i lista" (existing or new), "Tagga med"; button "Importera 1 183 kontakter".
- Files and pastes of at most 2 000 rows are converted, analysed and imported inside the request;
  larger files go to the tick and the page polls.

**Anmälan** (`kontakter/anmalan/`, S1): title, intro, channels (E-post; Sms from S2), list, tags,
active switch (created inactive), the public URL with "Kopiera länk", "Förhandsgranska", QR code as
SVG and PNG (S4, `segno`). The public page has separate fields Förnamn (letters, space, hyphen and
apostrophe only, at most 40 characters), Mobil and E-post, and checkbox labels that are the full
consent sentences (so `text_shown` is exactly the label). Errors "Fyll i ett mobilnummer för sms."
and "Fyll i din e-post för e-post." Tack page, the same for every outcome so that it never tells
who already gets offers (S1 security review): "Om a***@e***.example inte redan får erbjudanden från
Exempelrör får du ett mejl dit. Klicka på länken i mejlet för att börja få e-post."; sms in S2 in
the same neutral form. "Förhandsgranska" opens `?forhandsgranska=1`, which also shows a closed page
to the account's users and staff (no submit). The list and tags are added at the DOI confirmation.

### I.8 Utskick list and report

**List** (`utskick/`, `flow_step__isnull=True` everywhere):
- Header line from `pricing.usage` and the email counts: "Oktober: 2 640 sms-delar · 4 120 mejl ·
  kostnad hittills 1 186 kr av taket 3 000 kr (gemensamt med sms-API:t)".
- Chips: Alla, Utkast, Schemalagda, Skickade, Flöden (S5).
- Columns: Utskick (name plus audience line "Lista Kunder · 388"), Kanal, Status, Mottagare, Klick
  (n and %), Svar, Förfrågningar. A flow (S5) is one row "Flöde · 3 steg · Igång" with numbers
  aggregated over its steps.
- Status labels: Utkast, Schemalagt (time), "Skickas: 120 av 388", I.5 labels, Skickat, Avbrutet.
- Pagination 30 with `?sida=`; empty state "Inga utskick än." with "Nytt utskick". The "Automatiska
  flöden" button appears from S5.

**Guide**:
- Mottagare: lists, tags, segments (S4) and single contacts, with excludes and "Fick ett utskick
  senaste 14 dagarna". Box: "Avregistrerade, studsade och (för reklam) kontakter utan samtycke
  räknas aldrig med. 3 hoppas över på grund av veckotaket."
- `app_utskick_count` returns `{total, modes: {sms_only: {sms, skipped}, email_only: {...},
  sms_then_email: {sms, email, skipped}, both: {...}}, skipped_by_reason}`; the Kanal cards show
  "Sms till 388 · e-post till 24 som bara har e-post" and "388 mottagare · 24 hoppas över".
- Kanal: in S2 only "Bara sms" (email modes are hidden until S3). "Bara sms" is locked when sms is
  off: "Sms är inte aktiverat för dig. Be ADX slå på det." Purpose (Reklam, Information with
  `info_reason`).
- Innehåll: tabs Sms and E-post for `sms_then_email` and `both`. Sms composer: placeholders
  `{förnamn}`, `{länk:<key>}` (picker: a Flamingo page from the account's campaigns, or an external
  address under E.8), `{avregistrering}`; live counter on the rendered text for the longest merge
  values ("Längsta namnet ger 2 delar för 6 personer"); `non_gsm` characters listed with a one-click
  fix through `normalize_typography`; max 6 parts; "Mallar" (I.10); "Skriv med AI" (S3). Preview
  "Förhandsvisning med Anna Lindqvist · byt kontakt · skicka test till mig".
- Tid: Nu or a time; outside the window "Utanför tidsfönstret: sms:en går i väg 09.00 i morgon."
- Granska: I.6.

**Report, S2** (`utskick/<pk>/`):
- Header "Sms · skickat tisdag 8 okt 09.00 · 388 mottagare · kostnad 153 kr" (cost = sum of
  `SmsMessage.customer_price`).
- Progress line while sending ("Skickas: 120 av 388 · fortsätter 09.00 i morgon") and the I.5
  banner when paused.
- Tiles: Levererade ("98 % · 6 fel nummer"), Klick ("27 % av levererade", human clicks only), Svar
  ("12 svar · 2 STOPP"), Förfrågningar ("5 · 5,0 kr per förfrågan"; late attributions counted
  separately: "+1 senare").
- Every number links to `app_utskick_recipients?visa=`, including "Hoppades över" by reason:
  `no_consent` "Inget samtycke", `declined` "Vill inte ha erbjudanden", `pending_doi` "Väntar på
  bekräftelse", `suppressed` "Avregistrerad", `bounced` "Studsad adress", `weekly_cap` "Veckotaket",
  `no_address` "Saknar nummer eller e-post", `invalid_number` "Ogiltigt nummer", `country` "Land
  som inte är tillåtet", `duplicate` "Dubblett", `deleted` "Borttagen", `address_changed` "Nytt
  nummer sedan utskicket skapades", `reply_collision` "Fick nyss sms från en annan ADX-kund",
  `recent` "Fick ett utskick nyligen", `ses_suppressed` "Spärrad hos e-posttjänsten",
  `adx_cap` "Taket för ADX-domänen".
- Recipients table columns: Kontakt, Levererat, Klick, Tid på sidan, Svar, Resultat. "Spara som
  lista" on any recipients view (`app_utskick_save_list`, new or existing list).
- Staff line when relevant: "Skickat av ADX (Giovanni) åt kunden". Information utskick show their
  reason ("Information: ändrade öppettider").

S3 email variant: tiles Levererade, Klick, "Öppnat (indikation)" only when open tracking was on,
Studsar, Avregistreringar; kr per förfrågan hidden for email-only sends. S4 full: the funnel
(Skickade, Levererade, Klickade, Stannade 30 s+, Förfrågan), clicks per hour, the per-link table, "På
landningssidan: mediantid 1 min 52 s · 91 % i mobil", "Följ upp de som inte klickade", CSV export.

### I.9 Settings rows

| Page and row | Field | Stage | Editable |
|---|---|---|---|
| Kontakter > Inställningar: Företagsnamn i utskick | `display_name` | S1 | read-only: "Be ADX ändra det." |
| Adress för anmälan | `public_slug` | S1 | read-only, with Kopiera |
| Samtyckestext sms / e-post | `consent_text_*` | S1 | yes (company name filled in; the tracking sentence follows Spåra öppningar) |
| Kryssrutor på landningssidor | `lp_consent` | S1 | yes, with the H.5 prerequisites explained |
| Integritetspolicy | `privacy_url` | S1 | yes, https; empty uses the generated page |
| Text under E-post med erbjudanden | `pref_email_note` | S1 | yes, empty by default |
| Extra text på avregistreringssidan | `unsubscribe_text` | S1 | yes |
| Biträdesavtal | DpaAcceptance | S1 | "Godkänt av Anna Lindqvist 2 okt 2026 · Visa" (the accepted `DpaVersion.text`) |
| Senast exporterad | ExportLog | S1 | read-only |
| Utskick > Inställningar: Svarsnummer | `UTSKICK_REPLY_NUMBER` | S2 | read-only: "0766 86 00 46 · delas av alla ADX-kunder · svar och STOPP hamnar i Inkorgen" |
| Avsändarnamn | `SmsAccount.senders` | S2 | read-only: "ADX godkänner avsändarnamn. Be ADX lägga till ett." |
| Kostnadstak per månad | `SmsAccount.monthly_cap_kr` | S2 | "Höj taket" when `customer_manages_api`, else "Be ADX ändra taket"; usage bar "Oktober 1 186 kr av 3 000 kr" |
| Tidsfönster för sms | `sms_window` | S2 | yes, within 08.00 till 21.00 |
| Tak per kontakt | `weekly_cap_sms`, `weekly_cap_email` | S2 | yes, 1 to 7 per week |
| Meddela mig om svar | `notify_on_reply` | S2 | yes |
| E-post: avsändardomän | SenderDomain | S3 | "Använd din egen domän" (domain flow) or the ADX-domain card "utskick.adx.se · 1 240 av 2 000 mejl i oktober · svar till Inkorgen" (or "svar till hej@exempelror.example") |
| Svar på mejl | `email_reply_mode`, `own_reply_to` | S3 | yes; an own address must be on a verified domain or confirmed through a one-time link |
| Spåra öppningar | `open_tracking` | S3 | yes, off by default: "Av som standard. Öppningar mäts bara hos dem som sagt ja till det när de anmälde sig." |
| Spårningsskript | SiteSnippet | S4 | `utskick/installningar/skript/`: domain, the script tag with Kopiera, status |
| API-nycklar | UtskickApiKey | S5 | yes |

A scheduled utskick that will not fit the cap shows on the settings page too: "Höstservice
värmepump ryms inte i taket."

### I.10 Texts written out

Built-in sms "Mallar" (S2; `D` = `display_name` inserted literally at creation; each passes the
promise, urgency and typography guards in a test):
1. Påminnelse (reklam): "Hej {förnamn|du}, det är dags för service igen. Boka en tid som passar
   dig: {länk:boka} /D"
2. Erbjudande (reklam): "Hej {förnamn|du}, D har ett erbjudande till dig som är kund. Läs mer:
   {länk:erbjudande}"
3. Nya öppettider (information, reason oppettider): "Hej, D har nya öppettider från måndag:
   vardagar 8 till 17. Välkommen."
4. Fråga (reklam, reply number): "Hej {förnamn|du}, vill du att D ringer upp dig om en tid för
   service? Svara JA i så fall."

Flow templates (S5, "Nytt flöde" offers Tomt plus these as drafts):
- Välkommen (trigger new contact from Anmälan, reklam): e-post "Välkommen hos D" with text "Tack för
  att du vill få nyheter och erbjudanden från D. Du kan ändra vad du får eller avregistrera dig
  längst ned i varje mejl." -> Vänta 3 dagar -> e-post "Det här kan D hjälpa dig med" -> mål:
  förfrågan.
- Offert utan svar (trigger API event `offert_skickad`, information, reason arende): Vänta 3 dagar
  -> villkor "har svarat eller skickat förfrågan" -> nej: sms "Hej {förnamn|du}, har du hunnit
  titta på offerten från D? Svara på det här sms:et om du har frågor." -> mål.
- Be om omdöme (trigger list added "Klara jobb", reklam): Vänta 2 timmar -> sms "Tack för att du
  valde D. Vill du berätta hur det gick? Lämna gärna ett omdöme: {länk:omdome}" (the link goes to
  the Google review host on the global allowlist).

### I.11 Other views

- **Leveranshälsa** (S3): the I.5 banner when a pause is active; tiles Studsar 30 d, Klagomål 30 d,
  Studsade adresser ("Får aldrig mejl igen"), Domän ("SPF, DKIM och DMARC OK" or "ADX-domänen");
  event table Adress (masked), Händelse, Vad vi gjorde ("Markerad som studsad", "Avregistrerad
  efter klagomål"), with `data-label`; account block copy "E-postutskick är spärrade tills ADX har
  gått igenom studsarna."
- **Länkar** (S4): rows for each utskick's personal links ("k.adx.se/a8Kf2X + 387 · personliga",
  leading to Rapport) and named links; destination chips Flamingo-sida, Skript finns, Extern;
  actions QR-kod (SVG and PNG), Kopiera, Rapport; the explainer "Två klickdomäner: k.adx.se i sms
  (kort), klick.adx.se i mejl och QR-koder."; "Ny länk" form: label, a Flamingo page or an address
  (E.8), slug.
- **Segmentbyggaren** (S4): rule rows (field, operator, value) with "+ Villkor" and "+ Grupp
  (ELLER)"; months supported ("äldre än 5 månader"); the "öppnade" rule is locked when open
  tracking was never on: "Öppningar spåras inte. Slå på Spåra öppningar under Inställningar."; live
  count "388 kan få sms · 301 kan få e-post"; rows wrap at 375 px.
- **Flöden** (S5): vertical step list with yes/no branches (no free canvas); wait "Vänta 2 timmar" or
  "Vänta 3 dagar" (1 hour to 365 days); a wait ending outside the window moves to the next window
  and the node says "Skickas 09.00 nästa morgon om väntan slutar utanför tidsfönstret."; locked
  trigger "Händelse via API: skapa en API-nyckel under Inställningar först."; branches stack below
  760 px.
- **Startpaket** (S6): a dismissible card "Kom igång med ett välkomstflöde" with "Skapa utkasten",
  which creates the welcome-flow draft and, only if missing, an inactive signup page. Nothing is
  created automatically, including for accounts enabled during S1 to S5.

---
## J. Build plan

General rules for every stage: feature off for every customer until the agency enables it (D2), and
nothing sends until the Switchboard says so (D.8); every AddField follows B.0; `./deploy` runs tests,
ruff and `makemigrations --check`; test command `TEST_DB_NAME=test_utskick SENTRY_DSN= uv run python
manage.py test apps.utskick apps.flamingo apps.sms apps.common --noinput`; 375 px QA before deploy;
manual steps marked **ask** need Giovanni's go-ahead (memory "Fråga innan åtgärd"); AWS and Route53
steps need `aws sso login --profile atlasholly-org`; production shell commands run with
`SENTRY_DSN=` empty. A failed deploy rolls back the code but not the migrations (B.0).

Module layout (grows per stage): `models.py`, `access.py`, `nav.py`, `keys.py`, `limits.py`,
`normalize.py`, `freemail.py`, `consent.py`, `suppression.py`, `contacts.py`, `importer.py`,
`xlsx2csv.py`, `capture.py`, `optin.py` (DOI and confirm sms), `tokens.py`, `timing.py`,
`audience.py`, `composer.py`, `aws.py`, `sending/` (`tick.py`, `freeze.py`, `checks.py`, `sms.py`,
`sms_wrapper.py`, `email.py`, `recover.py`, `health.py`), `links.py`, `link_views.py`,
`attribution.py`, `threads.py`, `inbound/` (`elks.py`, `routing.py`, `stop.py`, `queues.py`,
`email.py`, `events.py`), `email/` (`registry.py`, `blocks.py`, `style.py`, `render.py`, `text.py`,
`mime.py`, `transport.py`, `images.py`, `checks.py`, `domains.py`), `ai.py`, `segments.py`,
`reports.py`, `timeline.py`, `flows/` (`engine.py`, `triggers.py`), `api.py`, `alerts.py`,
`retention.py`, `demo.py`, `app_views/`, `app_urls.py`, `public_views.py`, `public_urls.py`,
`webhook_urls.py`, `manage_views.py`, `manage_urls.py`, `templatetags/utskick_tags.py`,
`management/commands/`, `admin.py`.

### S1: Contacts, import, consent, lists and tags, DPA, signup, preferences, LP checkboxes

Models and migrations: `utskick.0001` (Switchboard, UtskickSettings, DpaVersion, DpaAcceptance,
Contact, Consent, ConsentLog, Suppression, FieldDef, Tag, ContactList, ListMembership, ImportJob,
SignupForm, Event, ExportLog, Counter); `flamingo.0016` (Lead.contact).

Create: the S1 modules above, `app_views/{contacts,imports,lists,fields,signup,settings,dpa}.py`,
`app_urls.py`, `public_views.py`, `public_urls.py`, `manage_views.py`, `manage_urls.py`,
`templatetags/utskick_tags.py`, `management/commands/utskick_tick.py` (phases 1, 3, 8, 9),
`utskick_daily.py`, `demo.py`, `timeline.py` (S1 sources), templates in I.1 marked S1,
`static/css/flamingo-app-kontakter.css`, `static/css/flamingo-app-utskick.css` (shared sub-nav),
`static/css/utskick-public.css`, `static/js/flamingo-app-kontakter.js`,
`static/js/utskick-public.js`, `static/utskick/kontakter-mall.xlsx` (example file, generated once,
fictional rows), `config/test_runner.py`, `server/crontab.d/adx-utskick`,
`server/logrotate.d/adx-utskick`, `server/aws-utskick-role.sh`.

Modify: the S1 parts of C.2 to C.5 (settings and `.env.example`, production key check, test runner,
root and Flamingo URLs, nav, `Lead.contact`, LP form checkboxes
and capture, demo hooks, customer panel include, "Inloggningar hos", manage URLs, `RESERVED_SLUGS`,
robots, analytics skip, Sentry, `monitor_check` heartbeat, static_version, `pyproject.toml` with
openpyxl, defusedxml and boto3, `deploy.sh` flock, `apps/flamingo/README.md`).

Import details:
- Upload max 10 MB (the file goes to disk via the `FILE_UPLOAD_HANDLERS` temp file), CSV with BOM
  and `;` or `,` sniffed, cp1252 fallback (Excel-sv), paste limited to 2 000 rows, max 50 000 rows.
- **Convert once** (`importer.convert`): xlsx is first opened with `zipfile`; refused when the total
  uncompressed size is over 100 MB, any member's compression ratio is over 100, or
  `xl/sharedStrings.xml` is over 30 MB ("Filen går inte att läsa. Spara den som CSV och försök
  igen."). Then a child process `python -I -m apps.utskick.xlsx2csv <in> <out>` with `RLIMIT_AS` 512
  MB and a 60 s timeout streams `openpyxl.load_workbook(read_only=True, data_only=True)` (with
  `defusedxml`) into a normalised UTF-8 CSV in `PRIVATE_MEDIA_ROOT/utskick-import/`. CSV input is
  re-encoded the same way. Chunks then seek with `ImportJob.byte_offset`.
- Column guess by header words (namn, förnamn, efternamn, mobil, telefon, e-post, mail, orgnr,
  företag, personnummer/pnr -> forced skip) and by values (personnummer pattern plus Luhn on most
  values -> forced skip).
- At most 2 000 rows: analysis and import run inside the request. Larger: the tick, chunks of 2 000
  rows, at most 15 s per tick, after the sending phases; the page polls.
- Matching: phone or email; a row that matches two different contacts is a conflict; a row whose
  phone matches but whose email differs from a non-empty stored email (or the reverse) is a conflict,
  never an overwrite (B.1). Suppressed values stay "Avregistrerad". `contact_limit` enforced under
  the contact-limit lock. `can_collect` required before step 1. Agency alert for more than 1 000
  rows with "De har sagt ja" or "befintliga kunder".

DOI in S1 (`optin.py`): sent by tick phase 3 through `email/transport.send(kind="doi")` (the SES API
in eu-west-1 via `utskick.aws.session()`; boto3 imported lazily), From `"Exempelrör"
<bekrafta@utskick.adx.se>`, no Reply-To, templates `utskick/mail/doi.*` with **no submitted text**
(no first name; the company is `display_name`), a privacy link and the confirm link. Limits keyed on
the value hash (`Counter`): 1 per address per account per 24 h, 3 per address across ADX per 24 h,
300 DOI per hour ADX-wide; signups 5 per IP per account per hour. Needs `doi_ready_at` and
`UTSKICK_EMAIL_LIVE`. Demo never sends.

Checklist for the lead (S1), in this order:

1. **Legal page (ask).** Create `/bitradesavtal/` in production via `/manage/` with the H.9 content
   (lawyer-reviewed), link it in a footer column, confirm `lankrapport` stays green. Verify the
   Sentry data region for the sub-processor list.
2. **Keys before code.** Generate `UTSKICK_HASH_KEY` and `UTSKICK_LINK_KEY` (`python -c "import
   secrets; print(secrets.token_urlsafe(48))"`), add both to production `../.env`, store copies in
   the password manager (losing the hash key breaks every suppression match), `systemctl restart
   adx`. Production refuses to start without them, so this comes before the deploy.
3. **AWS (SSO).**
   - `aws iam list-role-policies --role-name django-ec2-instance-role` and `get-role-policy` for
     each; copy anything live but missing into `server/aws-instance-role.sh` and commit.
   - Run `server/aws-utskick-role.sh`: role `adx-utskick` (trust: the instance role with
     `sts:ExternalId`), inline policy `utskick` with the S1 subset of H.8 (send for
     `utskick.adx.se` in eu-west-1, `ses:GetAccount`), and the instance role's separate inline
     policy `utskick-assume`.
   - IMDSv2: `aws ec2 modify-instance-metadata-options --instance-id i-00366e8f91f9ebc65
     --http-tokens required --http-put-response-hop-limit 1`; then check that Bedrock (AI writing)
     and `aws_sync` still work.
   - SES eu-west-1: domain identity `utskick.adx.se` with Easy DKIM (RSA 2048), the 3 CNAMEs in
     Route53 zone `Z2HKAATG1V4QEA`; custom MAIL FROM `bounce.utskick.adx.se` (MX 10
     `feedback-smtp.eu-west-1.amazonses.com`, TXT `v=spf1 include:amazonses.com -all`);
     `_dmarc.utskick.adx.se` TXT `v=DMARC1; p=quarantine; adkim=r; aspf=r`.
   - **Request production access in eu-west-1** (a new region starts in the sandbox; allow days).
     Describe the double opt-in, the per-customer suppression list, bounce and complaint handling.
   - `.env`: `UTSKICK_AWS_ROLE_ARN`, `UTSKICK_AWS_EXTERNAL_ID`; restart.
4. **Deploy S1** (`./deploy`).
5. **Cron and logs** (as ubuntu): `mkdir -p /home/djangouser/sites/adx/run` owned by djangouser,
   install the lines from `server/crontab.d/adx-utskick` into djangouser's crontab, copy
   `server/logrotate.d/adx-utskick` to `/etc/logrotate.d/`. Check the heartbeat on `/manage/utskick/`
   and that `monitor_check` stays quiet. Measure a busy tick's `VmPeak` and set
   `UTSKICK_TICK_MAX_MB` to it plus 30%.
6. **DOI live**, once production access is granted: `UTSKICK_EMAIL_LIVE=true` in `.env`, restart;
   sign up a test inbox on the internal test customer (a real customer record for ADX itself with
   Flamingo and utskick enabled, never the demo), check `dkim=pass` with `d=utskick.adx.se` in
   the message source, confirm; then set `doi_ready_at` on `/manage/utskick/nodstopp/`.
7. **Publish the DPA version** on `/manage/utskick/avtal/` (snapshots the page text).
8. Enable utskick for the demo customer only, run QA, leave every real customer off.

Tests (new files are scanned by the typography and promise guards: write `chr(0x2013)` instead of
the character, never the promise phrases):
- `test_s1_models.py`: constraints, hash stability, normalisation (Swedish mobile, 0046, landline to
  field, org number legal vs personnummer rejected, personnummer in a field refused).
- `test_s1_consent.py`: transition table, import cannot flip or lift, `declined`, `company`
  derivation (freemail refused, only from missing), value-hash binding and `change_address`,
  eligibility per purpose, ConsentLog append-only.
- `test_s1_import.py`: CSV sv/en, BOM, cp1252, xlsx, zip-bomb refusal, child-process conversion,
  in-request at 2 000 rows, 50 000-row chunking with `byte_offset`, mapping guesses, personnummer
  column forced to skip, conflicts (no overwrite), errors CSV, file and sample cleanup, contact limit,
  DPA gate, per-channel consent choice.
- `test_s1_views.py`: every route 404 for another account and when disabled; foreign ids in bodies
  give 400 (`owned_ids` parametrised); staff view-as works and is logged; nav shows Kontakter only
  when enabled (extend `test_core.py` near line 477); export is POST, logged and limited; bulk
  "Markera alla som matchar"; timeline shows only the account's leads.
- `test_s1_public.py`: signup 404 without `can_collect` or when inactive; botcheck with
  `utskick-public.js` proof; honeypot; Counter limits (IP, address per account, address across ADX,
  hourly cap); first-name rules; DOI carries no submitted text; DOI GET does not confirm, POST does,
  expiry; preference page semantics (toggle off = declined, on = confirmation); privacy page; no
  analytics cookies.
- `test_s1_lp.py`: checkbox texts exact, never pre-checked, absent when off, without DPA, without a
  privacy notice or without `doi_ready_at` (email); field errors; capture gating (no Contact for a
  plain lead without consent; link only on exact match); `Lead.contact`; no cookies (extend
  `test_security.py:828`).
- `test_s1_tick.py`: flock-free advisory lock path, fingerprint mismatch refuses writes and sends,
  heartbeat, `monitor_check` alert when stale with work.
- `test_s1_guards.py`: no `style=` or inline `<script>` in `templates/flamingo/app/kontakter`,
  `templates/flamingo/app/utskick`, `templates/utskick/public`; every `<td` in the Flamingo folders
  has `data-label`; every table in `templates/manage/utskick/` has `<thead>`; imports from
  `django.core.mail` only in `apps/utskick/alerts.py`, SES `send_email` only in
  `apps/utskick/email/transport.py`, stdlib `email.message` allowed in `email/mime.py`; migration
  rule (B.0); production refuses missing keys; the no-network runner is active.
- Extend: `test_demo.py` `counts()` and the route list (line 403) with `flamingo:app_contacts`,
  `app_contact` per contact, `app_lists`, `app_import`, `app_signup`, `app_contacts_settings`;
  `utskick.demo.seed` creates `UtskickSettings` enabled and `dpa_ok` is true for the demo;
  `demo.reset` deletes Suppression and ConsentLog rows; `_flamingo_routes` also walks
  `manage:utskick_*`; `test_inbox.py:315` folders `app/kontakter`, `app/utskick`;
  `apps/common/test_sentry.py` patterns; MCP forbidden names.

Acceptance: agency toggles utskick on the demo; DPA accepted (customer and staff-with-statement
paths); a 1 000-row Excel import with "Vet inte" gives correct counts and no reklam eligibility; a
suppressed row in the file stays avregistrerad; the signup page creates an email `pending`, the DOI
mail arrives with DKIM pass and confirming works; the LP form shows the checkboxes with exact texts
and stores proof; GDPR export and delete work (including the erasure suppression); nothing appears
for customers with utskick off; 375 px zero overflow on every new view and the 9-link nav.

Deploy notes: migrations are additive (B.0); the tick runs with nothing to do (exits after one
query); no link hosts yet.

### S2: Sms utskick, k.adx.se, click tracking, LP attribution, replies and STOPP, basic report

Models and migrations: `utskick.0002` (Utskick without email columns, Recipient, AllowedHost,
TrackedLink, LinkCode, Click, Thread, ThreadMessage, InboundMessage, FKs from
ConsentLog/Suppression/Event to Utskick/Recipient); `flamingo.0017` (Lead utskick FKs, attribution,
activity_at with backfill, source reply); `sms.0006` (source, sender 16, indexes,
MonthlyStatement.by_source), all per B.0.

Create: `audience.py`, `composer.py`, `timing.py`, `sending/*` (sms parts), `links.py`,
`link_views.py`, `config/urls_links.py`, `attribution.py`, `threads.py`,
`inbound/{elks,routing,stop}.py`, `webhook_urls.py`, `reports.py` (S2), `alerts.py` additions,
`app_views/{utskick,inbox_reply}.py`, templates (list, steps, review, report, recipients,
`_thread.html`, `links/*`), `static/js/flamingo-app-utskick.js`, `apps/sms/hooks.py`.

Modify: C.1 in full (apps/sms), the S2 items of C.2 (leads, limits, public views, beacon,
`flamingo-lp.js`, inbox, owner texts, overview, `three_things`, `Lead` fields, demo,
FlamingoGate early return), C.3 (link host middleware, webhook URLs, ASGI host check, analytics host
skip, Sentry) and C.5 (link hosts, access logs, certificate lineage).

Checklist for the lead (S2), in this order:

1. Route53: `k.adx.se` and `klick.adx.se` CNAME `adx.se` (the box has no Elastic IP).
2. Production `.env`: append `,k.adx.se,klick.adx.se` to `ALLOWED_HOSTS` and
   `,https://k.adx.se,https://klick.adx.se` to `CSRF_TRUSTED_ORIGINS` (at the end); set
   `UTSKICK_LINK_HOSTS`, `UTSKICK_REPLY_NUMBER`, `UTSKICK_ELKS_INBOUND_TOKEN`, and
   `SMS_DLR_ALLOWED_IPS` (46elks' IPs) if empty; `systemctl restart adx`; run `lankrapport`.
3. Deploy S2 (`Switchboard.sms_enabled` stays off: nothing can send).
4. As `ubuntu`: `server/nginx-only.sh adx` (renders the port-80 link block only, because
   `/etc/letsencrypt/live/adx-links/` does not exist yet), `server/certs.sh adx` (lineage
   `adx-links`), `server/nginx-only.sh adx` again (now with the 443 block).
5. Curls: `curl -sI https://k.adx.se/robots.txt` and `https://klick.adx.se/` (200, noindex, no
   `Set-Cookie`), `https://k.adx.se/mcp`, `/manage/`, `/flamingo/app/` (404). Set `links_ready_at`
   with the note.
6. **46elks (ask Giovanni)**: confirm `+46766860046` is on ADX's 46elks account and can send with
   `from=+46766860046`, then set the number's `sms_url` to
   `https://adx.se/api/utskick/46elks/inkommande/<token>/` in the 46elks dashboard. With "Provsms
   till mig" on `/manage/utskick/nodstopp/` (to a number the staff member types; works before
   `sms_enabled`; sent from the reply number, billed to the internal test customer, source `test`): a reply from the phone lands in that customer's Inkorg; "Stopp"
   suppresses and the confirmation arrives; "Start" gives a confirm link that works; the webhook
   response body is empty (no echo sms); the reconcile finds nothing missing. Set
   `sms_inbound_ready_at`.
7. Real-device check: `k.adx.se/xxxxxx` is auto-linked in iOS Messages and Google Messages; the
   iMessage preview counts as a bot hit, not a click.
8. **Content (ask; lawyer)**: update the integritetspolicy (link tracking via k.adx.se and
   klick.adx.se, URL token on landing pages, retention periods from E.7, replies, the shared number)
   and `/sms-villkor/` (STOPP and START, replies to the shared number, utskick share the cost cap,
   the suppression endpoint for API customers). Update memory note `flamingo-utskick`.
9. **Turn on sending**: `sms_enabled` on `/manage/utskick/nodstopp/`.

Tests:
- `apps/sms/tests.py` additions: `send()` behaviour unchanged except the 429 mapping (REJECTED
  `rate_limited`, 429 to the caller, no alert); `send_for_account` source stored; reply number only
  with `allow_reply_number`; sender 16 chars; headroom from settings, clamped; a requeued recipient
  after a 429 reaches FakeElks; hook called on delivery reports; `message_detail` 404 for utskick
  rows; suppression endpoint; statement `by_source`; portal labels; `LayoutGuardTests` green.
- `test_s2_tick.py` (`TransactionTestCase` where locks matter, `FakeElks`): freeze chunks,
  atomicity, code-collision redraw, idempotency, tampered audience yields no foreign recipient;
  claim with skip_locked; while-loop throughput reaches 45 per account per minute; deferring vs
  per-person checks (pause mid-batch then resume: every recipient sent once; window edge; Switchboard
  off); crash after reserve -> adoption by the constraint predicate only, never a second sms;
  circuit breaker; rate_limited requeue; cap -> all the account's utskick paused, one BLOCKED_CAP row,
  resume after raise reuses the reference; quiet hours incl. holidays and DST days; weekly cap;
  consent withdrawn or address changed between freeze and send; reply collision with and without a
  name sender; late start pauses; account disabled pauses everything and STOPP still works; demo
  simulation with no network and `assert_not_demo` in the wrapper; delivery hook moves recipients
  forward only; heartbeat.
- `test_s2_links.py`: host router (`HTTP_HOST="k.adx.se"`), only `urls_links` answers (no
  `/manage/`, no `/flamingo/app/` on k.adx.se), 302 headers and referrer policy only on redirects,
  HEAD, bots counted not stored, 20-rows cap, miss limit, codes GSM-7 (`encoding.analyse(body).encoding
  == "gsm7"`), `ut` attribution of form leads and call clicks (also on a paused campaign), foreign
  `ut` ignored, forwarded link creates a separate contact, raised lead limits, no cookies;
  `/s/`, Ångra (30 minutes, single use), `/p/` and `/b/` with `Client(enforce_csrf_checks=True)`,
  `HTTP_ORIGIN="null"`, no Referer: they work with the form nonce and set no cookie; external link
  rules and AllowedHost flow.
- `test_s2_inbound.py`: token, IP list required in production, empty body, atomic handler (an
  exception returns 500 and nothing is stored), idempotent id, reconcile inserts a missing id,
  routing to the single candidate, ambiguous held, unroutable, the STOPP phrase table (G.1), STOPP
  across several accounts, START confirm flow, confirmation once per 24 h and cap pre-check, loop
  guard, reply without a current DPA creates a thread without a Contact, batched owner notice.
- `test_s2_inbox.py`: reply lead in list and badge, one chip row with counts, status `<select>`,
  thread view, "Klar" label, reply send with counter and the appended name, staff checkbox
  required, demo refuses, `activity_at` ordering and backfill, `channel()` labels, overview
  exclusion, `can_send_to_google` false with utskick, "Avregistrera från sms" writes Suppression and
  ConsentLog with the acting user.
- `test_s2_info.py`: information rules block, staff override logged, alerts at 200 recipients and
  more than 2 per 30 days, opt-out line on information sms.
- Extend guards: `test_demo.py` routes (`app_utskick_list`, `app_utskick` per utskick, steps), patch
  `apps.sms.elks._post` to fail if called during the demo walk; `test_inbox.py` folders; no
  `{% csrf_token %}` in `templates/utskick/links/`.

Acceptance: a 400-recipient sms utskick on the internal test customer with real 46elks in
production paces at 45 per minute or less, leaves the customer's API working, appears on the sms
statement as utskick, shows delivered and clicks in the report; a click from a phone lands on the LP
with `ut` and the address bar loses it; a form lead shows "Utskick: Höstservice värmepump" in the
Inkorg and is not in Google cost-per-deal; a reply and a STOPP behave as G.1; pause at cap and resume
work; 375 px clean on list, steps, review, report, recipients, inbox thread and the 10-link nav.

### S3: Email in Brev, SES API, domains, delivery health, email replies, one-click unsubscribe

Models and migrations: `utskick.0003` (Utskick email columns with `db_default`, SenderDomain,
EmailImage, EventReceipt).

Create: `email/*`, `inbound/{queues,email,events}.py`, `sending/email.py`, `sending/health.py`,
`ai.py`, `app_views/{brev,health,domain}.py`, brev templates, `static/css/flamingo-app-brev.css`,
`static/js/flamingo-app-brev.js`.

Modify: `flamingo-pb.js` and `pages.editor_config` (profile, F.6), `pagebuilder/blocks.py` and
`media.py` (`types=` parameter, delete protection, C.2), settings and `.env.example` (C.4),
`server/aws-utskick-role.sh` (S3 permissions).

Domain flow: the customer enters a domain and the sender address; the claim rules of B.3 run; the
app calls `CreateEmailIdentity` (Easy DKIM, eu-west-1, `ses_created=True`) and
`PutEmailIdentityMailFromAttributes(MailFromDomain="studs.<domain>",
BehaviorOnMxFailure="USE_DEFAULT_VALUE")`, shows the records as `.fl-kv` blocks (3 DKIM CNAMEs, MX
and SPF TXT for `studs`, DMARC TXT recommendation `v=DMARC1; p=none` if none exists) with Kopiera
buttons and "Be ADX om hjälp" (agency alert, no mail to the customer). "Kontrollera igen" (1 per
minute) and `utskick_daily` (14 days) check with dnspython and `GetEmailIdentity`; verified ->
usable as sender (probe applies, D.9); 14 days without verification -> `expired` and an agency
alert. Removing a domain deletes the identity only when `ses_created`.

Checklist for the lead (S3), in this order:

1. IAM: `aws sesv2 list-email-identities` in eu-north-1 and eu-west-1; extend the `utskick` policy
   per H.8 (identity create/get/put/delete, the explicit Deny for `adx.se`, `utskick.adx.se`,
   `svar.utskick.adx.se` and every listed identity, SQS, S3 `in/*`) with
   `server/aws-utskick-role.sh`.
2. SES eu-west-1 events: configuration set `adx-utskick` (reputation metrics on,
   `SuppressionOptions.SuppressedReasons=["BOUNCE"]`); SNS topic `adx-utskick-events`; SQS queue
   `adx-utskick-events` with DLQ `adx-utskick-events-dlq` (maxReceiveCount 5, retention 14 days) and
   a queue policy allowing that topic; subscription with `RawMessageDelivery=true`; event
   destination (SEND, REJECT, BOUNCE, COMPLAINT, DELIVERY, DELIVERY_DELAY, RENDERING_FAILURE, OPEN).
3. SES eu-west-1 receiving: verify `svar.utskick.adx.se`; MX `svar.utskick.adx.se` 10
   `inbound-smtp.eu-west-1.amazonaws.com`; private S3 bucket (eu-west-1, SSE-S3, lifecycle 7 days on
   `in/`, bucket policy for `ses.amazonaws.com` with `aws:SourceAccount` and the receipt rule's
   `aws:SourceArn`); SNS topic `adx-utskick-inbound` -> SQS `adx-utskick-inbound` + DLQ; receipt
   rule set (check whether one is already active in eu-west-1; only one can be) with a rule for the
   domain, scanning on, S3 action (prefix `in/`) with the topic.
4. `.env`: `UTSKICK_SQS_EVENTS_URL`, `UTSKICK_SQS_INBOUND_URL`, `UTSKICK_SES_INBOUND_BUCKET`;
   `systemctl restart adx`.
5. Deploy S3 (`email_enabled` stays off).
6. Quotas: `aws sesv2 get-account --region eu-west-1` must show `ProductionAccessEnabled=true`;
   request a higher daily quota and rate if below about 50 000 per day and 14 per second; run
   `utskick_daily --only ses` to fill `ses_max_rate`.
7. Live checks with "Provmejl till mig" on `/manage/utskick/nodstopp/` (works before
   `email_enabled`): Gmail shows "Avsluta prenumerationen" and it works; the message source has
   `dkim=pass` for `utskick.adx.se` and the `DKIM-Signature h=` list contains `list-unsubscribe`
   and `list-unsubscribe-post`; a reply lands in the Inkorg; a bounce to
   `bounce@simulator.amazonses.com` and a complaint to `complaint@simulator.amazonses.com` update the
   test contact; both DLQs are empty. Set `email_ready_at`.
8. **Turn on sending**: `email_enabled` on `/manage/utskick/nodstopp/`.

Tests: `test_s3_registry.py` (24 elements, kinds, url rules, no merge tags in URLs, locks for
information, requires), `test_s3_render.py` (tables, 560/28/20/30 and the `.t-brev` tokens parsed
from the mockup, inline styles only, absolute image URLs, mso wrapper, text version, merge
fallbacks and single-line merge, per-recipient links, 102 kB check, accent palette light and dark,
pixel only for `tracking_ok`, web-view CSP), `test_s3_transport.py` (FakeSes: raw MIME headers,
List-Unsubscribe and Post, configuration set, tags, `max_attempts=1`, throttling requeue, timeout
and 5xx -> unknown never resent, AccountSendingPaused, unknown adoption, demo refused),
`test_s3_queues.py` (receipt and effects atomic, duplicate receipt, failure leaves the message, DLQ
alert), `test_s3_events.py` (hard, soft x5, complaint, `OnAccountSuppressionList`, thresholds,
probe hold, daily cap, account block and release), `test_s3_inbound_email.py` (bucket pin, token
from receipt recipients first, unknown token never fetches, autoreply, spam, size, quote stripping,
mailto unsubscribe with and without the recipient row, hourly cap), `test_s3_domains.py`
(dnspython mocked; adx.se, freemail, public suffix, parent/child and AlreadyExists refusals,
`ses_created` delete rule, expiry, uniqueness among verified), `test_s3_caps.py` (ADX 2 000 cap
incl. tests), `test_s3_one_click.py` (POST without CSRF unsubscribes, GET does not, works after the
recipient row is gone), `test_s3_media.py` (delete blocked by a draft, allowed after send, rendition
survives), `test_s3_link_check.py` (analyzer.fetch used, private IPs refused, 15 s budget, hourly
limit), `test_pagebuilder_editor.py` (page profile config unchanged). Extend `test_demo.py` to patch
the transport client to fail if called.

Acceptance: a Brev mail with all 24 elements renders correctly in Gmail web and app, Apple Mail
(light and dark), Outlook 365 web and classic Outlook (manual check with real inboxes, screenshots
in the stage notes); a 2 000-recipient email on the ADX domain respects the probe, the cap and the
rate; a customer domain verifies end to end; bounces and complaints pause at the thresholds; replies
arrive; 375 px clean on the editor, health, domain and settings.

### S4: Segments, full report, own-site snippet, links view, contact-card sms

Models: `utskick.0004` (Segment, SiteSnippet, TrackedLink slug index). Create: `segments.py` (rules
-> `Q` with `Exists` subqueries starting from the account's contacts, ids through `owned_ids`, live
count endpoint), timeline sources `site_visit` and "Svarar oftast", `reports.py` full (I.8),
`link_views.snippet` and `snippet_beacon`, `static/utskick/s.js` (versioned, SRI),
`app_views/{segments,links,snippet}.py`, named links with QR (`segno` SVG and PNG), contact-card sms
(`app_contact_sms`, all send-time checks), `pyproject.toml` with segno. Lead checklist: privacy
policy line for the own-site snippet (cookieless, the `adx` parameter). Tests: segment compiler per
rule and nesting, months, locked "öppnade", counts match freeze, foreign ids refused; timeline
privacy (no other account rows); snippet Origin check, SRI file name, no cookies, token from another
account refused, `adx=` only after `last_seen_at`; named-link slug uniqueness and E.8 rules;
follow-up segment content; report numbers against a seeded utskick. Acceptance: the segment "Service
i höst" (field Senaste service older than 5 months AND list Kunder AND no lead in 30 days) counts
live while editing; report numbers click through to recipient lists; a snippet on a test site
reports a visit from a klick link.

### S5: Automated flows and API events

Models: `utskick.0006` (`0005` is S4's `OldPublicSlug`; Flow, FlowStep, FlowRun, UtskickApiKey, Recipient.flow_run with
`db_default` null, the flow-run recipient constraint, Event.idempotency_key constraint). Create:
`flows/engine.py` (tick phase 7: wake due runs with skip_locked, execute one step per run per tick;
send steps create recipients with `flow_run` on the step's hidden Utskick and reuse the sms/email
loops and every check; wait steps set `wake_at` in hours or days, moved into the window for send
steps; condition `clicked_or_lead` since run start; tag add/remove; goal ends as result),
`flows/triggers.py` (list_added and new_contact from `contacts`/`capture` hooks called explicitly by
the writers, no signals; date_field and fixed_dates evaluated hourly at the configured hour with
cycle keys; api_event from `api.py`), `api.py` (`POST /api/utskick/v1/events/` with `Authorization:
Bearer adxut_...`, body `{event, phone|email|contact_id, data, occurred_at, idempotency_key,
consent?}`; `contact_id` through `owned_ids`; 403 `dpa_required` without `can_collect`; consent
follows the import rule: `missing -> yes|existing` only, accepted only with `text_shown` and
`collected_at`), flow builder views and `flamingo-app-flows.js`, the three flow templates (I.10).
Lead checklist: none beyond deploy. Tests: engine idempotency per cycle, re-entry and second cycle
(new recipient rows, no IntegrityError), hidden utskick never listed and never finished, wait in
hours and across DST, condition branches, exits on STOPP or unsubscribe, flows respect quiet hours,
weekly cap and suppression, account disable pauses flows, API auth, idempotency, rate limit (60
events per minute per key), demo never sends, flow labels and templates pass the promise guard.
Acceptance: "Service varje säsong" runs on seeded contacts with simulated time in a test; "Offert
utan svar" triggers from an API event.

### S6: The "Senare" items

- Utskick to flow: "När något händer" in the Tid step converts a draft into a flow with trigger
  list_added (`Utskick.converted_to_flow`).
- Startpaket: the dismissible card of I.11; drafts only, created on click.
- Per-step stats on the flow map (started, clicked after step, waiting) and test mode: "Testkör"
  runs the flow on a test contact with simulated time, `FlowRun.is_test`, no provider calls, results
  in seconds.
- Click map for email: clicks drawn over the rendered mail by `TrackedLink.block_id`/`position`,
  plus clicks per mailbox provider grouped by recipient domain (gmail.com, outlook/hotmail,
  telia.com, others). Tests: test mode never calls 46elks or SES; conversion keeps content and
  audience; click map counts equal report counts.

---

## K. Open risks and questions

### K.1 Risks

1. **Box capacity.** A tick starts every minute (about 1 to 2 s CPU, 80 to 120 MB) on a 2 GB
   t3.small without swap, shared by 8 sites. `flock -n` prevents overlapping processes, `choom`
   makes the tick the OOM killer's first choice, `RLIMIT_AS` caps it, and the tick exits after one
   EXISTS when idle. Disk is about 76% full; retention (E.7), counters instead of bot rows, the
   free-space alert at 15% and the freeze stop at 8% cover growth. If either becomes tight, the fix
   is a bigger instance or moving Postgres, not code.
2. **Shared box user.** Every site runs as `djangouser`; the dedicated AWS role and IMDSv2 limit
   exposure through the metadata service and SSRF, not through local code execution on the box.
3. **Shared reputations.** All customers share the eu-west-1 SES account and `utskick.adx.se` (cap 2
   000 per month, probe, daily ramp, pauses at 4% / 0.08%), and one 46elks account and one reply
   number (100 sms per minute: utskick at most 60 of the 80 that apps/sms allows; the stops health
   check protects the number). ADX's own mail in eu-north-1 is isolated from all of it.
4. **eu-west-1 production access** is a separate SES request with days of lead time; until it is
   granted, DOI and email stay off (`doi_ready_at`).
5. **Reply routing by latest outbound** (D4): collisions within 14 days are avoided at send time,
   STOPP applies to every recent sender, and ambiguous free text is held for the agency; a reply
   older than 30 days after two customers' sends can still reach the later one.
6. **Agency workload**: link-host approvals, held replies, information-utskick alerts, health
   releases and DLQ checks land on the agency. All are listed on `/manage/utskick/`.
7. **Link previews and scanners** inflate clicks; heuristics (E.3) reduce but cannot remove them.
   Apple Mail Privacy Protection makes opens meaningless, which is why opens are an "indikation",
   off by default and limited to `tracking_ok` recipients.
8. **Short sms codes** can be guessed; masked pages, the miss limit, the 30-minute Ångra and
   confirm-before-opt-in contain the damage.
9. **46elks inbound** retry behaviour is not documented to us; the 10-minute reconcile bounds a lost
   STOPP to 10 minutes.
10. **Gmail one-click** depends on SES DKIM covering the List-Unsubscribe headers; verified in the
    S3 checklist.
11. **HSTS preload** means a certificate mistake on `k.adx.se` breaks every sent link; the separate
    `adx-links` lineage keeps adx.se's own renewal independent, and `links_ready_at` gates sending.
12. **Guard false positives** on customer copy: the AI guard only warns on customer text; the
    information-content block has a staff override.
13. **flamingo-pb.js profile refactor** touches the page builder; the page profile test and
    `test_pagebuilder_editor.py` must stay green.
14. **The no-network test runner** may expose existing tests that touch the network; they are fixed
    in the S1 commit.
15. **Legal texts** (DPA, privacy policy, sms terms, the generated privacy fallback) need a lawyer's
    review before S1 and S2 go live for a real customer.

### K.2 Questions that need Giovanni

1. **46elks number.** Is `+46766860046` rented on ADX's 46elks account, and may we point its
   `sms_url` at `https://adx.se/api/utskick/46elks/inkommande/<token>/` when S2 is deployed?
   **Answered (Giovanni, after revision 2): yes, after S2 is deployed, as a manual step.**
2. **DPA on the customer's behalf.** May the agency accept the biträdesavtal in view-as with a
   recorded statement ("Godkänt av Anna Lindqvist per mejl 2 okt"), or must a customer contact click
   it themselves? The plan allows staff with the statement.
3. **Retention.** Confirm E.7 (clicks and recipients 13 months, events 25 months, consent proof 36
   months after deletion) before the S2 privacy text is written; how long reply threads and reply
   leads are kept (plan: as long as the inbox lead exists); and whether data is deleted
   automatically some months after utskick is turned off for a customer (plan: no, only the staff
   action "Avsluta utskick och radera allt").
4. **Own click domain** (`klick.<kundens domän>`) is not in S1 to S6: it needs a certificate per
   customer domain on the shared box. Leave it out until pricing is decided?
5. **Sms consent from forms.** The approved mockup says "Sms bekräftas inte separat". The plan makes
   signup-page sms consent `pending` until the person clicks a link in a confirmation sms (from S2;
   costs one sms, billed to the customer), because anyone can type someone else's number. For the
   landing-page checkbox the plan keeps a direct `yes` (the person sent an inquiry with that number
   to the company). OK, or should the LP checkbox also confirm by sms?
6. **GDPR delete and sms rows.** The sms-api decision keeps sms numbers and texts forever. The plan
   blanks `to` (keeping country) and `body` of utskick, flow, reply and test sms when a contact is
   erased, keeping parts, prices and reference for billing; API sms are untouched. OK?
7. **Yearly sms fee.** Enabling sms on the card starts the 999 kr yearly fee
   (`service_year_start`). Should a customer who uses sms only through Utskick pay it? If not, the
   card sets `yearly_fee_kr=0` when sms is enabled for utskick only (test included).
   **Answered (Giovanni, after revision 2): the fee applies to them too; no change in code.**

---

## L. Review decisions

Where the plan deviates from a reviewer's proposed fix, with the reason:

- **Ops 3 (46elks 429)**: the RESERVED row becomes `REJECTED` with `rate_limited` instead of being
  deleted: a stopped status releases the reference and costs 0 just the same, and sms rows are kept
  forever by decision.
- **Ops 9 / Security 27 (SNS delivery)**: took the reviewer's preferred alternative, SQS polled by
  the tick, for events and inbound alike, which removes the SNS HTTPS endpoint, its signature code,
  `UTSKICK_SNS_TOKEN` and the 2 000 web requests per 2 000-mail send.
- **Ops 8 (SES isolation)**: utskick mail, DOI included, moves to eu-west-1 from S1 (not S3), so
  production access is requested early and DOI never touches ADX's eu-north-1 reputation.
- **Ops 14 (`RLIMIT_AS` 400 MB)**: the limit is a setting (default 700 MB, set from a measured peak
  in S1): a normal Django process's address space can exceed 400 MB without using that memory, and
  `flock` plus `choom` are the real protection.
- **Security 7 (tokens embed account, channel and hash)**: done for the HTTPS unsubscribe and
  preference links; the mailto and Reply-To tokens stay short (64-octet local part) and fall back to
  the account in the token plus the sender's address when the recipient row is gone.
- **Security 8a (reply collision)**: no requeue to "other send + 14 days" (a two-week delay breaks
  dated offers); the recipient gets the name sender, or is skipped `reply_collision` when the
  account has none.
- **Security 8d (company name in every reply-number sms)**: inbox replies get " /Exempelrör"
  appended instead of being blocked (a person in a conversation should not be refused an answer).
- **Security 9 (STOPP words)**: "STOPP" or "STOP" first auto-stops at any length (the reviewer's
  own example has 10 words); the other words keep the 8-word limit, and a second word INTE never
  stops.
- **Security 12 / Product 19 (/p/ details)**: no first name on `/s/` and `/p/` (security wins over
  the "first name only" wording).
- **Security 13 (signup sms confirmation)**: the signup page offers sms only from S2, when the
  confirm link host exists; in S1 it is email only. The LP checkbox keeps a direct `yes` pending
  K.2.5.
- **Security 35 (account-level suppression)**: in addition to the recipient-only mapping, the
  configuration set suppresses only BOUNCE, so complaints never cross customers in the first place.
- **Ops 25 (sms delivered status)**: a callback hook from `apps.sms` instead of a read-time join, so
  reports, segments and the timeline read one status column.
- **Ops 34 / Security 13 (demo DPA)**: `dpa_ok` is true for the demo instead of seeding a
  `DpaVersion`, so a fresh development database renders the demo without legal content.
- **Product 3 (sub-navigation under 560 px)**: a `<details>` list instead of a `<select>`, because
  it needs no JavaScript to navigate.
- **Product 7 (several signup forms)**: one signup page per account (`SignupForm.slug` dropped);
  nothing in the mockup needs more.
- **Product 14 (inbox chips)**: one chip row with counts, and the status filter as a `<select>` with
  a Visa button (no JS), only for accounts with utskick on; other inboxes are unchanged.
- **Product 17 (gallery and signature)**: the gallery stays two-up on mobile as in the mockup; the
  signature uses a system script font stack, not the mockup's Caveat web font (no external fonts in
  mail).
- **Product 10 (timeline in S1)**: built in S1 as asked; "Svarar oftast på sms, kvällstid" is
  computed in S4 from at least 3 replies, never guessed.
