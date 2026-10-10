# S3 handoff: the foundation is in, four builders finish S3

Written by the S3 foundation agent on 2026-10-10. The contract is `README.md` in this folder
(read "Fixed decisions", "Naming rule", "Deviations", "S1 as built", "S2 as built", A, B.0, B.3,
B.7, C (S3 parts), D.6 to D.9, E.1, E.2, E.5, E.7, F, G.2, G.3, H (H.8 above all), I and J S3).
This file says what exists now, who owns what, which helpers to call and which signatures to
implement. `S1-HANDOFF.md` and `S2-HANDOFF.md` still describe the S1 and S2 helpers (access,
consent, suppression, contacts, keys, limits, alerts, timeline, optin, tokens, links, threads,
sending/*, composer, reports). Delete this file when S3 ships and fold the deviations into README
("S3 as built").

The approved email design is `mockups/flamingo-epost-brev.html`: the Brev style with all 24
elements, logo left, center or none, one accent colour per utskick (a light colour gives black
button text and darker links). The rendered mail must match it closely (tables, inline styles,
560 px, mobile stacking). No other style exists. `mockups/flamingo-utskick.html` has the app
views (editor, Leveranshälsa, Egen domän, settings rows).

Production facts for S3: SES eu-west-1 has production access (50 000 per day, 14 per second); the
identity `utskick.adx.se` is verified (DKIM, MAIL FROM `bounce.utskick.adx.se`, DMARC
`p=quarantine`); the role `adx-utskick` exists with the S1 policy (send from `utskick.adx.se`,
`GetAccount`) and the server assumes it through `apps/utskick/aws.py`; `k.adx.se` and
`klick.adx.se` are live with TLS. The S3 resources (configuration set, SNS and SQS, receiving, the
S3 bucket, the extended IAM policy) do **not** exist yet: `server/aws-utskick-s3.sh` and the
extended `server/aws-utskick-role.sh` create them when the lead runs them. Every S3 code path
must work with them absent: empty `UTSKICK_SQS_*` and `UTSKICK_SES_INBOUND_BUCKET` mean "off",
and nothing sends email utskick until `Switchboard.email_enabled` (D.8).

## What the foundation built

- **Migration `utskick/0003_brev_och_epost.py`** (applied to the local dev database). Only the
  foundation edits `models.py` and migrations in S3; ask the lead if a field is missing.
  - `Utskick`: `subject`, `preheader`, `email_doc`, `email_rev`, `accent`, `logo_position`
    (`Utskick.LogoPosition`), `sender_domain` (FK `SenderDomain`, **RESTRICT**, see deviations),
    `from_name`, `confirmed_terms`, `terms_confirmed_by`, `terms_confirmed_at`, `open_tracking`,
    `text_override`, and `email_snapshot` (new, see deviations). Every column has `default` and
    `db_default` (B.0); the FKs are nullable. Properties `has_email`, `has_sms`.
  - `UtskickSettings`: the D.9 account email block `email_blocked_at`, `email_blocked_reason`
    (`db_default ""`), `email_released_at`, `email_released_by` (new, see deviations).
  - `Switchboard`: `ses_account` (JSON, `db_default {}`), `ses_checked_at` (new).
  - New tables `SenderDomain` (B.3; `Status`, `PENDING_DAYS = 14`, `CLAIMING`, properties
    `from_address`, `mail_from_domain`, `is_verified`; partial unique `utskick_domain_verified`
    on `domain` where verified; indexes on `(account, status)`, `domain`, `(status, created_at)`),
    `EmailImage` (B.3; `Purpose`, `Format`, file under `MEDIA_ROOT/utskick-img/<random>/`, the file
    is removed on commit when the row is deleted, partial unique `(asset, purpose, width)` where
    asset is set), `EventReceipt` (`key` unique, `at` indexed, `KEEP_DAYS = 3`).
  - `Event.OPENED = "opened"`, `Event.S3_KINDS`.
  - The migration ends with `dbfk.apply` for every new FK (`S3_FOREIGN_KEYS`): CASCADE and
    SET NULL are in Postgres too, so S2 code (a rollback) deleting an account, a media asset or a
    user never hits an `IntegrityError`. `DbOnDeleteTests` in `test_s2_foundation` and
    `test_s3_foundation` guard it.
- **Settings (C.4 S3)** in `config/settings/base.py` and `.env.example`:
  `UTSKICK_SES_CONFIGURATION_SET` (`adx-utskick`), `UTSKICK_ADX_MONTHLY_MAIL_CAP` (2000),
  `UTSKICK_EMAIL_PER_SECOND` (10), `UTSKICK_REPLY_DOMAIN` (`svar.utskick.adx.se`),
  `UTSKICK_SES_INBOUND_BUCKET`, `UTSKICK_SQS_EVENTS_URL`, `UTSKICK_SQS_INBOUND_URL` (empty = off).
  `config/test_runner.py` also blanks `UTSKICK_AWS_ROLE_ARN`, both SQS URLs and the bucket for the
  whole test run (a developer `.env` never reaches AWS from a test).
- **Tokens (E.2), implemented and tested** in `tokens.py` (section "S3"): click, unsubscribe,
  web view, pixel, calendar, the reply/thread/mailto local parts and the own-reply-address
  confirmation. Formats in the module docstring.
- **URL builders, implemented and tested** in `links.py` (router section "S3"):
  `email_link_base`, `email_url`, `unsubscribe_url`, `email_preferences_url`, `web_view_url`,
  `pixel_url`, `calendar_url`, `mailto_unsubscribe`. `email/mime.py` got
  `unsubscribe_headers(https_url, mailto_url)` and `ONE_CLICK`.
- **Small foundation helpers, implemented**: `inbound/queues.py` (`queue_url`, `events_enabled`,
  `inbound_enabled`, `enabled`, `dlq_url`, constants), `inbound/events.py` (`EVENT_TYPES`,
  `receipt_key`, `recipient_tags`), `email/style.py` (`ACCENT_RE`, `DEFAULT_ACCENT`, `SWATCHES`,
  `LIGHT_TEXT`, `valid_accent`), `email/checks.py` (`Item`, `blocking`, `as_json`, levels,
  `MAX_HTML_BYTES`), `email/registry.py` (`BLOCK_KEYS` in library order, `DOC_ELEMENTS`,
  `OFFER_KEYS`, `MAX_BLOCKS = 30`, the field-kind names), `email/transport.py` (the S3 kind
  constants `UTSKICK`, `TEST`, `REPLY`, `PROBE`, `REPLY_CONFIRM`, `S3_KINDS`; `KINDS` is still
  only `doi`).
- **URL wiring for every S3 route** (tables below) pointing at stubs: GET pages render
  `templates/flamingo/app/utskick/_stub.html` with the tab row (`app_views.render_stub`), POST
  and JSON answer 501 (`app_views.not_built`), a foreign utskick 404, utskick off 404. The six
  link-host views answer the "Länken har gått ut" 404 until built. Manage stubs answer 501.
- **Skeleton modules** with every name and signature other builders call (each function raises
  `NotImplementedError("STUB: ...")` and names its owner; the owner replaces the whole module,
  keeping the names): `email/{registry,blocks,style,render,text,images,checks,domains}.py`,
  `inbound/{queues,events,email}.py`, `sending/{email,health}.py`, `ai.py`,
  `app_views/{brev,health,domain}.py`, `manage_email.py`. `test_s3_foundation.SignatureTests`
  pins the names and leading parameters; keep it green (extend it if you add callers).
- **Nav**: the Utskick tab row is Utskick, Leveranshälsa, Inställningar (`nav.UTSKICK_TABS`, tab
  key `health`); `render_utskick` accepts `"health"`.
- **Manage overview hook**: `manage_views.overview` merges `manage_email.panel_context(now)`;
  `overview.html` includes `manage/utskick/_overview_email.html` (an empty stub partial).
- **static_version** already lists `css/flamingo-app-brev.css`,
  `css/flamingo-app-utskick-email.css` and `js/flamingo-app-brev.js` (missing files are
  skipped), so no builder edits `apps/manage/context_processors.py`.
- **AWS scripts (not run)**: `server/aws-utskick-role.sh` now writes the whole H.8 policy;
  `server/aws-utskick-s3.sh` creates the S3 resources idempotently and prints the `.env` lines.
  Both were run against a fake `aws` (`test_s3_foundation.ScriptTests`): valid JSON everywhere,
  the same names in both scripts.
- **Tests**: `apps/utskick/test_s3_foundation.py` (55 tests). Existing tests changed on purpose:
  `test_s1_views` (the route walk knows `app_brev*` take an utskick pk and
  `app_utskick_reply_confirm` takes a token), `test_s2_ui.GuardTests` (no longer asserts that
  `utskick/_stub.html` is absent; the builders delete the file with the last stub and may put
  the assertion back).

## The four builders and what they own

One owner per file. "Shared" files: append in a marked block, do not change behaviour others rely
on, say so in your report. Never edit `models.py`, migrations, settings, `config/urls*.py`,
`app_urls.py`, `manage_urls.py`, `apps/flamingo/urls.py` or the middleware list: ask the lead.

### A. Brev and the renderer ("brev-byggaren")

- Python: `email/registry.py` (the 22 `EMAIL_TYPES` with `pagebuilder.registry` Field, Variant,
  BlockType; `available`, `library`), `email/blocks.py` (validation, the new field kinds,
  `rich_basic`, `save` with `email_rev`, terms), `email/style.py` (palette and `brev_styles`),
  `email/render.py`, `email/text.py`, `email/images.py`, `email/checks.py`.
- Outside the app: `apps/flamingo/pagebuilder/blocks.py` and `apps/flamingo/media.py` (the
  `types=` parameter of F.1, default the page `TYPES`, page behaviour unchanged;
  `media.delete_asset` asks `email.images.uses` and raises `MediaInUse`, C.2),
  `apps/website/tests.py` `VvsLegacyGuardTests` allow-list (the second review source of F.1 #12 is
  the review site that guard blocks: add `apps/utskick/email/registry.py`, `email/blocks.py`, the
  block template and the test files that name it, by name).
- Templates: `templates/utskick/brev/layout.html`, `templates/utskick/brev/blocks/<key>.html`
  (inline styles from `S`, tables, no `<script`, no `class=`-dependent layout, no external fonts,
  no relative URLs). They are outside `templates/flamingo/`, so the "no `style=`" guard does not
  apply; write your own guard in `test_s3_render.py`.
- Tests: `test_s3_registry.py`, `test_s3_render.py` (the `.t-brev` tokens parsed from the mockup,
  560/28/20/30, mso wrapper, text version, merge fallbacks, single-line merge, per-recipient
  links, 102 kB, accent light and dark, pixel only for `tracking_ok`), `test_s3_media.py`
  (delete blocked by a draft, allowed after send, rendition survives),
  `apps/flamingo/test_pagebuilder*.py` stay green.

### B. The editor and the utskick UI for email ("redigerar-byggaren")

- Python: `app_views/brev.py` (every `app_brev*` view), `ai.py` (F.7), the S3 parts of
  `app_views/utskick.py` (shared S2 module, marked blocks): email modes on the Kanal step (locked
  with "E-post är inte påslaget än." while `state.email_live()` is false, I.8), the E-post tab on
  Innehåll linking to the editor, the S3 rows in Granska (`email.checks`, ADX mail cap via
  `sending.email.adx_cap_left`, I.6), the email test send in `utskick_test` through
  `sending.email.send_test` (F.8), the email variant of the report (I.8) with `reports.py` email
  numbers (shared S2 module), the S3 settings rows of I.9 on `utskick_settings` (the domain card
  from `email.domains.summary`, links to Egen domän for the domain and the reply address, the
  open-tracking toggle with "Av som standard. Öppningar mäts bara hos dem som sagt ja till det när
  de anmälde sig.").
- Outside the app: `static/js/flamingo-pb.js` and `apps/flamingo/app_views/pages.py`
  `editor_config` (the profile of F.6; the page profile must behave exactly as before and a test
  asserts the page config is unchanged: `apps/flamingo/test_pagebuilder_editor.py`).
- Templates: `templates/flamingo/app/utskick/brev_editor.html` plus partials (`_brev_*`), the S3
  parts of `step_kanal.html`, `step_innehall.html`, `step_review.html`, `report.html`,
  `settings.html` (shared S2 templates, marked blocks).
- Static: `static/css/flamingo-app-brev.css` (`.fl-br-`), `static/js/flamingo-app-brev.js`.
- Tests: `test_s3_editor.py` (tenancy for every `app_brev*` route, foreign media and domain ids
  give 400, 409 on a stale `rev`, a scheduled utskick becomes a draft, staff checkbox),
  `test_pagebuilder_editor.py` (page profile unchanged), the `test_demo.py` route list.

### C. Sending, events, health and domains ("sändnings-byggaren")

- Python: `sending/email.py` (tick phase 6, D.6, `deliver`, `send_test`), `sending/health.py`
  (D.9), `email/transport.py` and `email/mime.py` (S3 parts, shared S1 modules), `email/domains.py`
  (B.3, J S3 domain flow), `inbound/queues.py` (D.7: the poll loop, receipts, DLQ),
  `inbound/events.py` (`apply`), `app_views/health.py`, `app_views/domain.py` (Egen domän and the
  own reply address with its confirmation link), `manage_email.py` (health release, domain admin,
  queues and DLQ, `panel_context`).
- Shared S2 modules, marked blocks: `sending/tick.py` (phase 2 `queues.poll` when
  `queues.poll_due`, phase 6 `email.send_due`, `work_exists`), `sending/freeze.py` (email
  recipients get `basis` and `tracking_ok` as today; at the end of the freeze
  `render.snapshot` into `Utskick.email_snapshot` and the TrackedLinks from
  `render.collect_links`, recorded in `email_snapshot["links"]`; media ids through `owned_ids`
  again), `sending/state.py` pre-checks (ADX mail cap `paused_cap adx_mail_cap`,
  `email_disabled`, `account_health`, sender domain), `sending/recover.py` (D.5 email: a stale
  `sending` email becomes `unknown`, never resent), `retention.py` (`purge_s3`: EventReceipt after
  3 days, `email.images.purge_unused`), `management/commands/utskick_daily.py` (GetAccount via
  `health.adx_wide`, `domains.check_due`, `queues.check_dlq`, `inbound.email.sweep_bucket`,
  `email.stale_unknown`), `manage_sending.probe` ("Provmejl till mig", J S3 step 7),
  `manage_views.customer_update` if the card needs the email block, `manage_views.end_account`
  ("Avsluta utskick och radera allt" also removes the S3 rows: EmailImage, SenderDomain with
  `DeleteEmailIdentity` only when `ses_created`; utskick go first, the FK is RESTRICT),
  `alerts.py` additions, `optin.py` (the DOI mail gets the configuration set and tags from S3).
- Templates: `flamingo/app/utskick/health.html`, `flamingo/app/utskick/domain.html` (DNS
  records as `.fl-kv` blocks with one Kopiera per value, not a table), the reply-confirm page,
  `manage/utskick/_overview_email.html`, `manage/utskick/domain.html`, `manage/utskick/queues.html`
  (every table with `<thead>`).
- Static: `static/css/flamingo-app-utskick-email.css` (`.fl-ut-em-`); `static/css/manage-utskick.css`
  section `.mu-em-` only if the panel classes are not enough.
- Server: none beyond what the foundation wrote; change the two AWS scripts only together with
  `test_s3_foundation.ScriptTests`.
- Tests: `test_s3_transport.py` (FakeSes: raw MIME headers, List-Unsubscribe and Post,
  configuration set, tags, `max_attempts=1`, throttling requeue, timeout and 5xx to unknown and
  never resent, AccountSendingPaused, adoption, demo refused), `test_s3_queues.py`,
  `test_s3_events.py` (hard, soft x5, complaint, `OnAccountSuppressionList`, thresholds, probe
  hold, daily cap, account block and release), `test_s3_domains.py` (dnspython mocked),
  `test_s3_caps.py` (ADX 2 000 cap incl. tests).

### D. Inbound mail, Inkorg and the link-host email pages ("inkorg-byggaren")

- Python: `inbound/email.py` (G.3), the S3 section of `link_views.py` (`email_click`,
  `email_unsubscribe` with the one-click POST, `email_preferences`, `web_view` with the F.4 CSP,
  `open_pixel`, `calendar`; the section is marked, the S2 views above it are unchanged),
  `link_actions.py` (email unsubscribe and preference rules, shared S2 module), the email parts of
  `threads.py` (shared S2 module: email threads, leads, `In-Reply-To`), the email reply in
  `app_views/inbox_reply.py` (G.2, through `sending.email.deliver(kind=transport.REPLY)` with
  `Reply-To` from `tokens.reply_address(tokens.THREAD, ...)`), the S3 sources of `timeline.py`
  (email delivered, "Öppnade (indikation)", bounced) and the email "Senast" labels in
  `app_views/contacts.py` (shared, marked blocks), and the S3 parts of `contacts.export_contact`
  and `contacts.delete_contact` (H.4: email threads and their reply leads, `InboundMessage`
  email rows blanked; shared S1 module, marked blocks).
- Outside the app: `apps/flamingo/app_views/inbox.py` and its templates for email threads
  ("E-postsvar" chip, attachment note), `static/css/flamingo-app-utskick-thread.css` (email
  parts).
- Templates: `templates/utskick/links/{email_unsubscribe,email_preferences,...}.html` extending
  `utskick/links/_base.html` (never `{% csrf_token %}`), `templates/flamingo/app/utskick/_thread.html`
  (email parts).
- Tests: `test_s3_inbound_email.py` (bucket pin, token from the receipt recipients first, unknown
  token never fetches, autoreply, spam, size, quote stripping, mailto unsubscribe with and without
  the recipient row, hourly cap), `test_s3_one_click.py` (POST without CSRF unsubscribes, GET
  does not, works after the recipient row is gone), `test_s3_link_check.py` if the shared link
  checker changes, link-host page tests with `Client(enforce_csrf_checks=True)`.

## Implemented helpers (call these)

### tokens.py (S3 section)

```python
email_click_token(recipient_id, link_id) -> "<r62>.<l62>.<sig8>"   # recipient 0/None = test mail
read_email_click(token) -> EmailClickRef(recipient_id | None, link_id) | None
unsubscribe_token(account_id, value_hash) -> "<a36>.email.<hash43>.<sig16>"   # never expires
read_unsubscribe(token) -> PreferenceRef(account_id, "email", value_hash) | None
preference_token(account_id, "email", value_hash)   # S1; the same token works on /v/ and /utskick/val/
web_view_token(utskick_id, recipient_id=None); read_web_view(token) -> WebViewRef | None
pixel_token(recipient_id); read_pixel(token) -> recipient_id | None
calendar_token(utskick_id, block_id); read_calendar(token) -> CalendarRef | None   # block id b_ + 12
REPLY, THREAD, MAILTO = "r", "t", "u"; REPLY_KINDS
reply_token(kind, account_id, object_id) -> "r1a.f4x<sig10>"   # lower case only, fits 64 octets
read_reply_token(token) -> ReplyRef(kind, account_id, object_id) | None   # case-insensitive
reply_address(kind, account_id, object_id) -> "s+<token>@svar.utskick.adx.se"
read_reply_address(address) -> ReplyRef | None   # only on UTSKICK_REPLY_DOMAIN
reply_domain() -> "svar.utskick.adx.se"
reply_confirm_token(account_id, address, now=None); read_reply_confirm(token, address, now=None)
    -> account_id | None   # bound to the address, REPLY_CONFIRM_DAYS = 7
```

Every signature uses `UTSKICK_LINK_KEY` with its own purpose prefix, so a token of one kind never
works as another (tested). The kinds of `ReplyRef`: `r` = reply to an utskick (object id =
recipient), `t` = reply in an Inkorg thread (object id = thread), `u` = mailto unsubscribe (object
id = recipient). When the recipient row is gone, fall back to the account in the token plus the
`From` address (G.3).

### links.py (router section "S3")

```python
email_link_base() -> "https://klick.adx.se"   # UTSKICK_EMAIL_LINK_BASE; locally http://klick.localhost:8770
email_url(utskick, recipient, link) -> ".../m/<token>"   # recipient None = test mail
unsubscribe_url(account_id, value_hash) -> ".../a/<token>"
email_preferences_url(account_id, value_hash) -> ".../v/<token>"
web_view_url(utskick_id, recipient_id=None) -> ".../w/<token>"
pixel_url(recipient_id) -> ".../o/<token>.gif"
calendar_url(utskick_id, block_id) -> ".../c/<token>.ics"
mailto_unsubscribe(account_id, recipient_id) -> "mailto:s+u...@svar.utskick.adx.se?subject=avregistrera"
```

### email/mime.py, inbound/*, sending/state.py

```python
mime.unsubscribe_headers(https_url, mailto_url) -> {"List-Unsubscribe": "<https>, <mailto>",
                                                     "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
mime.build(mail) / mime.parse(raw) / mime.text_part(message)   # S1; parse is the only stdlib parser
queues.queue_url("events" | "inbound"); events_enabled(); inbound_enabled()   # inbound also needs the bucket
queues.enabled(); queues.dlq_url(url) -> url + "-dlq"; QUEUES, POLL_IDLE, POLL_BUSY_AFTER_SEND
events.receipt_key(event) -> "<mail.messageId>:<eventType>" | ""; events.recipient_tags(event) -> (a, u, r)
events.EVENT_TYPES   # what the configuration set publishes (no OPEN, no CLICK)
state.email_live()   # S2: UTSKICK_EMAIL_LIVE and Switchboard.email_enabled (D.8)
state.EMAIL_OFF_TEXT # "E-post är inte påslaget än."
```

## Stub signatures to implement (callers rely on these exact names)

The docstring of each module describes the behaviour; the leading parameters are pinned by
`test_s3_foundation.SignatureTests`.

| Module (owner) | Signatures | Called by |
|---|---|---|
| `email/registry.py` (A) | `EMAIL_TYPES`, `get_type(key)`, `available(account, utskick) -> {key: (ok, why)}`, `library(account, utskick) -> [dict]` | B (editor), A |
| `email/blocks.py` (A) | `StaleRevision`, `BlockError(errors)`, `new_block(type_key, account, utskick, *, user=None, now=None)`, `validate(account, utskick, blocks)`, `save(utskick, blocks, *, rev, user=None, account=None, now=None) -> new rev`, `clean_url(account, value, *, allow_pending=True)`, `parse_rich(text, *, bold_only=False)`, `media_ids(blocks)`, `urls(blocks)`, `terms_from(blocks)`, `active_blocks(utskick, doc=None)` | B, C (freeze), A |
| `email/style.py` (A) | `default_accent(account)`, `accent_for(utskick)`, `picker(account)`, `palette_for_accent(value)`, `brev_styles(palette)` | A, B (picker), D (link-host logo colour if wanted) |
| `email/render.py` (A) | `RenderContext`, `LinkSpot`, `context_for(utskick, *, mode, recipient=None, contact=None, test=False, snapshot=None)`, `render_html(utskick, ctx, mode=None)`, `render_block(utskick, block, *, ctx=None)`, `snapshot(utskick, *, now=None)`, `collect_links(utskick, doc=None)`, `html_size(utskick)`, `web_view(utskick, recipient=None)`, `calendar_ics(utskick, block_id)`, `subject_for(utskick, ctx)`, `preheader_for(utskick, ctx)` | B (editor, preview), C (freeze, send, test), D (`/w/`, `/c/`) |
| `email/text.py` (A) | `render_text(utskick, ctx)`, `default_text(utskick, ctx)` | C, B |
| `email/images.py` (A) | `rendition(asset, purpose, *, width=MAX_WIDTH)`, `logo_for(account)`, `absolute_url(image)`, `uses(account_id) -> set`, `purge_unused(now=None)` | B (`app_brev_image`), apps/flamingo media, C (retention) |
| `email/checks.py` (A) | `email_checks(utskick, *, now=None, contact=None) -> [Item]` | B (`app_brev_checks`, Granska) |
| `ai.py` (B) | `make_utskick_guard(account, utskick)`, `write_sms(utskick, *, user, brief="")`, `write_block(utskick, block_type, *, user, brief="", block=None)`, `WRITE_SMS_TOOL`, `WRITE_BLOCK_TOOL`, `AiResult` | B |
| `sending/email.py` (C) | `work_exists(now)`, `send_due(now, deadline, only=None)`, `claim`, `process`, `adx_cap_left(account, now=None)`, `adx_month_count(account, now=None)`, `from_for(account, utskick=None, sender_domain=None)`, `reply_to_for(account, recipient=None, thread=None)`, `deliver(account, mail, *, kind, utskick=None, recipient=None, now=None) -> transport.Sent`, `send_test(utskick, *, address, contact=None, actor, now=None)`, `simulate(account, now, only=None)`, `stale_unknown(now=None)` | C (tick), B (test send, Granska cap), D (inbox replies) |
| `sending/health.py` (C) | `utskick_health`, `check_utskick`, `account_health`, `check_account`, `release(account, *, actor, now=None)`, `is_blocked(account)`, `probe_state`, `daily_cap_left`, `adx_wide` | C, B (report and Leveranshälsa numbers) |
| `email/domains.py` (C) | `normalize(raw)`, `claim_problem(account, domain)`, `claim(account, domain, *, from_local, from_name, user, now=None)`, `records(row)`, `check(row, *, now=None, alert=True)`, `check_due(now=None, deadline=None)`, `remove(row, *, user, now=None)`, `sendable(account, domain_id)`, `summary(account)`, `IN_USE_TEXT`, `DomainRefused`, `Record` | C, B (settings row, editor's sender choice) |
| `inbound/queues.py` (C) | `poll_due(now)`, `poll(now, deadline) -> dict`, `dlq_counts()`, `redrive(which)`, `check_dlq(now=None)` | C (tick, daily, manage) |
| `inbound/events.py` (C) | `apply(event, now=None) -> str` | C (`queues.poll`) |
| `inbound/email.py` (D) | `receive(notification, now=None) -> InboundMessage or None` (inside the queue transaction), `process_pending(now, deadline) -> dict` (after commit), `pending_exists()`, `sweep_bucket(now=None)`, `is_autoreply(message)`, `strip_quotes(text)` | C (`queues.poll`, `poll_due`, daily) |
| `email/transport.py` (C) | `send(mail, *, kind, account_id=None, utskick_id=None, recipient_id=None)` with the configuration set and the tags `k`, `a`, `u`, `r`; `KINDS` gains `S3_KINDS` | only `sending/email.py` and `optin.py` |

`deadline` is always a `time.monotonic()` value; return dicts hold counts only (they go into the
tick summary and `backups/utskick.log`, never addresses).

### The seams between builders

- **Freeze (C) uses the renderer (A)**: `render.collect_links(utskick)` gives the link spots; C
  creates one `TrackedLink` per spot (kind `lp` when the URL is one of the account's Flamingo
  pages, else `external` with `links.clean_external`) with `block_id` and `position`, and stores
  `render.snapshot(utskick)` plus `{"links": {"<block_id>:<position>": link_id}}` in
  `Utskick.email_snapshot`. A's `render_html(..., mode="send")` maps every `href` through that
  table and `links.email_url`. A host that is pending or refused pauses with reason `content`
  (D.3), so `state.content_problems` (S2, shared) must also see the email URLs (C adds them).
- **Send (C)**: per recipient `ctx = render.context_for(u, mode="send", recipient=r,
  snapshot=u.email_snapshot)`, `render.render_html`, `text.render_text`, `render.subject_for`,
  `render.preheader_for`, then `transport.OutgoingMail(..., headers={**mime.unsubscribe_headers(
  links.unsubscribe_url(a, hash), links.mailto_unsubscribe(a, r.pk)), "Reply-To":
  sending.email.reply_to_for(account, recipient=r)})` and `transport.send(..., kind=UTSKICK,
  account_id, utskick_id, recipient_id)`.
- **Test mail (B -> C)**: the editor and `utskick_test` call `sending.email.send_test`; never the
  transport.
- **Inbox reply by email (D -> C)**: D builds the body and the thread message, then
  `sending.email.deliver(account, mail, kind=transport.REPLY, utskick=thread.utskick)` with
  `Reply-To` from `tokens.reply_address(tokens.THREAD, account.pk, thread.pk)` and `In-Reply-To`
  from the inbound `Message-ID` (keep it in `InboundMessage.meta["message_id"]`).
- **Queues (C) and inbound mail (D)**: `queues.poll` writes `EventReceipt(f"in:{messageId}")` and
  calls `inbound.email.receive(notification, now)` in the same transaction, deletes the SQS
  message after commit, then runs `inbound.email.process_pending(now, deadline)`.
- **Link-host pages (D) use A and C**: `/w/` renders `render.web_view(utskick, recipient)` (no
  pixel); `/c/` serves `render.calendar_ics`; `/o/` sets `Recipient.opened_at` (forward only) and
  writes `Event(kind="opened")` once per recipient, only when the recipient carried a pixel.
- **Leveranshälsa and the report (B, C)**: numbers come from `sending.health`; the report tiles
  of I.8 for email (B) read `Recipient` statuses and `opened_at`.

## Routes (all resolve today)

App, namespace `flamingo`, prefix `/flamingo/app/` (`apps/utskick/app_urls.py`), all behind
`@utskick_view` (`@require_POST` or `@require_safe` under it):

| Name | Path | Method | View (owner) |
|---|---|---|---|
| `app_brev` | `utskick/<pk>/brev/` | GET | `brev.brev_editor` (B) |
| `app_brev_save` | `utskick/<pk>/brev/spara/` | POST JSON | `brev.brev_save` (B) |
| `app_brev_render_block` | `utskick/<pk>/brev/rita/` | POST JSON | `brev.brev_render_block` (B) |
| `app_brev_image` | `utskick/<pk>/brev/bild/` | POST JSON | `brev.brev_image` (B) |
| `app_brev_checks` | `utskick/<pk>/brev/kontroller/` | GET JSON | `brev.brev_checks` (B) |
| `app_brev_ai` | `utskick/<pk>/brev/ai/` | POST JSON | `brev.brev_ai` (B) |
| `app_brev_preview` | `utskick/<pk>/brev/forhandsvisning/` (`?lage=mobil|dator|morkt&kontakt=`) | GET HTML | `brev.brev_preview` (B) |
| `app_utskick_health` | `utskick/halsa/` | GET | `health.utskick_health` (C) |
| `app_utskick_domain` | `utskick/installningar/doman/` | GET, POST `action=` | `domain.utskick_domain` (C) |
| `app_utskick_reply_confirm` | `utskick/installningar/svarsadress/<token>/` | GET button, POST confirms | `domain.reply_confirm` (C) |

`app_utskick_test` (S2) is extended for email by B.

Link hosts, `config/urls_links.py`, namespace `links` (`reverse(..., urlconf="config.urls_links")`),
all on `klick` only (`links.on_link_host(KIND_EMAIL)`), views in the S3 section of `link_views.py`:

| Name | Path | Methods | View (owner) |
|---|---|---|---|
| `email_click` | `/m/<token>` (`[A-Za-z0-9.]{12,40}`) | GET, HEAD | D |
| `email_unsubscribe` | `/a/<token>` (`[A-Za-z0-9._-]{40,120}`) | GET page, POST button (form nonce) or `List-Unsubscribe=One-Click` (no nonce, 200 empty body), `csrf_exempt` | D |
| `email_preferences` | `/v/<token>` (same) | GET, POST (form nonce), `csrf_exempt` | D |
| `web_view` | `/w/<token>` (`[A-Za-z0-9.]{16,40}`) | GET, HEAD | D |
| `open_pixel` | `/o/<token>.gif` (`[A-Za-z0-9.]{10,30}`) | GET, HEAD | D |
| `calendar` | `/c/<token>.ics` (`[A-Za-z0-9._]{20,60}`) | GET, HEAD | D |

Manage, namespace `manage` (`manage_urls.py`, `staff_required`), views in `manage_email.py` (C):

| Name | Path | Method |
|---|---|---|
| `utskick_health_release` | `/manage/utskick/konto/<pk>/halsa/` (pk = FlamingoAccount) | POST |
| `utskick_domain_admin` | `/manage/utskick/doman/<pk>/` (pk = SenderDomain) | GET, POST |
| `utskick_dlq` | `/manage/utskick/koer/` | GET, POST `action=redrive` |
| `utskick_probe` (S2) | `/manage/utskick/prov/` | POST; C adds "Provmejl till mig" |

## Templates, CSS and JS

| Folder or file | Owner | Notes |
|---|---|---|
| `templates/flamingo/app/utskick/_stub.html` | foundation | delete with the last stub (and `app_views.render_stub` / `not_built`) |
| `templates/flamingo/app/utskick/brev_editor.html`, `_brev_*.html` | B | `.fl-br-`, no `style=`, no inline script, `data-label` on every cell |
| `templates/flamingo/app/utskick/health.html`, `domain.html`, `reply_confirm.html` | C | same rules; DNS records as `.fl-kv` |
| `templates/utskick/brev/` | A | inline styles only (the mail), own guard |
| `templates/utskick/links/email_*.html`, `web_view` has no template chrome | D | extend `utskick/links/_base.html`; never `{% csrf_token %}` |
| `templates/manage/utskick/_overview_email.html`, `domain.html`, `queues.html` | C | `<thead>` in every table |
| `static/css/flamingo-app-brev.css`, `static/js/flamingo-app-brev.js` | B | already in static_version |
| `static/css/flamingo-app-utskick-email.css` | C | `.fl-ut-em-`; already in static_version |
| `static/css/utskick-public.css` | D appends | section "S3, mejlens sidor", prefix `.up-` |

## Tests and conventions

- Test command: `TEST_DB_NAME=<unique> SENTRY_DSN= uv run python manage.py test <labels> --noinput`
  (`--parallel 1` for the full suite). No network: `transport.FakeSes` for SES sends;
  `mock.patch("apps.utskick.aws.client", return_value=<fake>)` for SQS, S3 and the SES API
  client; `mock.patch("dns.resolver.resolve")` (or a resolver object) for dnspython. The test
  runner blanks the role, the queues and the bucket; set them with `override_settings` when a test
  needs them.
- Link-host tests: `override_settings(**test_s3_foundation.LINK_SETTINGS)` and
  `HTTP_HOST="klick.adx.se"`; POSTs with `Client(enforce_csrf_checks=True)`, `HTTP_ORIGIN="null"`,
  no Referer; assert that no response sets a cookie.
- Utskick tests use `testing.UtskickFixture`; `make_utskick` and `make_domain` in
  `test_s3_foundation` show the minimum rows.
- Guards that bite in S3: `django.core.mail` only in `alerts.py`; the text `send_email(` only in
  `email/transport.py` (also in docstrings and comments); **any** `email.*` stdlib import only in
  `email/mime.py` (parse incoming mail with `mime.parse`); no `style=` and no inline `<script>`
  under `templates/flamingo/app/utskick/`, `templates/utskick/links/` and `templates/manage/utskick/`;
  `data-label` on every `<td` in the Flamingo folders (the guard matches the literal text, comments
  included: write "tabellcell"); the review-site word only in allow-listed files; copy rules (no
  en or em dash, typographic quotes, ellipsis character, `[ ]` decoration, exclamation marks,
  response-time promises; in `test_*.py` write `chr(0x2013)` instead of the character).
- Demo: it never sends (D12). `sending.email` simulates the demo like `sending.sms.simulate`;
  `assert_not_demo` sits in the transport path (C) and a test proves every path refuses it.
  Extend `apps/flamingo/test_demo.py` to patch the transport client to fail if called.

## Deviations from the contract (foundation)

1. `Utskick.sender_domain` is `on_delete=RESTRICT`, not `PROTECT`. Django's collector raises
   `ProtectedError` for PROTECT even when the referencing utskick is deleted by the same
   cascade, so deleting an account (or "Avsluta utskick och radera allt") with a domain in use
   would fail. RESTRICT still refuses to delete a domain that an utskick uses on its own. No
   database rule (NO ACTION, checked at commit). Removing a domain sets `status=removed`; the row
   stays.
2. `EmailImage.purpose` is `max_length=7` (`content` has seven characters; B.3 says 6).
3. Columns not listed in B, added because S3 needs them (each nullable or `db_default`):
   `Utskick.email_snapshot` (the frozen mail, F.1 #12 "snapshotted into the frozen doc", the link
   table of F.4 and the web view after send); `UtskickSettings.email_blocked_at`,
   `email_blocked_reason`, `email_released_at`, `email_released_by` (the D.9 account block and
   "Släpp spärren", separate from `sending_blocked`); `Switchboard.ses_account` and
   `ses_checked_at` (the GetAccount snapshot for the agency overview).
4. **The configuration set does not publish OPEN (or CLICK).** With an OPEN event destination SES
   inserts its own tracking pixel into every HTML mail of the configuration set, including
   recipients without `tracking_ok`, which H.5 and LEK 9 kap. 28 § forbid. Opens are recorded by
   our own pixel `klick.adx.se/o/<token>.gif` only (E.1), which is only rendered for
   `open_tracking` and `tracking_ok`. `events.apply` ignores Open. D.7's "plus OPEN only for the
   pixel" is read as our pixel.
5. Token formats the contract left open: `/w/` `<utskick62>.<recipient62>.<sig12>`, `/o/`
   `<recipient62>.<sig8>`, `/c/` `<utskick62>.<block_id>.<sig10>`; the reply local parts use a
   lower-case base36 signature (mail systems may lower-case the local part) and the kind `t`
   (thread) for Inkorg replies next to `r` and `u`; `/v/` reuses the S1 preference token, so
   `/v/<t>` and `/utskick/val/<t>/` accept the same token. A test mail's click token carries
   recipient 0 and must redirect without counting (as the S2 test sms).
6. DLQ URLs are derived (`queue URL + "-dlq"`), not settings; the script creates them so.
7. The own reply address is confirmed through an app route (`app_utskick_reply_confirm`, login
   required, CSRF), not a public page: the link proves the mailbox and the login proves the
   account. The mail with the link is sent only from a button that says the customer is mailed.
8. The S3 manage views live in a new module `manage_email.py` (one owner), like the S2 split.
9. The daily email cap is a wait, not a pause (D.9 and I.5); D.3's "`paused_cap`
   `email_daily_cap`" is not built and no pause reason is added.
10. IAM (H.8): besides the listed actions the role gets `sqs:SendMessage` on the two main queues
    and `sqs:ListMessageMoveTasks` (StartMessageMoveTask needs to send to the destination); the
    Deny uses `arn:aws:ses:*:...:identity/<name>`; the identities that existed before S3 are kept
    in `server/aws-utskick-identities.txt`, written by the first run and then only read, so
    customer domains created later never land in the Deny.
11. `aws-utskick-s3.sh` creates the `svar.utskick.adx.se` identity with Easy DKIM and UPSERTs its
    DKIM CNAMEs and the MX in Route53 (zone `Z2HKAATG1V4QEA`); bucket name
    `adx-utskick-inbound-500841883756`; rule set `adx-utskick` (or the one already active),
    rule `adx-utskick-svar`, TLS optional.
12. The Leveranshälsa tab is in the tab row for every enabled account, also before email is live
    (the page explains the state).

## Open items for S3

- **Lead (ask Giovanni first)**: J S3 steps 1 to 3 are `server/aws-utskick-role.sh` then
  `server/aws-utskick-s3.sh` (SSO login); commit `server/aws-utskick-identities.txt` after the
  first role run; the scripts print the `.env` lines for step 4.
- The S1 DOI mail is sent without a configuration set; from S3 it should carry it and the tags so
  DOI bounces and complaints are seen (C, `optin.py` and the transport).
- Demo content for S3 (an email utskick, simulated, with the Brev blocks), `demo.reset`
  removing the S3 rows, and the `test_demo.py` extensions: integration step.
- `README.md` "S3 as built", the checklist notes and the deviations above: lead.
- 375 px QA of every real S3 view (editor with the bottom-sheet panel, Leveranshälsa, Egen domän,
  settings rows, the link-host email pages) is the builders' job; the stub pages only show a title
  and the tab row (checked by the foundation, see the report).
- After the builders: delete `_stub.html`, `app_views.render_stub` and `not_built`, and put the
  "no `_stub.html`" assertion back in `test_s2_ui.GuardTests`.

## Requests from editor

Written by the editor builder (B) on 2026-10-10 while building `app_views/brev.py`,
`flamingo-app-brev.js` and the brev profile of `flamingo-pb.js`. The editor codes against these;
the renderer builder (A) owns the files named.

1. **Editor-mode markup** (`render.render_html(utskick, ctx)` with `ctx.mode == "editor"`, and
   `render.render_block`). The renderer chose `<tbody data-brev-canvas>` with the header row
   (`data-brev-element="header"`), the block rows and the footer row (`data-brev-element="footer"`)
   as siblings; the editor follows that: profile "brev" mounts on canvas root
   `[data-brev-canvas]`, swaps the two `data-brev-element` rows as chrome, and inserts a block
   that goes last before the footer row (new profile key `canvasEnd`). Please keep:
   - each block row carrying `data-pb-block`, `data-pb-type` (the `{% pb_block %}` tag), and no
     other `<tr>` directly under the tbody besides the header and footer rows;
   - text fields with `data-pb-field="<path>"` (page-builder paths, `items.0.q`), empty ones
     `data-pb-empty`, images `data-pb-media data-pb-field="<path>"` also inside items
     (`items.1.image`). `rich_basic`, `url`, `date`, `time`, `email`, `phone`, `code` and
     `choice` fields may carry `data-pb-field`: the editor never makes them contenteditable, a
     click opens its field panel;
   - `EDITING_CSP` first in `<head>` (the editor only adds its canvas stylesheet).
2. **Render from the object passed in.** `context_for`, `render_html`, `render_block`,
   `subject_for` and `preheader_for` read `email_doc`, `accent`, `logo_position`, `subject`,
   `preheader`, `from_name` and `sender_domain` from the `utskick` argument and never re-read
   the row: the editor renders unsaved changes on a `copy.copy(utskick)` (as
   `pages.page_render_block` does with `preview.draft`).
3. **`email/blocks.py`**: `is_signed(version)` and the stamping inside `save` exist (thanks).
   `brev_save` uses `_stamp`-like logic only to see whether the blocks changed (a save without
   changes neither bumps `email_rev` nor makes a scheduled utskick a draft) and passes the
   client's blocks to `save`, which stamps and signs them. `save` is called **outside** the row
   lock: it requests new link hosts after its UPDATE (an agency mail), and the S2 rule is that
   no mail goes out while the row is locked. The scheduled-to-draft step (`state.unconfirm`) and
   the fields outside the blocks are written first, under the lock.
4. **`registry.library(account, utskick)`**: the editor reads `ok`, `why_not` and `group_name`
   from each entry and draws its own icons (`#pb-i-brev-<key>`, `_brev_icons.html`).
5. **`images.rendition(asset, purpose)`** returns the `EmailImage` with `width` and `height`
   set; the editor answers `{"url": images.absolute_url(image), "width", "height", "alt":
   asset.alt}`. `purpose` comes from the editor as one of `EmailImage.Purpose` values
   (`content`, `logo`, `video`, `avatar`).
6. **New block over `rita/`**: there is no `app_brev_block_new` route, so `brev_render_block`
   answers both: `{"type", "variant"?, "fields"?}` gives `{"ok", "block"}` from
   `blocks.new_block` (+ `blocks.add_version` for AI fields). No route change needed; noted as
   a deviation.
7. **`email.checks`**: Granska calls `email_checks(utskick, now=now, email_count=n_email,
   check_links=False)` (the cap row comes from the renderer's "sender" item with Granska's
   count; links are checked in the editor only, because of the hourly quota). The editor's
   panel calls it with `check_links=False` on every load and with `check_links=True` only from
   its "Kontrollera länkarna" button.

8. **Built by the editor (B), and its deviations for "S3 as built":**
   - Files: `app_views/brev.py`, `ai.py`, `templates/flamingo/app/utskick/brev_editor.html`,
     `_brev_card.html`, `_brev_icons.html`, `static/css/flamingo-app-brev.css`,
     `static/js/flamingo-app-brev.js`, `test_s3_editor.py`; marked S3 blocks in
     `app_views/utskick.py`, `reports.py` (`email_numbers`), `step_kanal.html`,
     `step_innehall.html`, `step_review.html`, `report.html`, `settings.html`, `list.html`;
     the profile in `static/js/flamingo-pb.js` and `apps/flamingo/app_views/pages.py`
     (`PAGE_PROFILE`, asserted unchanged in `test_pagebuilder_editor.py`); the demo walk in
     `apps/flamingo/test_demo.py`; one assertion in `test_s2_ui.py` (the Kanal step now shows
     the email modes, locked with "E-post är inte påslaget än.").
   - `flamingo-pb.js` gained the profile keys `profile`, `canvasRoot`, `chromeSelectors`,
     `canvasEnd`, `paletteMarker`, `devices`, `placement`, `panelKinds`, `texts`, with
     `addWords` and `wireframes` moved to the server, and the API `setField`, `touch`, `flush`,
     `saveExtra`, `renderExtra`, `rerender`, `openMedia`, `isMobile`, `toast` plus the event
     `field` and `data` on `saved`. `openMedia` takes item paths (`items.1.image`); the list
     popover draws media, rich_basic and the other email kinds. The page profile passes none of
     the new keys' effects (the defaults are today's values).
   - The desktop canvas is 680 px, not 560: at 560 the mail's own `@media (max-width:620px)`
     would show the phone layout in "Dator". Phone is 375.
   - A new block is fetched from `rita/` with `{"type"}` (no new route).
   - Subject, preheader, accent, logo, sender, fallbacks and "Uppgifterna stämmer" are saved
     with the blocks in one `spara/` call (rev checked); a save without changes keeps the rev.
     If `blocks.save` fails after a scheduled utskick became a draft, it stays a draft.
   - The editor's checks are light by default; links and the recipient count against the ADX
     cap only with "Kontrollera allt, också länkarna" (`?lankar=1`). Granska passes
     `email_count` and never checks links.
   - "Förhandsvisa" is a `<dialog>` (Dator, Mobil, Mörkt läge, byt kontakt), not a panel;
     "Mörkt läge" approximates clients that invert colours (CSS invert in the preview only).
     `app_brev_preview` also answers JSON (subject, preheader, From, contact, next contact).
   - Test mail (F.8): a customer user only to the own login address; staff to their own login
     address (typed when the staff user has none), and to one of the customer's login
     addresses behind "Skicka test till kunden (kunden får mejlet)" with the I.4 checkbox (a
     `<details>`, as the S2 test sms). Pre-checks in the view: subject, `content_problems`,
     suppression, demo; `send_test` does the rest.
   - Kanal: the email modes are locked until `state.email_live()` (demo excepted), and an
     email mode is locked when its mails exceed `adx_cap_left` without a verified own domain
     (the I.3 text). The sms sender stays visible for email-only (ignored, not required).
   - Innehåll: "Skriv med AI" for the sms is a step action; the suggestion is kept in the
     session and becomes the text only with "Använd förslaget". The email tab is a card with
     the editor link and the test mail. Saving the sms keeps the mail's fallbacks.
   - Settings: "Spåra öppningar" adds or removes "Mejlen innehåller en bild som visar om de
     öppnas." in `consent_text_email` and refuses when the text would exceed 200 characters;
     new utskick copy the setting.
   - Report: email tiles (Levererade, Klick, Öppnat (indikation) only with open tracking,
     Studsar, Avregistreringar) are not links, because `reports.recipients_for` lists sms
     recipients only; email-only sends show Förfrågningar without kr per förfrågan. Banners
     for `adx_mail_cap`, `bounces` (customer may resume, C's request 2), `complaints`,
     `account_health` and `email_disabled`.

## Requests from sending

Written by the sending builder (C) on 2026-10-10. No model or migration change was needed. What
the other builders and the lead should know:

1. **Renderer (A)**: the loop calls `render.context_for(u, mode=render.SEND, recipient=r,
   snapshot=u.email_snapshot or None)`, `render_html(u, ctx, render.SEND)`, `text.render_text`
   and `render.subject_for`; the test mail calls `context_for(u, mode=render.PREVIEW,
   contact=kontakt, test=True)` (no TrackedLinks exist before the freeze, so the test mail has
   the real addresses) and gives the test mail no `List-Unsubscribe` at all (renderer request
   3). `freeze_email` calls `render.snapshot(u, now=now)` first and then
   `render.collect_links(u, doc, data=snapshot)` (renderer request 2; only http and https spots
   become TrackedLinks, mailto and tel are skipped), then sets `snapshot["links"] =
   {"<block_id>:<position>": link_id}`. Media ids are re-checked with
   `blocks.media_ids(blocks.active_blocks(u, doc))`. `retention.purge_s3` calls
   `images.purge_unused(now)`. Renderer request 1 is done in a marked block of
   `manage_sending.py`: `info_override` stores `email_fingerprint` for an utskick with email,
   and the overview marks the override stale when it no longer matches. My tests replace the
   renderer with `test_s3_transport.fake_render()`; a manual run with the real renderer
   (compose, snapshot, collect_links, MIME) worked.
2. **Editor and Granska (B)**: `send_test` counts `Counter("test_send")` itself (the view must
   not count again) and writes `Event(kind="test_send")` for a contact. `error_text(sent)` gives
   the Swedish text for any refused or failed single mail. `state.email_prechecks` pauses an
   utskick that does not fit the ADX cap before its first mail (`adx_cap_text` is the I.5
   banner). The settings rows of I.9 can use `email.domains.summary(account)` (`card_text` is
   "utskick.adx.se · 1 240 av 2 000 mejl i oktober · svar till Inkorgen") and link to
   `flamingo:app_utskick_domain` (page title "Avsändare och svar": Egen domän and the own reply
   address live there). I.5 lets the customer "Ta bort studsade och fortsätt" on `bounces`;
   `CUSTOMER_RESUMES` in `app_views/utskick.py` does not list `bounces` yet. A resumed utskick
   is judged on what was sent after the resume (`stats["resumed"]`), so a resume is not
   paused again by the same bounces.
3. **Inbox (D)**: test mails and the agency probe mail carry `Reply-To` and the mailto
   unsubscribe with the recipient id `sending.email.NO_RECIPIENT` (`zzzzzzzz` in base 36),
   which never exists: `target_for` and `_mailto` fall back to the account and `From`, as
   G.3 says for a missing recipient. `deliver` raises `keys.KeyMismatch` when the process keys
   are wrong (the S2 sms paths do the same).
4. **Lead**:
   - Deviations: (a) the configuration set and its IAM grant only exist after J S3 steps 1 to
     3, so `transport.configuration_set()` sends `ConfigurationSetName` only when
     `UTSKICK_SQS_EVENTS_URL` is set; until then mail goes as in S1, and in production the
     email loop waits (agency alert) without the event queue, because bounces and complaints
     would never reach the health checks. The DOI mail gets the set and the tags k and a through
     the transport, so `optin.py` is unchanged. (b) Test mails and inbox replies from the ADX
     domain are counted in a `Counter("adx_mail")` whose window is the start of the next
     Stockholm month, so `limits.purge` (two days) keeps it until the month is over. (c) The
     health check runs on bounce and complaint events and every 50 sends, not on deliveries (a
     delivery can never cross a threshold). (d) A transient bounce makes the recipient `failed`
     ("Tillfällig studs"); the fifth in a row is a hard bounce. (e) One domain per account at
     a time; an expired pending domain also has its SES identity deleted (when `ses_created`),
     so the customer can claim it again. (f) Removing a domain moves drafts that used it to the
     ADX domain and is refused while a scheduled, sending or paused utskick uses it. (g) A staff
     resume after a failed probe passes the probe.
   - `utskick_daily --only ses|domains|dlq` runs one part (J S3 step 6 says `--only ses`).
   - The customer card shows no email block; the block and "Släpp spärren" are on
     `/manage/utskick/#epost-halsa` (`manage:utskick_health_release` accepts `back=kund`).

## Requests from renderer

Written by the renderer builder (A) on 2026-10-10. Built: `email/{registry,blocks,style,render,
text,images,checks}.py`, the S3 block of `email/mime.py`, `templates/utskick/brev/` and the tests
`test_s3_{registry,render,media,link_check}.py`. Outside the app: `pagebuilder/blocks.py`
(`types=`, `salt=`), `flamingo/media.py` (`types=`, delete protection) and the review-site
allow-list in `apps/website/tests.py`. What the others and the lead need:

1. **Lead or C, the agency override for email information (H.5).** `content_fingerprint` in
   `sending/checks.py` covers the sms text only. `email.checks` therefore lets the override
   release the ad words of an email only when `content_override["email_fingerprint"] ==
   email.checks.email_fingerprint(utskick)` (fails closed). Please add one line to
   `manage_sending.info_override`: `"email_fingerprint": email_checks.email_fingerprint(utskick)`
   in the stored dict, and let `override_stale` compare it too when `utskick.has_email`. Until
   then an email information utskick with ad words cannot be overridden.
2. **C, freeze.** `render.collect_links(u, doc, *, data=None)` takes the frozen data: calling
   `snap = render.snapshot(u, now=now)` first and then `render.collect_links(u, data=snap)` counts
   the spots on exactly what is sent. The current order also works; a block whose image cannot be
   decoded at the freeze then just leaves an unused TrackedLink. `blocks.media_ids` and
   `blocks.urls` accept the block list or the `active_blocks(...)` tuples (the freeze passes the
   tuples). The snapshot carries `image_ids` (EmailImage pks) that `images.purge_unused` keeps
   while recipients remain; call `images.purge_unused(now)` from `retention.purge_s3`
   (returns `{"deleted": n}`).
3. **C, test mail.** With `test=True` the footer's "Ändra vad du får" and "Avregistrera dig" point
   at `https://klick.adx.se/` (the previewed contact's hash would otherwise unsubscribe that
   contact from the customer's own test mail). The `List-Unsubscribe` header of a test mail must
   not carry the contact's hash either.
4. **C, MIME.** `mime.MAIL_POLICY` (S3 block of `mime.py`) is the SMTP policy except that
   `List-Unsubscribe`, `List-Unsubscribe-Post`, `In-Reply-To` and `References` are folded only
   between items and never RFC 2047 encoded: the stdlib folded a `List-Unsubscribe` longer than
   78 characters into `=?utf-8?q?...?=`, which Gmail cannot read (`MimeTests` pins it).
5. **B, editor.** Editor mode is as you describe (`data-brev-canvas`, `data-brev-element`,
   `EDITING_CSP` first, `body[data-brev-editing]`, empty fields `data-pb-empty`, which the
   inline `<style>` hides until your stylesheet shows them). Rendering reads only the object
   passed in (`email_doc`, `accent`, `logo_position`, `subject`, `preheader`, `is_information`).
   `blocks.save(..., terms_ok=True)` with the user records "Uppgifterna stämmer"
   (`terms_confirmed_at/_by`; changed terms reset it). `BlockError.errors` items are
   `{"block", "field", "where", "text"}`; `BlockError.texts` gives "where: text".
   `blocks.add_version(block, fields, source, user, *, account)` cleans and signs AI versions.
   `registry.logo_state(account)` locks the logo choice ("Ladda upp en logotyp under Media.");
   `style.picker(account)` gives the logo colours, the swatches and `light_text`;
   `style.is_light(hex)` decides the warning. `blocks.merge_problems(account, text)` checks the
   subject and preheader before you save them. Icons: the registry names `hero`, `heading`,
   `text`, `button`, `image`, `image_text`, `columns`, `divider`, `guarantee` (offer), `price`,
   `reviews`, `steps`, `event`, `person`, `video`, `gallery`, `faq`, `area` (hours), `callout`,
   `spacer`, `social`; you draw `#pb-i-brev-<key>` yourself, which is fine.
6. **D, link host.** `render.web_view(utskick, recipient=None)` returns the whole document (the
   frozen mail, no pixel, no "Visa i webbläsaren"); the F.4 CSP and `X-Frame-Options` headers
   are yours. `render.calendar_ics(utskick, block_id)` returns the text (CRLF, folded) or None.
7. **Deviations (renderer), for "S3 as built":**
   - F.1 refactor: `types=` on `pagebuilder.blocks.active_fields`, `visible_items` and
     `media.media_ids_in`, and `salt=` on `sign_version`, `is_signed`, `sign_blocks`. Email
     validation lives in `email/blocks.py` (it reuses the page builder's id patterns, structure
     rules, sanitizers and signature) instead of `types=` on `validate_blocks`, `clean_fields`,
     `new_block` and `add_version`: the email kinds need the account (E.8 hosts, field keys) and
     errors per block and field. Page behaviour is unchanged.
   - Required fields and `min_items` never stop a draft save (autosave); `email_checks` blocks
     required fields and warns on too few items.
   - Every block has one variant, `brev`.
   - Every transparent image is flattened onto white (PNG), not only logos (dark mode).
   - The footer name is the legal customer name (`Customer.name` without "(demo)"), the footer
     grey is the mockup's `#9AA0A6`, links are `#9AA0A6` underlined.
   - Unconfirmed offer terms are a warning, not a block. A reviews block whose profile is gone
     is not drawn and warns.
   - In an information utskick the "Hitta hit" link the hours block builds from the company
     address is allowed; every other link must go to the customer's own site.
   - The link check runs only with `check_links=True` (the hourly quota of ten runs).

## Integration (2026-10-10)

The inbound builder's section "Requests from inbound" did not survive a concurrent edit of this
file; its three items are applied here. Everything below is folded into README "S3 as built".

- **Requests applied.** Editor 1 to 7 (A built them, B's code uses them); sending 1 (freeze and
  test mail as asked), 2 (`bounces` in `CUSTOMER_RESUMES`, settings rows link to Avsändare och
  svar), 3 (a reply to a test or probe mail routes by account and sender: `test_s3_flow`);
  renderer 1 (`info_override` stores `email_fingerprint`), 2 and 3 (freeze order, no
  `List-Unsubscribe` on test mails); inbound: `utskick_daily` runs `inbound.email.sweep_bucket`
  (`retention.s3_email_steps`), the `limits.py` docstring lists the S3 scopes and the locks
  `0x5557` and `0x5558`, and `contacts._erase_s2` blanks `InboundMessage.subject`.
- **Built by the integration.** The four remaining `klick.adx.se` views in `link_views.py`:
  `email_click` (`/m/`), `email_preferences` (`/v/`, template `utskick/links/email_preferences.html`,
  `link_actions` takes `channel=` and `detail=`), `open_pixel` (`/o/`), `calendar` (`/c/`); the
  S3 sources of `timeline.py`, the email "Senast" labels and `contacts.touch` on email reply,
  hard bounce and complaint; the S3 parts of the GDPR export and delete; `email.blocks.own_page`
  (a link to the account's own Flamingo page is never requested from ADX and never blocks
  Granska; found by the end-to-end test); warnings on the e-post panel; the domain help alert
  with pks and the admin link; the ADX cap line on the settings page; the demo's "Höstbrevet";
  the `test_demo` SES guards; the mail template guard; the Sentry patterns; the stubs removed.
- **Tests added.** `test_s3_flow.py` (the whole path), `test_s3_link_pages.py` (the four views),
  `DemoUtskickS3Tests` in `apps/flamingo/test_demo.py`, `MailTemplateGuardTests` in
  `test_s1_guards.py`, S3 cases in `apps/common/test_sentry.py`, the domain help alert in
  `test_s3_domains.py`.
