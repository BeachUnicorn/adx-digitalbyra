# S2 handoff: the foundation is in, four builders finish S2

Written by the S2 foundation agent on 2026-10-09. The contract is `README.md` in this folder
(read "Fixed decisions", "Naming rule", B.2, C.1 to C.5, D, E, G, H, I, J S2). This file says
what exists now, who owns what, which helpers to call and which signatures to implement.
`S1-HANDOFF.md` still describes the S1 helpers (access, consent, suppression, contacts, keys,
limits, alerts, timeline, optin, capture, tokens). Delete this file when S2 ships and fold the
deviations into README ("S2 as built").

Decisions confirmed by Giovanni after the contract was written: the shared reply number
`+46766860046` may be pointed at our inbound webhook after S2 is deployed (manual step, K.2.1);
the yearly 999 kr sms fee also applies to customers who use sms only through Utskick (no fee
change in code, K.2.7); SES eu-west-1 has production access; the identity `utskick.adx.se` is
verified; the role `adx-utskick` exists.

## What the foundation built

- **Models and migrations** (only the foundation edits `models.py` and migrations in S2; ask
  the lead if a field is missing):
  - `utskick/0002_utskick.py`: `Utskick`, `Recipient`, `AllowedHost`, `TrackedLink`,
    `LinkCode`, `Click`, `Thread`, `ThreadMessage`, `InboundMessage`; nullable `utskick` FKs on
    `ConsentLog`, `Suppression`, `Event` and `Event.recipient`.
  - `flamingo/0017_svar_pa_utskick.py`: `Lead.utskick`, `Lead.utskick_recipient`,
    `Lead.attribution` (`db_default {}`), `Lead.activity_at` (`db_default now()`, backfilled
    to `created_at` in the same migration, index `flamingo_lead_activity`), source `reply`.
  - `sms/0006_svar_och_utskick.py`: `SmsMessage.source` (`db_default "api"`), `sender` 16
    chars, index `sms_msg_source (account, source, created_at)`, partial index
    `sms_msg_reply_number (to, created_at) WHERE sender = '+46766860046'`,
    `MonthlyStatement.by_source` (`db_default {}`).
  - **B.0 follow-up done**: every foreign key created in S2 gets its `ON DELETE CASCADE / SET
    NULL` in Postgres too (`apps/utskick/dbfk.py`, a `RunPython` last in each migration), so
    S1 code (a rollback) deleting a contact, lead, account or user never hits an
    `IntegrityError` from a table it does not know. Guard:
    `test_s2_foundation.DbOnDeleteTests` fails for any FK touching utskick added after S1
    without the database rule. **Any later migration that adds an FK to or from utskick must
    end with `migrations.RunPython(lambda a, s: dbfk.apply(a, s, [...]), RunPython.noop)`.**
  - Model properties changed in `apps/flamingo/models.py` (foundation-owned in S2):
    `Lead.can_send_to_google` is false when `utskick_id` is set (D11), `Lead.display_name`
    falls back to "Svar på utskick" for `source="reply"`.
- **apps/sms (C.1 in full)**: `service.send_for_account`, reply-number rule, `source` on
  every row, `ratelimit.headroom`/`check_account_minute(headroom=, global_headroom=)`,
  `estimate_cost(part_cost_hint=)`, 46elks 429 -> `rate_limited`, `hooks.py`
  (`status_changed`, `labels`), `api.message_detail` limited to `source="api"`, `"source"` in
  the API JSON, `GET /api/sms/v1/suppressions/?to=`, `by_source` on statements and
  `usage()`, portal source filter and labels, `/smsz/` and portal docs, `elks.list_messages`
  (for the G.1 reconcile), README section. API behaviour unchanged except the 429 mapping.
- **Settings (C.4 S2)** in `config/settings/base.py`, `development.py` and `.env.example`:
  `UTSKICK_LINK_HOSTS`, `UTSKICK_SMS_LINK_BASE`, `UTSKICK_EMAIL_LINK_BASE`,
  `UTSKICK_REPLY_NUMBER`, `UTSKICK_ELKS_INBOUND_TOKEN`, `UTSKICK_SMS_ACCOUNT_PER_MINUTE` (45),
  `UTSKICK_SMS_GLOBAL_PER_MINUTE` (60). Locally the link hosts are `k.localhost` and
  `klick.localhost` with bases `http://k.localhost:8770` / `http://klick.localhost:8770`, so
  `http://k.localhost:8770/robots.txt` works against the dev server.
- **Link host router (E.1, C.3)**: `links.LinkHostMiddleware` right after
  `SecurityMiddleware`; `config/urls_links.py` (namespace `links`, `handler404` = "Länken har
  gått ut"); `FlamingoGateMiddleware` returns early on link hosts; ASGI returns 404 for MCP
  and OAuth paths on link hosts and drops the link hosts from the MCP allowed hosts. Real views:
  `link_views.home`, `robots`, `not_found`.
- **URL wiring for every S2 route** (tables below) pointing at stub views; `/api/utskick/`
  webhook URLs; `lp/<slug>/besok/` beacon stub; manage URLs in three new modules.
- **Nav**: "Utskick" after "Kontakter" in the Flamingo side menu when utskick is on (10
  links); tab row Utskick, Inställningar (`nav.utskick_tabs`); `app_views.render_utskick`;
  `templates/flamingo/app/utskick/_layout.html` (blocks `app_title`, `ut_head`, `ut_header`,
  `ut_content`) and `_stub.html`.
- **Shared primitives, implemented and tested** (owners may extend, not change signatures):
  `sending/sms_wrapper.py`, `codes.py`, `smsbridge.py`.
- **Stub modules with the exact signatures other builders call** (below). Every stub says
  `STUB` and names its owner; the owner replaces the whole module (or, for `links.py`, the
  section under "Länkarnas regler").
- **Overview hook**: `manage_views.overview` merges `panel_context(now)` from
  `manage_sending`, `manage_inbound`, `manage_links`; `overview.html` includes
  `_overview_sending.html`, `_overview_inbound.html`, `_overview_hosts.html` (stub partials)
  above "Nödstopp".
- **static_version** already lists `css/flamingo-app-utskick-views.css`,
  `css/flamingo-app-utskick-thread.css`, `js/flamingo-app-utskick.js` (missing files are
  skipped), so no builder edits `apps/manage/context_processors.py`.
- Tests: `apps/utskick/test_s2_foundation.py` (51 tests) and 36 new tests in
  `apps/sms/tests.py` (classes from `SourceTests` to `ElksListTests`). Existing tests changed
  on purpose: `test_s1_models.NavTests` expects Kontakter and Utskick (C.2);
  `test_s1_views.ViewsFixture` has an utskick and a reply lead per account so the H.1 route
  walk (`TenancyTests`) covers the S2 routes (B extends it when the views are real);
  `apps/flamingo/test_demo.DemoContentTests.test_leads_in_every_status...` temporarily
  excludes source `reply` (**A removes the exception** when `demo.seed` creates the reply
  threads).
- 375 px: checked in the browser on the local demo: the side menu with ten links wraps and
  nothing is clipped, `utskick/installningar/` (stub) shows "Utskick: Inställningar" in the
  collapsed sub-nav, `http://k.localhost:8770/` has no overflow and sets no cookie. The stub
  pages have no content of their own, so B and D still owe the real 375 px QA.

## The four builders and what they own

One owner per file. "Shared" files: append, do not change behaviour others rely on, say so in
your report. Never edit `models.py`, migrations, settings, `config/urls*.py`,
`apps/flamingo/urls.py`, `apps/flamingo/public_urls.py`, `apps/sms/*_urls.py` or the
middleware list: ask the lead.

### A. Sending engine ("sändnings-byggaren")

- Python: `audience.py`, `composer.py`, `timing.py`, `sending/state.py`, `sending/checks.py`,
  `sending/freeze.py`, `sending/sms.py`, `sending/recover.py`, `sending/tick.py` (S2 phases
  and `work_exists`), `retention.py` (E.7 rows for S2 tables), `demo.py` (S2 seed: one sent
  and simulated utskick with clicks and leads, one scheduled, one draft, two reply threads, one
  STOPP; `reset` deletes the S2 rows; `simulate`), `manage_sending.py` (`info_override`,
  `probe`, `panel_context`), `sending/sms_wrapper.py` (extend only).
- Also: `manage_views.customer_update` and `end_account` additions (D.8 `pause_account` when
  utskick is turned off or `sending_blocked` is set; "Avsluta utskick och radera allt" removes
  S2 rows), agency alerts of D.9 and H.5 (first utskick over 500, information over 200 or more
  than 2 in 30 days, breaker) through `alerts.agency`.
- Templates: `templates/manage/utskick/_overview_sending.html`.
- Tests: `test_s2_tick.py`, `test_s2_info.py`, `test_demo.py` extension that patches
  `apps.sms.elks._post` to fail during the demo walk.

### B. Utskick UI ("utskick-ui-byggaren")

- Python: `app_views/utskick.py` (every view in the route table), `reports.py`, the I.10 sms
  templates ("Mallar"), the S2 rows of I.9 in `utskick_settings`, test send (F.8, through
  `sms_wrapper.send(source="test")`, `Counter(scope="test_send")`, `Event(kind="test_send")`).
- Outside the app: `apps/flamingo/rules.three_things` (paused utskick, pending link hosts via
  `AllowedHost`), `apps/flamingo/app_views/overview.numbers_for` (C.2: `leads` excludes
  `source="reply"`, `google_*` exclude utskick, `utskick_leads`/`utskick_deals`),
  `timeline.py` S2 sources (shared; sms sent and delivered, clicked, LP visit, reply, STOPP),
  `contacts._summary` / `LAST_LABELS` S2 parts (shared).
- Templates: `templates/flamingo/app/utskick/{list,step_mottagare,step_kanal,step_innehall,
  step_tid,step_review,report,recipients,settings}.html` plus partials (prefix `_`), extending
  `flamingo/app/utskick/_layout.html`; delete `_stub.html` when the last stub is gone.
- Static: `static/css/flamingo-app-utskick-views.css` (prefix `.fl-ut-`),
  `static/js/flamingo-app-utskick.js` (counts, link picker, confirm `<dialog>`, **and the sms
  counter that the inbox thread reuses**, see "The sms counter contract").
- Tests: `test_s2_ui.py` (tenancy for every route, foreign ids 400, staff checkbox, 375 px
  template guards), `test_demo.py` route list (`app_utskick_list`, `app_utskick` per utskick,
  the steps).

### C. Links and attribution ("länk-byggaren")

- Python: `links.py` section "Länkarnas regler" (replace the stubs; leave the router above
  it), `link_views.py` (keep `home`, `robots`, `not_found`; replace the stubs), `codes.py`
  (extend only), `attribution.py` (new), `tokens.py` additions (form nonce, `ut` token,
  shared S1 module), `optin.py` S2 part (`sms_work_exists`, `send_due_sms`: signup with sms,
  "slå på sms" on Mina val, both through `/b/`), `public_views.py` and `app_views/signup.py`
  S2 parts (sms channel when `links_ready_at`, K.2.5), `manage_links.py`.
- Outside the app: `apps/flamingo/leads.py` (`UT_KEY`, `ut_from`, `click=`),
  `apps/flamingo/limits.py` (raised limits with a same-account click),
  `apps/flamingo/public_views.py` (`landing` with `ut`, `visit_beacon` real,
  `_live_campaign(slug, click=None)`, hidden `ut`), `templates/flamingo/lp/ren/blocks/form.html`,
  `static/js/flamingo-lp.js`, `templates/manage/utskick/_customer_card.html` pending hosts,
  `server/` link-host files of C.5 (`sites.d/adx.conf`, `templates/nginx.conf.template`
  `${LINK_BLOCK}`, `lib.sh`, `certs.sh` lineage `adx-links`, `nginx-only.sh`, `access_log off`
  for `/api/utskick/`, `/utskick/val/`, `/utskick/bekrafta/`). Nothing is run on the server.
- Templates: `templates/utskick/links/{sms_unsubscribe,sms_preferences,confirm,...}.html`
  extending `utskick/links/_base.html` (no `{% csrf_token %}` there, ever),
  `templates/manage/utskick/_overview_hosts.html`.
- Static: `static/css/utskick-public.css` (append a section, prefix `.up-`).
- Tests: `test_s2_links.py` (with `LINK_SETTINGS` overrides, see Tests below).

### D. Inbound and Inbox ("inkorg-byggaren")

- Python: `inbound/elks.py` (replace the stub: webhook view, `handle`, `reconcile_due`,
  `reconcile`), `inbound/routing.py`, `inbound/stop.py`, `threads.py` (replace: thread and lead
  writes, queued STOPP/START answers and batched owner notices, `work_exists`, `send_due`),
  `app_views/inbox_reply.py`, `manage_inbound.py`.
- Outside the app: `apps/flamingo/app_views/inbox.py` and its templates (C.2: `channel()`,
  `-activity_at` ordering, one chip row with counts, status `<select>`, "Klar", thread include,
  `select_related("reply_thread")`, docstring fix), `apps/flamingo/sms.py`
  (`owner_reply_text`, `notify_owner_text`, `notify_new_lead` skip for utskick leads),
  `contacts.delete_contact` and `contacts.export_contact` S2 parts (H.4: threads, reply leads,
  recipients blanked, `SmsMessage.to/body` blanked for `source != "api"`, inbound blanked;
  shared S1 module), `apps/flamingo/test_inbox.py` folder list.
- Templates: `templates/flamingo/app/utskick/_thread.html` (the only D template in that folder),
  inbox templates, `templates/manage/utskick/_overview_inbound.html`.
- Static: `static/css/flamingo-app-utskick-thread.css` (prefix `.fl-th-`); the counter comes
  from B's `flamingo-app-utskick.js`.
- Tests: `test_s2_inbound.py`, `test_s2_inbox.py`.

## Implemented helpers (call these)

### apps/sms

```python
service.send_for_account(account, data, *, source, api_key=None, allow_reply_number=False,
                         headroom=0, global_headroom=0, part_cost_hint=None) -> Outcome
#   account is an SmsAccount; data {"to", "message", "from", "reference", "dryrun"}.
#   Outcome: .ok .error .detail .message (SmsMessage) .created .duplicate .unknown .retry_after
#   errors: invalid_request invalid_number country_not_allowed sender_not_allowed
#   message_too_long monthly_cap_reached sms_not_enabled rate_limited provider_error;
#   .unknown with a RESERVED row = provider_unknown (never resend; D.5)
service.reply_number() -> "+46766860046"
service.THROTTLED_RETRY_AFTER == 60
ratelimit.headroom(account_per_minute, agency_per_minute) -> (headroom, global_headroom)
hooks.register_status_callback(func); hooks.register_labeler(func); hooks.labels(messages)
elks.list_messages(since, *, direction="incoming", to=None, max_pages=20)
    -> [{"id", "from", "to", "message", "created" (aware datetime), "direction"}]   # ElksError
SmsMessage.Source.{API, UTSKICK, FLOW, REPLY, SYSTEM, TEST}; models.REPLY_NUMBER
pricing.usage(account)["by_source"] -> {source: {"sms", "parts", "cost"}}
```

### apps/utskick/sending/sms_wrapper.py (A owns; C and D call it)

```python
send(account, *, to, body, sender, source, reference="", part_cost_hint=None) -> service.Outcome
#   account is a FlamingoAccount. Raises DemoRefused (demo, D12) and keys.KeyMismatch (H.7).
#   No SmsAccount -> Outcome error "sms_not_enabled". Reply number allowed as sender.
#   Headroom only for source utskick/flow (45/60 per minute); reply, system, test have none.
#   Does NOT check Switchboard, the breaker, the window, collisions, consent or suppression:
#   the caller runs sending/checks first. Byrån's probe deliberately skips sms_enabled.
assert_not_demo(account); sms_account_for(account); headroom_for(source); DEMO_TEXT
```

References: `u<utskick>:<recipient>` (utskick), `t<thread_message>` (inbox reply),
`x<inbound>` (STOPP/START answer), anything else must stay within
`[A-Za-z0-9._:-]{1,64}` and be unique per account while the sms is not stopped.

### apps/utskick/codes.py (C owns; A and D call it)

```python
ALPHABET, LENGTH == 6, MAX_TRIES == 3
new_code() -> str; new_codes(n) -> list[str] (distinct); taken(codes) -> set
create_confirm(account, *, value_hash, purpose, contact=None, now=None) -> LinkCode
#   kind confirm, expires after LinkCode.CONFIRM_HOURS (24); CodeCollision after 3 tries
find(code, kind) -> LinkCode | None        # the view decides about expiry and used_at
```

A's freeze inserts link and person codes in bulk inside a savepoint and redraws on collision
(D.3 step 3).

### apps/utskick/links.py router (foundation; C appends the rules)

```python
link_hosts() -> set; link_host_kind(host) -> "k" | "klick" | ""; host_of_scope(scope)
LinkHostMiddleware          # request.urlconf, request.is_link_host, request.link_host
on_link_host("k")           # decorator: 404 on the other host and on adx.se
private(response)           # Referrer-Policy no-referrer + Cache-Control private, no-store
sms_link(code, path="") -> "k.adx.se/Ab12Cd" | "k.adx.se/s/Ab12Cd"   # as written in the sms
```

### apps/utskick/smsbridge.py (foundation)

`sync_from_message(message)` (registered as the apps/sms status hook: Recipient
`sending|unknown -> sent`, `sending|sent|unknown -> delivered|failed`, ThreadMessage
`sending -> sent|failed`, conditions in the UPDATE so it never moves backwards) and
`labels_for(messages)` ("Utskick: <name>", "Flöde: <name>"). The contract's
`sending.sms.sync_from_message` is this function. A's loop must write `status="sent"` only
`WHERE status IN ('sending', 'unknown')` (D.5) and may read the SmsMessage status after the
send to catch a report that arrived first.

### Models (constants worth knowing)

`Utskick.Status`, `.ChannelMode`, `.InfoReason`, `.SenderKind`, `.SendMode`, `.PauseReason`
(labels = I.5 list labels), `Utskick.EDITABLE`, `ACTIVE`, `PAUSED_STATES`, `FINISHED`,
`RECONFIRM_REASONS`, `Utskick.objects.listed()` (use it for every list, count and header: S5
makes it exclude flow-step utskick), `REKLAM`, `INFORMATION`. `Recipient.Status`,
`.SkipReason` (labels = I.8 "Hoppades över"), `Recipient.RANK`, `SENT_LIKE`.
`LinkCode.Kind`, `.Purpose`, `CONFIRM_HOURS`. `Click.Channel`, `.Kind`,
`MAX_ENGAGED_SECONDS`. `InboundMessage.Status`. `Thread.Kind`. `ThreadMessage.Direction`,
`.Status`, `MAX_BODY`. `Event.S2_KINDS` (`lp_visit`, `call_click`, `reply`, `stop`,
`start`). `Lead.SOURCE_REPLY`.

## Stub signatures to implement (callers rely on these exact names)

| Module (owner) | Signature | Called by |
|---|---|---|
| `audience.py` (A) | `clean(account, data) -> dict` (owned_ids, ForeignIds -> 400); `contacts(utskick) -> QuerySet`; `count(utskick, now=None) -> dict` (I.8 JSON shape); `describe(utskick) -> str` | B, A |
| `composer.py` (A) | `placeholders(body)`; `validate(account, body) -> list[str]`; `merge(text, values, fallbacks) -> str`; `render_sms(utskick, recipient, sender) -> str`; `preview(utskick, contact=None, sender=None) -> dict` (`text, parts, encoding, chars, non_gsm, longest_parts, longest_count, cost_ore`); `opt_out_line(sender_kind, person_code=None) -> str` | B, A, D (appending " /Exempelrör" is D's) |
| `timing.py` (A) | `is_holiday(day)`; `window_for(settings_row, day)`; `sms_window_open(settings_row, now)`; `next_window_start(settings_row, now)`; `window_text(settings_row, day=None)`; `next_start_text(settings_row, now)` | B, A |
| `sending/state.py` (A) | `transition(utskick, to, reason="", *, expected=None, now=None) -> bool`; `confirm(utskick, *, actor, nonce, summary, send_now=False, now=None) -> Result`; `unconfirm(utskick)`; `pause(utskick, reason, *, actor=None, note="", now=None)`; `resume(utskick, *, actor, now=None) -> Result`; `cancel(utskick, *, actor, now=None)`; `pause_account(account, reason, now=None) -> int` | B, A, manage |
| `sending/checks.py` (A) | `Check` (implemented); `send_time_checks(recipient, now=None)`; `reply_checks(account, contact, address, now=None)`; `sender_for(recipient, now=None) -> (sender, collided)`; `sender_identified(utskick, body=None)`; `information_problems(utskick) -> list[str]`; `collision_count(utskick, now=None)`; `breaker_active(now=None)`; `sms_ready()` | B (Granska), D (replies), A |
| `reports.py` (B) | `summary(utskick, now=None) -> dict`; `final_stats(utskick) -> dict` | A (finish stores `Utskick.stats`), B |
| `links.py` rules (C) | `GLOBAL_HOSTS`; `LinkRefused`, `HostPending`; `clean_external(account, url) -> str`; `host_status(account, host)`; `request_host(account, host, user) -> AllowedHost`; `add_link(utskick, *, key, campaign=None, destination="", label="") -> TrackedLink`; `check_destinations(account, urls) -> {url: bool}`; `build_destination(link, recipient, click) -> str`; `rollup(now, deadline=None) -> dict` | B (picker, Granska), A (tick phase 9) |
| `optin.py` S2 part (C) | `sms_work_exists(now=None) -> bool`; `send_due_sms(now=None, deadline=None, limit=100) -> dict` | A (tick phase 3) |
| `threads.py` (D) | `work_exists(now) -> bool`; `send_due(now, deadline) -> dict` | A (tick phase 3) |
| `inbound/elks.py` (D) | `inbound(request, token)` view; `reconcile_due(now) -> bool`; `reconcile(now, deadline) -> dict` | URLs, A (tick phase 2) |

`deadline` is always a `time.monotonic()` value at which the phase must stop (as
`optin.send_due` in S1). Return dicts hold counts only (they go into the tick summary and
`backups/utskick.log`, never addresses).

### Tick wiring (A owns `sending/tick.py`)

`work_exists(now)` = S1 queues `or` A's due/queued/stale checks `or optin.sms_work_exists(now)
or threads.work_exists(now) or inbound.elks.reconcile_due(now)`. Phases: 1
`importer.recover` + A's `recover_stale`; 2 `inbound.elks.reconcile(now, phase_deadline)` when
`reconcile_due`; 3 `optin.send_due` (DOI) + `optin.send_due_sms` + `threads.send_due`; 4
start_due and freeze; 5 sms loop; 8 imports; 9 finish (+ `links.rollup`,
`reports.final_stats`). STOPP/START answers and confirm sms are not windowed but do respect
the breaker and the cap pre-check (D.4).

### The sms counter contract (B implements, D reuses)

`static/js/flamingo-app-utskick.js` binds to every `textarea[data-ut-sms]` on the page and
writes into the element named by its `data-ut-sms-count` attribute (an id): "GSM-7 · 134 av
160 · 1 del" (mirroring `apps/sms/encoding.analyse`), plus " · 0,39 kr" when the textarea has
`data-ut-sms-ore-per-part`. `data-ut-sms-suffix=" /Exempelrör"` is counted but not typed.
No inline script; works without JS (the server validates again).

## Routes (all resolve today)

App, namespace `flamingo`, prefix `/flamingo/app/` (`apps/utskick/app_urls.py`), all behind
`@utskick_view` (+ `@require_POST` under it for POST views). Stubs: GET pages render
`_stub.html` with the tab row, POST/JSON answer 501, foreign pk 404.

| Name | Path | Method | View (owner) |
|---|---|---|---|
| `app_utskick_list` | `utskick/` | GET | `utskick.utskick_list` (B) |
| `app_utskick_new` | `utskick/ny/` | POST | `utskick.utskick_new` (B) |
| `app_utskick_settings` | `utskick/installningar/` | GET, POST | `utskick.utskick_settings` (B) |
| `app_utskick` | `utskick/<pk>/` | GET | `utskick.utskick_report` (B) |
| `app_utskick_step` | `utskick/<pk>/steg/<step>/`, step in `mottagare kanal innehall tid granska` (converter `utskick_steg`, `utskick.STEPS`) | GET, POST | `utskick.utskick_step` (B) |
| `app_utskick_count` | `utskick/<pk>/antal/` | GET JSON | `utskick.utskick_count` (B) |
| `app_utskick_sms_preview` | `utskick/<pk>/sms/` | GET/POST JSON | `utskick.utskick_sms_preview` (B) |
| `app_utskick_link_check` | `utskick/<pk>/lankkontroll/` | POST JSON | `utskick.utskick_link_check` (B) |
| `app_utskick_test` | `utskick/<pk>/test/` | POST | `utskick.utskick_test` (B) |
| `app_utskick_confirm` | `utskick/<pk>/skicka/` | POST | `utskick.utskick_confirm` (B) |
| `app_utskick_state` | `utskick/<pk>/lage/` | POST `action=` | `utskick.utskick_state` (B) |
| `app_utskick_recipients` | `utskick/<pk>/mottagare/` (`?visa=`) | GET | `utskick.utskick_recipients` (B) |
| `app_utskick_save_list` | `utskick/<pk>/mottagare/lista/` | POST | `utskick.utskick_save_list` (B) |
| `app_lead_reply` | `inkorg/<pk>/svara/` | POST | `inbox_reply.lead_reply` (D) |
| `app_lead_unsubscribe` | `inkorg/<pk>/avregistrera/` | POST | `inbox_reply.lead_unsubscribe` (D) |

Link hosts, `config/urls_links.py`, namespace `links` (`reverse(..., urlconf=
"config.urls_links")`):

| Name | Path | Host | View |
|---|---|---|---|
| `home` | `/` | both | `link_views.home` (real) |
| `robots` | `/robots.txt` | both | `link_views.robots` (real) |
| `click` | `/<code>` | k | `link_views.click` (C, stub 404) |
| `sms_unsubscribe` | `/s/<code>` | k | C, stub 404 |
| `sms_preferences` | `/p/<code>` | k | C, stub 404 |
| `confirm` | `/b/<code>` | k | C, stub 404 |

Code regex `[A-Za-z0-9]{6}`; the S3 routes (`/m/`, `/a/`, `/v/`, `/w/`, `/o/`, `/c/`) and S4
named links are not wired.

Others:

| Name | Path | View (owner) |
|---|---|---|
| `utskick_api:elks_inbound` | `/api/utskick/46elks/inkommande/<token>/` | `inbound.elks.inbound` (D; stub 404, empty body) |
| `flamingo_public:visit_beacon` | `/lp/<slug>/besok/` POST, csrf_exempt | `flamingo.public_views.visit_beacon` (C; stub 204) |
| `sms_api:suppressions` | `/api/sms/v1/suppressions/?to=` | `apps.sms.api.suppressions` (real) |
| `manage:utskick_inbound_route` | `/manage/utskick/inkommande/<pk>/` POST | `manage_inbound.inbound_route` (D) |
| `manage:utskick_host_decide` | `/manage/utskick/vardar/<pk>/` POST | `manage_links.host_decide` (C) |
| `manage:utskick_info_override` | `/manage/utskick/utskick/<pk>/undantag/` POST | `manage_sending.info_override` (A) |
| `manage:utskick_probe` | `/manage/utskick/prov/` POST | `manage_sending.probe` (A) |

## Templates, CSS and JS

| Folder or file | Owner | Notes |
|---|---|---|
| `templates/flamingo/app/utskick/_layout.html` | foundation (shared) | blocks `app_title`, `ut_head`, `ut_header`, `ut_content`; tab row from `ut_nav` (set `ut_nav=None` in the context to hide it in the guide) |
| `templates/flamingo/app/utskick/*.html` | B, except `_thread.html` (D) | `.fl-table`, `data-label` on every `<td>`, no `style=`, no inline script (S1 guards walk this folder) |
| `templates/utskick/links/*.html` | C (`_base`, `home`, `not_found` are foundation) | extend `utskick/links/_base.html`; never `{% csrf_token %}` |
| `templates/manage/utskick/_overview_{sending,inbound,hosts}.html` | A, D, C | every table needs `<thead>` |
| `static/css/flamingo-app-utskick.css` | foundation (shared) | tab row and `<details>` sub-nav only |
| `static/css/flamingo-app-utskick-views.css` | B | `.fl-ut-` |
| `static/css/flamingo-app-utskick-thread.css` | D | `.fl-th-` |
| `static/js/flamingo-app-utskick.js` | B | incl. the sms counter contract |
| `static/css/utskick-public.css` | C appends | `.up-` |
| `static/css/manage-utskick.css` | shared | append a marked section per builder (`.mu-se-`, `.mu-in-`, `.mu-ho-`) only if the panel classes are not enough |

## Tests and conventions

- Test command: `TEST_DB_NAME=<unique> SENTRY_DSN= uv run python manage.py test <labels>
  --noinput` (`--parallel 1` for the full suite). No network: FakeElks style
  `mock.patch("apps.sms.elks._post", side_effect=fake)` (see `apps/sms/tests.FakeElks` and
  `test_s2_foundation._FakeElks`).
- Link-host tests: the test settings are development settings (`k.localhost`), so decorate
  with `override_settings(UTSKICK_LINK_HOSTS=["k.adx.se", "klick.adx.se"],
  UTSKICK_SMS_LINK_BASE="https://k.adx.se", UTSKICK_EMAIL_LINK_BASE="https://klick.adx.se")`
  (`test_s2_foundation.LINK_SETTINGS`) and pass `HTTP_HOST="k.adx.se"`. The router reads the
  settings per request.
- Utskick tests use `testing.UtskickFixture`; an `SmsAccount(customer=self.customer,
  is_enabled=True, sender_name="Exempelror")` is needed before anything sends.
- Copy and code rules as in S1-HANDOFF ("Template conventions"); in `test_*.py` write
  `chr(0x2013)` for the forbidden characters and never the promise phrases.
- `Lead.activity_at` has both `default=timezone.now` and `db_default`; set it explicitly on
  every reply (`Lead.objects.filter(pk=...).update(activity_at=now)`).

## Deviations from the contract (foundation)

1. B.0 follow-up: Django 6.0.5 has no database-level `on_delete`; each S2 migration ends with
   `dbfk.apply` (drop and re-add the FK with `ON DELETE` and the same name, after flushing the
   schema editor's deferred SQL). Guard `DbOnDeleteTests`.
2. `Recipient.ses_message_id` and `opened_at` (S3) are in 0002, so S3 adds no column to a
   table S2 writes; the `ses_message_id` index is partial (`!= ''`), not `db_index`.
3. `Utskick.merge_fallbacks` is in S2 (F.3: the merge engine applies to sms too). Other email
   columns, `flow_step`, the partial index "where flow_step null" and `Recipient.flow_run` are
   not; `Utskick.objects.listed()` exists so S5 can add the filter in one place. The recipient
   unique constraint has no `flow_run null` term yet (S5 changes it).
4. `Recipient.basis` is `max_length=17` (`existing_customer`, as Consent in S1).
5. `TrackedLink.slug` and its partial unique constraint are in S2 (J S4 lists "TrackedLink slug
   index" for 0004; nothing is left for S4 there). Extra index `LinkCode (kind, created_at)`
   for retention.
6. `sending.sms.sync_from_message` is `smsbridge.sync_from_message` (foundation), registered in
   `UtskickConfig.ready()` together with the portal labeler.
7. Suppression endpoint: 404 only when the customer never had utskick (no `UtskickSettings`
   row). A disabled utskick still answers: the suppressions bind the customer's marketing
   either way (the contract says "404 when utskick is not enabled").
8. 46elks 429 on the estimate (dryrun) also maps to `rate_limited`, without a row or alert
   (the contract only names `_deliver`). The 429 outcome carries the REJECTED row, so the API
   error includes its `id` like other stored rejections.
9. Headroom in `sms_wrapper` only for sources `utskick` and `flow` (single sms such as replies,
   confirmations and tests use the full limits).
10. Portal source filter groups: "Utskick" = utskick, flow, test; "Svar" = reply, system.
    The filter is shown only when the account has non-API rows.
11. `elks.list_messages` pages with `start=<next>` (46elks sms-history docs as remembered); the
    contract says "paged with `end`". It stops after one page if the parameter is wrong (no
    loop), so the S2 checklist step "the reconcile finds nothing missing" must be verified
    with a known message older than the first page, or the docs read.
12. Manage S2 views live in `manage_inbound.py`, `manage_links.py`, `manage_sending.py`
    (one owner each); `manage_views.overview` only merges their `panel_context`.
13. The inbox reply routes are in `apps/utskick/app_urls.py` (namespace `flamingo`), next to
    Flamingo's own `inkorg/` routes.
14. Link-host 404s all say "Länken har gått ut" (no site chrome, no account data). The home
    page links ADX's privacy page at `SITE_BASE_URL + "/integritetspolicy/"`
    (`link_views.ADX_PRIVACY_PATH`); the slug is not in the local database, verify it.
15. `Utskick.purpose` defaults to `reklam`; `channel_mode` to `sms_only`.

## Open items for S2

- Verify the 46elks history paging parameter (deviation 11) and the ADX privacy page slug
  (deviation 14) before the S2 checklist.
- The probe (`manage:utskick_probe`) needs a rule for which account pays ("the internal test
  customer"): a setting, or a customer chosen in the form (A decides, documents it).
- `apps/flamingo/test_core.py` was not extended with the nav test (S1 did the same; the nav is
  covered by `test_s1_models.NavTests` and `test_s2_foundation.UrlWiringTests`).
- The local dev database has the three S2 migrations applied (see the foundation report).
- README "S1 as built" still says "not deployed"; the lead updates README (K.2.1 and K.2.7 are
  answered, see the top of this file) when S2 ships.
- 375 px QA of every S2 view is the UI and inbox builders' job (stub pages have no content).
- Note for every builder: the S1 template guard matches the literal text `<td` anywhere in a
  template under `templates/flamingo/app/utskick/`, comments included (write "tabellcell").

## Sending engine (done by the engine builder)

Built: `audience.py`, `timing.py`, `sending/{checks,state,freeze,sms,recover}.py`, the S2 phases
of `sending/tick.py` (S1 phases unchanged; `run(now, budget, only=None)`, `utskick_tick --only
<pk>`), `alerts.py` additions (`breaker`, `utskick_paused`, `first_big_utskick`,
`information_utskick`, `low_disk`), `retention.purge_s2` and the month-end RESERVED check
(`recover.month_end_check`) in `utskick_daily`, `manage_sending.py` and
`templates/manage/utskick/_overview_sending.html`, marked S2 blocks in
`manage_views.customer_update` (D.8 `pause_account`) and `end_account` (`_end_utskick`). Tests:
`test_s2_tick.py`.

APIs others rely on (beyond the stub table):

```python
checks.Check(defer, skip, reason, not_before, text, sender, collided)   # .ok
checks.sendable(account) -> "" | "blocked" | "account_disabled"; checks.sendable_q(prefix)
checks.SMS_OFF_TEXT, BREAKER_TEXT, SUPPRESSED_TEXT, BLOCKED_TEXT, DISABLED_TEXT, SENDER_TEXT,
       LOOKS_LIKE_AD_TEXT, INFO_REASON_TEXT, INFO_OTHER_TEXT, INFO_LP_TEXT, INFO_HOST_TEXT
checks.provider_trouble(now)        # after an unknown/provider_error outcome: breaker at 3 in 2 min
state.issue_nonce(utskick); state.prechecks(utskick, now, reconfirmed=False) -> Verdict
state.Result(ok, error, utskick); state.STALE_TEXT, RECONFIRM_TEXT, STAFF_RESUMES_TEXT, ...
audience.stored(u), is_empty(u), confirmed_total(summary, mode), recipient_total(u),
         week_counts(...), Judge, plan(mode, reasons), RECENT_DAYS
timing.clock_text(moment, now) -> "09.00 i morgon" / "10.00 lördag 17 okt"
freeze.ensure_person_code(recipient); sms.adopt(recipient, message, sender)
```

Data written for the UI: `Utskick.frozen_counts = {"sms", "email", "skipped",
"skipped_by_reason", "total", "estimate": {"sms", "parts", "cost", "remaining", "cap"}}`
(amounts in 1/10 000 kr, for the "Pausat vid taket" banner); `Utskick.stats["pause"] = {by,
user, staff, at, reason, note}` (staff note, the stops numbers), `stats["resumed"]`,
`stats["cancelled"]`; at finish `reports.final_stats` is merged over them.

Deviations (engine):

1. Swedish holidays include midsommarafton, julafton and nyårsafton (weekend window); the
   legal holidays are computed (Easter by the Gauss/Meeus algorithm).
2. The demo account is simulated before the Switchboard checks: it never sends, so the switch
   does not gate it, and `state.confirm` accepts a demo utskick while sms is off.
3. The breaker is read in real time (it is set from real time and SmsMessage.created_at).
4. When every due account has used its minute budget, the sms phase sleeps 1 s and tries
   again until its deadline, instead of ending (else the 60 s window halves throughput).
5. `sms_not_enabled` (and a missing or disabled SmsAccount) pauses all of the account's
   sending utskick (`sms_disabled`), not just one.
6. `invalid_number` / `country_not_allowed` at send time: recipient `failed` with
   `skip_reason` `invalid_number` / `country` and the label as `error` (report groups them).
7. Person codes are frozen only for the name sender; a reply-number recipient gets one at send
   time when a reply collision switches it to the name sender (`ensure_person_code`; composer
   creates any missing code too).
8. D.9 stops: `max(Recipient.stopped_at count, Suppression(utskick=u, reason stop|link))`.
9. Tick phase 4 also pauses utskick of accounts that can no longer send
   (`freeze.pause_unsendable`), so Flamingo off or an inactive customer pauses (D.8) even when
   changed outside the utskick card.
10. The freeze cost pre-check uses `composer.preview` parts x (`recent_part_cost("SE")` or
    5 200) plus markup; no 46elks dryrun inside the pre-check.
11. Weekly cap: reklam recipients sent or sending in the Stockholm ISO week of the send moment;
    information neither counts nor is capped.
12. Recovery: a stale `sending` recipient without a held message is requeued with its attempt
    counted; after 5 attempts it is `failed` ("Sms:et kunde inte skickas.").
13. `links.rollup(now, deadline)` runs on worked ticks while an utskick is sending or finished
    within 30 days (C: add a `work_exists` hook if the rollup needs idle ticks).
14. Probe payer (open item above): the staff member picks the paying customer in the form,
    among customers with an enabled SmsAccount and a non-demo Flamingo account (choose ADX's
    internal test customer). At most 10 probes per staff user and hour (`Counter` scope
    `probe`). Source `test`, sender the reply number, works before `sms_enabled`, not past the
    breaker.

## Requests from engine

- **Integration**: the S2 demo seed (`demo.seed` / `reset`, handoff A) and the `test_demo.py`
  extension, and `test_s2_info.py`, were left to you as the workflow says. `retention.purge_s2`
  is in `utskick_daily` already.
- **D (inbound)**: set `Recipient.stopped_at` and `Suppression.utskick` on STOPP (G.1 step 6) so
  the D.9 stops pause counts it. **C (links)**: `/s/` should create its Suppression with
  `utskick=<the code's recipient's utskick>` (reason `link`) for the same reason.
- **B (UI)**: the cap banner numbers are in `frozen_counts["estimate"]`; the stops banner note in
  `stats["pause"]["note"]`; `state.confirm` already refuses with `checks.SMS_OFF_TEXT` /
  `state.EMAIL_OFF_TEXT` and returns `state.PAST_TEXT` for a time that passed.
- **Lead**: fold the deviations above into README "S2 as built".

## Inbound and Inbox (done by the inkorg-byggaren)

Files: `inbound/{elks,routing,stop}.py`, `threads.py`, `app_views/inbox_reply.py`,
`manage_inbound.py`, `templates/flamingo/app/utskick/_thread.html`,
`templates/manage/utskick/_overview_inbound.html`, `static/css/flamingo-app-utskick-thread.css`,
`test_s2_inbound.py`, `test_s2_inbox.py`. Outside the app: `apps/flamingo/app_views/inbox.py`
(`channel()`, `status_label()`, `render_detail()`, `TYPES`/`?typ=`, status `<select>`,
`-activity_at` ordering, docstring), `templates/flamingo/app/inbox/{list,_card,detail}.html`,
`static/css/flamingo-app-inbox.css` (section "S2"), `apps/flamingo/sms.py`
(`owner_reply_text`, `notify_owner_text`, `NOTE_VIA_UTSKICK` skip in `_notify_owner`),
`static/css/manage-utskick.css` (section `.mu-in-`). Shared modules extended (added, nothing
changed for other callers): `contacts.export_contact` / `delete_contact` call `_export_s2` /
`_erase_s2` (H.4). Foundation test changed on purpose: `test_s2_foundation.UrlWiringTests`
(`test_inbox_reply_routes` and the inbound row of `test_manage_routes_are_staff_only` no longer
expect 501).

```python
# Webhook and tick (A wires them; signatures as in the stub table)
inbound.elks.inbound(request, token); handle(fields, now=None) -> (InboundMessage, created)
inbound.elks.reconcile_due(now) -> bool; reconcile(now, deadline) -> {"checked", "inserted"[, "failed"]}
threads.work_exists(now) -> bool; threads.send_due(now, deadline) -> {"answers", "notices", "skipped"}
# Threads (A's demo seed should use these, so the seeded rows look like real ones)
threads.thread_for(account, address, contact=, utskick=, sms_message=, now=)   # reply thread
threads.thread_for_stop(...); threads.add_inbound(thread, inbound, raise_lead=, looks_like_stop=, now=)
threads.queue_answer(thread, inbound, body, now=)       # STOPP/START answer, sent by the tick
threads.suppress_number(account, e164, reason=, source=, actor=, source_detail="", utskick=None, now=None)
threads.status_label(lead) -> "Klar" | "Avregistrerad automatiskt" | status display
threads.channel_label(thread) -> "Sms-svar" | "E-postsvar" | "STOPP"
inbound.stop.classify(text) -> "stop" | "start" | ""; looks_like_unsubscribe(text)
inbound.routing.candidates(e164, now) -> [Candidate(account, message, recent, recipient, utskick)]
```

Behaviour worth knowing:
- Queue of STOPP/START answers = `ThreadMessage(direction=out, status=sending, sms_message=None,
  inbound!=None)`; why one was not sent sits in `InboundMessage.meta["answers"][account_pk]`
  (`sent`, `cap`, `not_enabled`, `demo`, `expired`, `failed`). Not gated by `sms_enabled` (J S2
  step 6 tests STOPP before sms is turned on), gated by `checks.breaker_active` and the cap
  pre-check; dropped after 6 hours. At most one STOPP answer per number per 24 h, one START link
  per number and account per 24 h.
- Reply leads keep `Lead.utskick` and `utskick_recipient` empty (the answered utskick is
  `Thread.utskick`), so "Förfrågningar via utskick" never counts replies. The utskick sms a person
  answers is copied into the thread as an `out` ThreadMessage carrying its `sms_message`
  (mockup), once per sms and thread.
- No `Event` rows for reply/stop/start: the timeline reads `ThreadMessage` (threads with
  `contact=`) and the ConsentLog (`source="stop"`, by_label "Svar STOPP"). `contacts.touch` sets
  `last_activity_kind` to `reply`, `stop` or `start`.
- A reply from a number that is not a contact creates one (`Contact.Source.REPLY`) only when
  `can_collect` and the number is not suppressed (an erased person never comes back); otherwise
  the thread has no contact.
- Owner notice: `notify_on_reply` covers replies (inbound status `routed`), `account.notify_sms`
  covers utskick-attributed leads (Flamingo's own per-lead owner sms is skipped for them); one
  SmsLog row (kind owner) per notice; never for the demo.

## Requests from inbound

- **A (demo seed):** create the two reply threads and the STOPP through `threads.thread_for` /
  `thread_for_stop` + `add_inbound` with an `InboundMessage` (status `routed` / `stop`,
  `meta["keyword"]`), so the Inkorg shows "Sms-svar", "STOPP" and "Avregistrerad automatiskt";
  then remove the `test_demo` source exception. `demo.reset` must delete reply leads
  (`Lead(source="reply")`, threads cascade) and the account's `InboundMessage` rows.
- **A (retention, E.7):** `InboundMessage` 90 days. Held rows (`ambiguous`, `unroutable`) keep
  their body until the agency routes or ignores them; retention can delete them with the rest.
- **B (timeline, LAST_LABELS):** labels for `last_activity_kind` `reply` ("Svarade på sms"),
  `stop` ("Svarade STOPP"), `start`; `numbers_for` excludes `source="reply"` (the inbox month
  count already does).
- **C (`/b/` with purpose `start`):** the START code may have `contact=None` (a number that is
  not in the register, or a thread without contact). The confirm POST must then lift the
  suppression by `(account, value_hash)` itself; `consent.set_status(proved=True)` needs a
  contact.
- **Lead / README:** the contract and this file say both `templates/flamingo/app/inbox/_thread.html`
  (task text) and `utskick/_thread.html` (C.2, this file); it is `utskick/_thread.html`. The
  agency's "Provsms till mig" (A) must send from the reply number with source `test`, so a reply
  routes to the internal test customer (routing reads every `SmsMessage` from the reply number).

## Links and attribution (done by the länk-byggaren)

Files: `links.py` (section "Länkarnas regler" plus `site_urls()` next to the router),
`link_views.py` (click, `/s/`, `/p/`, `/b/`; `home`, `robots`, `not_found` kept),
`link_actions.py` (new: the rules behind `/s/`, Ångra, `/p/`, `/b/`), `attribution.py` (new),
`tokens.py` (S2 part), `optin.py` (S2 part: confirm sms), `manage_links.py`,
`templates/utskick/links/{sms_unsubscribe,sms_preferences,confirm,too_many}.html`,
`templates/manage/utskick/_overview_hosts.html`, `static/css/utskick-public.css` (section "S2,
länkvärdarna"), `test_s2_links.py`. S1 public pages, S2 part: `public_views.py` (signup with
sms, Mina utskick turns sms on), `app_views/signup.py` and the templates
`utskick/public/{signup,thanks,preferences}.html`, `flamingo/app/kontakter/signup.html`.
Outside the app: `apps/flamingo/leads.py` (`UT_KEY`, `ut_from`, `click=`), `limits.py`
(`UTSKICK_LEADS_PER_CAMPAIGN`, `click=`), `public_views.py` (`landing`, `call_click`,
`_live_campaign(slug, data) -> (campaign, click)`, `visit_beacon`, `_capture(..., click)`),
`pagebuilder/render.py` (one line: `ut` in the form context), `templates/flamingo/lp/ren/
{layout,blocks/form}.html` (`data-fl-visit`, hidden `ut`), `static/js/flamingo-lp.js`, `server/`
(`sites.d/adx.conf` `LINK_DOMAINS`/`LINK_CERT_NAME`, `lib.sh` `build_link_block`,
`templates/nginx.conf.template` `${LINK_BLOCK}` and `access_log off` for `/api/utskick/`,
`/utskick/val/`, `/utskick/bekrafta/`, `certs.sh` lineage `adx-links`, new `nginx-only.sh`,
`README.md` rows). Nothing was run on the server.

Shared files touched (marked blocks, nothing changed for other callers): `consent.restore`
(appended, Ångra), `templatetags/utskick_tags.utskick_hosts` (appended),
`templates/manage/utskick/_customer_card.html` (pending and refused hosts, `{% load
utskick_tags %}`), `apps/website/tests.py` (the review-site allowlist of `VvsLegacyGuardTests` += `links.py`,
`test_s2_links.py`: `GLOBAL_HOSTS` names the review site), `test_s2_foundation.test_manage_routes_are_staff_only`
(the host route no longer expects 501).

```python
links.GLOBAL_HOSTS  # ((host, path_prefix), ...): google.com only for /maps
links.clean_external(account, url, *, allow_pending=False) -> str   # LinkRefused / HostPending(host, status)
links.host_status(account, host) -> "allowed" | "pending" | "refused" | "new"
links.request_host(account, host, user) -> AllowedHost   # pending row + one agency alert per day
links.add_link(utskick, *, key, campaign=None, destination="", label="", user=None) -> TrackedLink
links.link_problems(utskick) -> [str]        # Granska blocks: PENDING_TEXT, "ADX har inte godkänt ..."
links.check_destinations(account, urls) -> {url: True | False | None}   # None = not checked
links.build_destination(link, recipient=None, click=None); links.bare_destination(link)
links.rollup(now, deadline=None) -> {"links"}; links.site_urls()   # reverse() to adx.se on a link host
attribution.resolve(ut, campaign) -> Click | None; record_lp_visit; record_beacon; attach; mark_called
attribution.classify(request, recipient, link) -> "bot" | "human" | "scanner"; record_click; count_bot
tokens.ut_token(click_id) / read_ut; form_nonce(code) / read_form_nonce; undo_nonce / read_undo
optin.offers_sms(); sms_signup_block(account) -> "" | text; sms_ready(now); requeue_sms(consent)
optin.sms_queued(now); sms_work_exists(now); send_due_sms(now, deadline, limit); confirm_sms_text
link_actions.confirm(code, *, text_shown, ip_hash) -> "klar" | ...   # also purpose start
```

Behaviour worth knowing:
- Click: HEAD gives the bare destination and logs nothing; bots and previews (analytics
  `is_bot`, empty UA, `attribution.EXTRA_BOTS`) count on Recipient and TrackedLink and get no
  `ut`; 20 Click rows per recipient, link and hour, then `repeat_count`; 3 distinct links of one
  recipient within 2 s make the third a scanner (a later LP beacon upgrades it). More than 20
  misses per `ip_hash` and hour give 429 for everything from that visitor (also real codes), on
  the click and on `/s/`, `/p/`, `/b/`.
- A link code without a recipient (B's test sms) redirects to `build_destination(link)` without
  a Click, counters or `ut`; `/s/` and `/p/` work by `(account, value_hash)` for it.
- `ut` is resolved only against a Click of the page's account; a foreign or tampered token is
  ignored completely. A lead gets `utskick`, `utskick_recipient` and `attribution` (`click`,
  `utskick`, `name`, `channel`, `link`, `label`, `clicked_at`, `contact_matched`, `late`), and
  `Lead.contact` only when the form's phone or email is the recipient contact's and
  `can_collect`. At most 3 attributed leads per click and hour (more are normal leads).
  Utskick leads count against `UTSKICK_LEADS_PER_CAMPAIGN` (200) and never against the normal 30.
- `/s/` adds `Suppression(reason link, utskick=<the code's recipient's utskick>)` and sets
  `Recipient.stopped_at` (A's D.9 request). Ångra (30 min) restores the consent from the log
  (`consent.restore`) and clears `stopped_at`; it is offered only when this POST created the
  suppression (never after a STOPP). Without a contact (erased) the suppression and a ConsentLog
  row without contact are written by hash.
- `/b/` with purpose `start` and no contact lifts the suppression by `(account, value_hash)`
  (D's request). Confirm codes for signup and `pref_on` come from `optin.send_due_sms` (pending
  sms consents, the same queue fields as DOI), sent from the reply number with source `system`
  and reference `b<code pk>`; skipped at the cap or without an enabled SmsAccount; only with
  `Switchboard.sms_enabled` and no breaker.
- The signup page offers sms when `optin.sms_signup_block(account)` is "" (switch on and the
  customer's sms enabled); sms consent is `pending` until the `/b/` click (K.2.5). The settings
  page posts `kanaler_visade=1` plus `kanal`; without the marker the channels are kept.

Deviations (links):

1. `links.clean_external` takes `allow_pending`; `add_link` stores a link to a new host and
   calls `request_host` (Granska blocks through `link_problems`). `check_destinations` returns
   `None` for a URL it did not check (the 10 runs per hour limit or the 15 s budget).
2. Retention of Click and LinkCode is `retention.purge_s2` (A); no second implementation in
   `links.py`.
3. HTML pages on the link hosts send `Cache-Control: private, no-store` (personal pages) but
   keep `Referrer-Policy: same-origin`; only the 302s get `no-referrer`.
4. The botcheck on `/p/` sees the path without the code (`_PathWithoutCode`), so a failed
   botcheck never logs a person code.
5. `/p/` "Anmäl dig" exists for email only (a person code always has a number); the address
   becomes the contact's only when no other contact has it, else that contact's email waits for
   the DOI click (signup-page semantics, no enumeration).
6. ConsentLog rows written by `consent.set_status` do not carry `utskick` (set_status has no
   such argument); the Suppression does.
7. The link-host port-80 block redirects with `return 301` at server level like the primary
   block; verify that `certbot --nginx` HTTP-01 passes for `adx-links` (checklist step 4).

## Requests from links

- **A (daily):** call `links.rollup(now)` from `utskick_daily` too: the tick only rolls up on
  worked ticks, and the 2-day window lets one daily run catch every click.
- **B:** `rules.three_things` (pending hosts) and `numbers_for` are in place; the link picker
  should check a foreign campaign id with `owned_ids` before `add_link` (which also refuses it
  with `LinkRefused`).
- **Lead / README:** fold the deviations above into "S2 as built"; checklist step 2 also needs
  the link hosts' static files (nginx serves `/static/` there; the pages load
  `utskick-public.css` and, on `/p/`, `utskick-public.js`).

## Utskick UI (done by the utskick-ui-byggaren)

Files: `app_views/utskick.py` (every view in the route table), `composer.py` (whole module, the
task moved it from A to B), `reports.py`, `templates/flamingo/app/utskick/{list,step_mottagare,
step_kanal,step_innehall,step_tid,step_review,report,recipients,settings}.html` plus partials
`_head`, `_step_header`, `_wizard`, `_step_actions`, `_phone`, `_recipients_table` (`_stub.html`
deleted), `static/css/flamingo-app-utskick-views.css` (`.fl-ut-`, the file the table above names;
`flamingo-app-utskick.css` is untouched), `static/js/flamingo-app-utskick.js`, `test_s2_ui.py`.
Outside the app (marked blocks): `apps/flamingo/rules.py` (`utskick_things` first in
`three_things`), `apps/flamingo/app_views/overview.py` (`Numbers.google_leads/google_deals/
utskick_leads/utskick_deals`, `leads` without `source="reply"`, kr per lead/deal on Google leads
only) and `templates/flamingo/app/overview.html` (the "Varav via utskick" line), shared
`timeline.py` (S2 sources: Recipient, human Click, inbound ThreadMessage; titles for the S2
Event kinds), shared `app_views/contacts.py` (`LAST_LABELS` S2, `_summary` "6 utskick · 2 klick ·
1 förfrågan"), shared `apps/flamingo/test_demo.py` (route walk: list, settings, report and
recipients per utskick, the steps for editable ones), foundation
`test_s2_foundation.UrlWiringTests.test_every_route_answers_for_the_own_account` (accepts 302 and
400 now that the views are real).

```python
composer.placeholders(body) -> Placeholders(tags, links, unsubscribe, unknown)
composer.validate(account, body, utskick=None) -> [str]   # with utskick: {länk:x} must exist
composer.merge_values(contact, defs=None) -> dict         # what freeze stores on Recipient.merge
composer.merge(text, values, fallbacks); render_sms(utskick, recipient, sender)
composer.render_test_sms(utskick, address, values, sender)  # codes with recipient=None
composer.preview(utskick, contact=None, sender=None, body=None, with_longest=True, sms_account=None)
composer.opt_out_line(sender_kind, person_code=None); sender_for_kind(u); is_reply_sender(s)
composer.gsm_fix(text); TEMPLATES; template_body(key, display_name); part_units(sms_account)
reports.summary(u, now=None); final_stats(u); list_numbers(rows); recipients_for(u, view)
app_views.utskick.review(request, account, u, now) -> {"items", "blocking", "summary", ...}
```

Behaviour worth knowing:
- Every step is one form; every button saves first (Tillbaka, Spara utkast, Nästa, a template,
  a link, Byt automatiskt, a test sms). A POST to a scheduled utskick calls `state.unconfirm`.
  Running, paused and finished utskick are read-only (steps redirect to the report), except
  Granska for a pause in `RECONFIRM_REASONS`.
- Granska issues the nonce (`state.issue_nonce`) on every GET; the confirm view checks it, the
  staff checkbox (`som_adx`) and re-runs every blocking check before `state.confirm(actor=
  access.Actor)`. `confirm_summary` = `{"sms", "email", "skipped", "skipped_by_reason", "total",
  "parts", "longest_parts", "longest_count", "cost_units", "remaining_units", "send_now",
  "scheduled_at", "purpose", "sender"}`.
- The test sms goes out in the request through `sms_wrapper.send(source="test")` to the
  account's `notify_phone` (customer) or a number staff types (staff only); to the customer staff
  must tick the box. Refused for the demo, without sms, before `sms_enabled`, under the breaker,
  with text errors or a missing company name, to a suppressed number; 10 per account and
  Stockholm day (`Counter` scope `test_send`); `Event(test_send)` on a matching contact.
- The count JSON takes the unsaved audience (`?urval=1&lists=..`, through `audience.clean`, 400
  for a foreign id); the sms preview JSON takes the unsaved text (POST `sms_body`) and returns
  `notes` ([{level, text}]) and `counter` for the editor.

Deviations (UI):

1. CSS lives in `flamingo-app-utskick-views.css` (this file's table), not appended to the shared
   `flamingo-app-utskick.css` as the task text said.
2. "Skicka test till kunden (kunden får sms:et)" is a `<details>` with the I.4 checkbox and a
   "Skicka som ADX" button (works without JS); Skicka nu in Granska is a real `<dialog>`, and
   without JS the same sentence stands above the button.
3. Purpose (Reklam, Information with the reason) is on the Kanal step (contract I.8), not on
   Mottagare as in the mockup.
4. The opt-out line is added to every utskick sms, information too (H.5 requires it above one
   recipient; the composer does not know the count per render). With a name sender the
   composer removes a typed "Svara STOPP för att inte få fler sms." (it cannot be answered).
5. A raw `http(s)://` or `www.` address in the text is a validation error: links go through
   `{länk:x}` (tracked, E.8 host rules).
6. "Byt kontakt" cycles through the first 50 audience contacts; the test sms uses the shown
   contact's values.
7. Pause from the app: a customer pause is reason `customer`, staff in view-as is `staff`.
   Fortsätt for staff-only pauses is shown to staff only; a reconfirm pause goes to Granska.
8. Cost estimates use `recent_part_cost("SE")` or 5 200 per part plus the account's markup
   (same rule as `state.cost_estimate`).

## Requests from composer-ui

- **C (links):** test-sms codes are real `LinkCode` rows with `recipient=None` (kind `link` with
  `link`, kind `person` with `value_hash` of the test number). The click view should redirect
  to `build_destination(link)` without counting, and `/s/` should work by `(account,
  value_hash)` as for an erased contact.
- **A (freeze):** freeze `Recipient.merge` with `composer.merge_values(contact, defs)` (date
  fields formatted, empty values left out) so the preview, the test and the real sms agree.
- **Lead:** README I.9 says "Tak per kontakt" for sms and email; the email row (S3) is already
  editable on the settings page because the field exists. 46elks part price: verify the 5 200
  fallback against a live dryrun when the S2 checklist runs.

## Integration (done by the integration step)

Every "Requests from ..." above, and where it landed:

- **Engine -> integration**: the S2 demo (`demo.seed` / `demo.reset`, through the real engine,
  routing and STOPP code), the `test_demo.py` extension (S2 content, idempotent counts incl. the
  S2 rows, the page walk with `apps.sms.elks._post` failing, the tick simulating the demo's
  scheduled utskick, the sending buttons refusing the demo) and `test_s2_info.py`. The
  `test_demo` source exception for `reply` is gone.
- **Engine -> D, C** (`Recipient.stopped_at`, `Suppression.utskick` on STOPP and `/s/`): done by
  D and C; `test_s2_flow.StopFlowTests` checks it end to end.
- **Engine -> B, inbound -> B, links -> B** (cap banner numbers, "Senast" labels, `numbers_for`
  without replies, foreign campaign id through `owned_ids` in the link picker): done by B.
- **Inbound -> A** (demo threads through the `threads` helpers, `demo.reset` deleting reply leads
  and inbound sms, 90-day inbound retention): demo done here; retention was already in
  `retention.purge_s2`. The two `test_s2_foundation` rows were updated by A.
- **Inbound -> C** (`/b/` with purpose `start` and no contact): done by C.
- **Links -> A** (`links.rollup` from `utskick_daily`): done here (`retention.daily` step `links`).
- **Composer-ui -> C, A** (test-sms codes without recipient; freeze with
  `composer.merge_values`): done by C and A.
- **Every "Lead / README" request**: folded into README "S2 as built" (incl. the checklist notes).

Also added here: `test_s2_flow.py` (the whole path with the real views, tick and apps/sms),
`threads.rows` labels an utskick sms without an SmsMessage (the demo's) "Utskick: <namn>",
`flamingo_demo` sets `Lead.activity_at = created_at` on the demo's leads (the Inkorg sorts on it),
the manage overview links its S2 sections, the nginx link block sets `X-Robots-Tag` itself and
hides `Set-Cookie`, Sentry masks scheme-less `k.adx.se/...` links, and `test_s1_guards` walks
`templates/utskick/links/` too. The flamingo README describes the S2 Inkorg and attribution.
