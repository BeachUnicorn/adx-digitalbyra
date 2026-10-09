# S1 handoff: the foundation is in, the rest of S1 builds on it

Written by the foundation agent on 2026-10-09. The contract is `README.md` in this folder; this file
only says what exists now, who owns what next, and which helpers to call. Delete this file when S1
ships (fold anything still useful into README.md).

## What the foundation built

- App `apps.utskick` (label `utskick`) in `INSTALLED_APPS` after `apps.sms`.
- `models.py`: every S1 model of B.1 with its constraints and indexes. Migrations
  `utskick/0001_kontakter.py` (depends on flamingo 0015 and sms 0005) and
  `flamingo/0016_forfragans_kontakt.py` (`Lead.contact`, nullable, related name `leads`).
  Applied to the local dev database. No other agent edits `models.py` or migrations in S1: ask the
  lead if a field is missing.
- Settings (C.4, S1 keys) in `config/settings/base.py` and `.env.example`; production refuses to
  start without `UTSKICK_HASH_KEY` and `UTSKICK_LINK_KEY` (`config/settings/production.py`).
  `TEST_RUNNER = "config.test_runner.NoNetworkRunner"` (C.3): no socket leaves loopback, no DNS
  lookup of real names, and `ADX_AWS_PROFILE="__test__"`, `SMS_SEND_LIVE=False`,
  `UTSKICK_EMAIL_LIVE=False` for the whole run (parallel workers included).
- `pyproject.toml`: `openpyxl`, `defusedxml`, `boto3` (uv.lock updated).
- Core modules: `access.py`, `nav.py`, `keys.py`, `normalize.py`, `freemail.py`, `consent.py`,
  `suppression.py`, `contacts.py`, `limits.py`, `alerts.py`, `timeline.py`,
  `templatetags/utskick_tags.py`, `testing.py` (test fixture).
- URL wiring for every S1 route name of section I, with stub views (see "Routes").
- Kontakter appears in the Flamingo side menu after Kampanjer only when utskick is enabled
  (`apps/flamingo/app_views/__init__.py` calls `nav.nav_for(account)`).
- Shared sub-navigation: `templates/flamingo/app/kontakter/_layout.html`, `_tabs.html`,
  `static/css/flamingo-app-utskick.css` (tab row; `<details class="fl-subnav">` under 560 px).
  Checked at 375 px on the local demo: no horizontal overflow, nothing clipped.
- Tests: `test_s1_models.py`, `test_s1_consent.py` (93 tests).

## Ownership for the next agents

One owner per file. Stubs are marked `STUB` in their docstring; the owner replaces the whole module.
Each later agent creates its own CSS and JS file (names below) and never edits another agent's.

| Agent | Python | Templates | Static |
|---|---|---|---|
| Contacts UI | `app_views/contacts.py`, `app_views/lists.py`, `app_views/fields.py`, `app_views/settings.py`, `app_views/dpa.py` | `templates/flamingo/app/kontakter/{list,form,detail,_timeline,delete,export,prune,lists,list_detail,fields,settings,dpa}.html` | `static/css/flamingo-app-kontakter.css` (`.fl-kt-`), `static/js/flamingo-app-kontakter.js` (select, bulk bar) |
| Importer | `importer.py`, `xlsx2csv.py`, `app_views/imports.py` | `templates/flamingo/app/kontakter/import_{upload,map,consent,wait,review,done}.html` | `static/css/flamingo-app-import.css` (`.fl-im-`), `static/js/flamingo-app-import.js` (polling), `static/utskick/kontakter-mall.xlsx` |
| Public pages | `public_views.py`, `tokens.py`, `optin.py`, `aws.py`, `email/transport.py` (DOI only in S1), `app_views/signup.py` | `templates/utskick/public/*.html`, `templates/utskick/mail/doi.html`, `doi.txt` | `static/css/utskick-public.css`, `static/js/utskick-public.js` (botcheck proof filler only) |
| Integration | `manage_views.py`, `capture.py` (LP checkboxes), `demo.py`, `management/commands/utskick_tick.py`, `utskick_daily.py`, changes outside `apps/utskick` listed under "Not done" | `templates/manage/utskick/*.html` (incl. `_customer_card.html`), `templates/flamingo/lp/ren/blocks/form.html` changes | `static/css/manage-utskick.css` if needed |

`app_views/__init__.py`, `access.py`, `nav.py`, `keys.py`, `normalize.py`, `freemail.py`,
`consent.py`, `suppression.py`, `contacts.py`, `limits.py`, `alerts.py`, `timeline.py`,
`templatetags/utskick_tags.py`, `testing.py` and `flamingo-app-utskick.css` are shared: extend them
with care (add, do not change behaviour other modules rely on), and say so in your report.

The three stub templates `flamingo/app/kontakter/_stub.html`, `utskick/public/_stub.html` and
`manage/utskick/_stub.html` go away when the last view using them is replaced.

## Helper APIs (call these, do not reimplement)

### access.py

```python
settings_for(account) -> UtskickSettings          # saved row or unsaved default (is_enabled False)
is_enabled(account, utskick_settings=None) -> bool  # utskick on + Flamingo on + customer active
current_dpa() -> DpaVersion | None
latest_acceptance(account) -> DpaAcceptance | None
dpa_ok(account) -> bool                           # latest acceptance is the current version; demo True
can_collect(account) -> bool                      # is_enabled and dpa_ok; gate EVERY contact-creating path
collect_block_reason(account) -> str              # NOT_ENABLED_TEXT, DPA_MISSING_TEXT, DPA_NEEDED_TEXT or ""
@utskick_view                                     # app_view + enabled check (404) + ForeignIds -> 400
owned(model, account, pk, via="account")          # 404 for another account's row
owned_ids(model, account, ids, via="account", limit=None) -> list[int]  # raises ForeignIds
class ForeignIds(Exception)
Actor(user=None, label="", staff=False); PERSON; SYSTEM
actor_for(request) -> Actor                       # staff in view-as: label "ADX (Giovanni)", staff=True
validate_public_slug(slug, exclude_pk=None) -> str   # ValidationError for reserved/taken
suggest_public_slug(name, exclude_pk=None) -> str
```

A view is `def view(request, account, ...)` decorated with `@utskick_view`;
`request.utskick_settings` is set. Put `@require_POST` *under* `@utskick_view` so the 404 gate runs
first.

### app_views/__init__.py

```python
render_contacts(request, template, tab, context=None, status=200)
# tab: "contacts" | "lists" | "import" | "fields" | "signup" | "settings"
```

Adds `kt_nav` (tab row) and `utskick_settings` and sets `app_active="contacts"`.

### consent.py (the only writer of Consent.status)

```python
set_status(contact, channel, status, *, source, actor=None, source_detail="", text_shown="",
           tracking_ok=False, evidence="", collected_at=None, confirmed_at=None, ip_hash="",
           proved=False, suppression_reason=None, now=None) -> Outcome
#   Outcome(consent, changed, refused, lifted); .ok, .refusal_text
#   refused codes: no_address, suppressed, locked, not_allowed, already, evidence
check_transition(old, new, source, *, channel, suppressed=False, proved=False) -> str
ensure_rows(contact, source, actor=None, source_detail="", now=None)
derive_company(contact, source, actor=None, now=None) -> Outcome
eligible(contact, channel, purpose, consent=None, suppressed=None) -> bool   # purpose REKLAM | INFORMATION
ineligible_reason(...) -> "" | no_address | bounced | suppressed | no_consent | declined | pending_doi
eligible_contacts(qs, channel, purpose) -> QuerySet   # same rule in SQL ("kan få sms" counts)
chip(contact, channel, consent=None) -> {"label", "tone", "channel"}   # tone: ok wait muted warn stop
```

Rules worth knowing: customer sources (`import`, `manual`, `api`) move only `missing` (or derived
`company`) to `yes`/`existing`, and need `evidence`; they never touch `pending`, `declined`,
`unsubscribed` or a suppressed hash. Forms (`lp_form`, `signup`, `preference`) go to `pending` (and
`lp_form` to `yes` for sms, K.2.5). Only `proved=True` with a proof source (`doi`, `confirm`,
`start`, `link`, `preference`) leaves `unsubscribed` and lifts the suppression. `unsubscribed` always
writes a Suppression. Every change writes one ConsentLog row. `confirm_sent_at` and
`confirm_count` are bookkeeping for `optin.py`, which writes them directly.

Invariant the SQL filter relies on: every channel where a contact has an address has a Consent row
whose `value_hash` is the hash of the current address. `contacts.create`, `change_address` and
`ensure_rows` keep it; never write `Contact.phone`/`email` any other way.

### suppression.py

```python
is_suppressed(account, channel, value=None, value_hash=None) -> bool
suppressed_hashes(account, channel, hashes) -> set
add(account, channel, value_hash, reason, note="", now=None) -> (Suppression, created)
suppress(account, channel, value, reason, source, source_detail="", actor=None, note="",
         ip_hash="", now=None) -> (Suppression, contact | None)   # works for disabled accounts
lift(account, channel, value_hash)   # only via consent.set_status(proved=True)
```

### contacts.py

```python
clean(account, data, defs=None) -> Cleaned          # ContactError(message, field)
create(account, data, *, source, actor=None, source_detail="", check_collect=True,
       cleaned=None, defs=None, now=None) -> Contact # CollectNotAllowed, ContactLimitReached
update(contact, data, *, actor=None, source="manual", defs=None, now=None)
change_address(contact, channel, value, *, actor=None, source_detail="Adressen ändrades", now=None) -> bool
match(account, phone="", email="") -> Match(contact, conflict)   # two_contacts, phone_differs, email_differs
fill_from(contact, cleaned, *, actor=None, now=None) -> bool     # import update, never overwrites an address
field_defs(account) -> {key: FieldDef}
search(qs, q) -> QuerySet; search_text_for(contact) -> str
room_left(account) -> int
touch(contact, kind, at=None)
record_event(contact, kind, data=None, lead=None, at=None, activity=True) -> Event  # activity=False: no touch
add_to_list(contact_list, contacts, source="manual") -> int; remove_from_list(contact_list, contacts)
add_tag(tag, contacts) -> int; remove_tag(tag, contacts) -> int
linked_leads(contact); export_contact(contact) -> dict
reserve_export(account, now=None) -> bool           # 10 full exports per account per Stockholm day
log_export(account, actor, kind, rows) -> ExportLog
delete_contact(contact, *, actor=None, delete_leads=True, suppress=True, now=None) -> {"leads", "suppressed"}
```

`data` keys: `kind`, `first_name`, `last_name` (or `full_name`), `company_name`, `org_number`,
`phone`, `email`, `fields` (`{key: value}`, keys must be FieldDefs). Messages:
`DUPLICATE_PHONE`, `DUPLICATE_EMAIL`, `NOT_MOBILE`, `LIMIT_TEXT`, `COLLECT_TEXT`. A landline goes to
`fields["telefon"]` (FieldDef created on demand). The importer should pass `defs=` and `cleaned=`
to avoid one FieldDef query per row, and wrap batches in `limits.lock_contacts(account)`.

### normalize.py, freemail.py, keys.py, limits.py, alerts.py, timeline.py

```python
normalize.phone(raw) -> Phone(e164, country, landline, error)    # via apps.sms.numbers.parse
normalize.email(raw) -> str                                      # InvalidValue
normalize.org_number(raw) -> OrgNumber(value, personal)          # InvalidValue for bad length
normalize.looks_like_personnummer(v) -> bool                     # regex + Luhn + month digit 0/1
normalize.field_value(kind, raw, choices=()) -> str              # refuses personnummer (PERSONNUMMER_TEXT)
normalize.split_name(full) -> (first, last); normalize.first_name(raw) -> str
normalize.mask_phone(e164); normalize.mask_email(addr)
freemail.is_freemail(email_or_domain) -> bool
keys.value_hash(channel, value) -> str        # HMAC-SHA256(UTSKICK_HASH_KEY, "sms:+46...")
keys.link_digest(message) -> bytes            # HMAC-SHA256(UTSKICK_LINK_KEY, ...), for tokens.py
keys.check_fingerprints(alert=True) -> bool; keys.require_fingerprints()  # KeyMismatch
limits.hit(scope, key, window, limit) -> bool  # True = over the limit (refuse)
limits.count(scope, key, window); limits.hour_window(now=None); limits.day_window(now=None)
limits.purge(before=None); limits.lock_contacts(account)
limits.TICK_LOCK, limits.ADX_MAIL_LOCK, limits.CONTACT_LIMIT_LOCK
alerts.agency(subject, lines, once=None, window="hour", now=None) -> bool   # agency mail only
timeline.for_contact(contact, page=1) -> Page(items, number, has_next, ...)  # Item(at, kind, title, detail, url, link_label, by_staff)
timeline.SOURCES, timeline.EVENT_TITLES   # later stages append
```

`django.core.mail` may be imported only in `alerts.py` (guard in `test_s1_guards`). The DOI mail goes
through `email/transport.py`, never through `alerts.py`.

### templatetags/utskick_tags.py

`{% load utskick_tags %}`: filters `procent`, `kanal`, `maskerat_nummer`, `maskerad_epost`; tags
`{% samtycke_chip kontakt "sms" as chip %}` (reads `prefetch_related("consents")`) and
`{% utskick_card customer as ut %}` (calls `manage_views.card_context`). Numbers and kronor come
from `flamingo_app` (`tal`, `kr`).

### testing.py

`UtskickFixture` (with `TestCase`): Exempelrör enabled with a current, accepted DPA, user `anna`
(customer login), `staff`, and a second enabled account `other_account` for leak tests.
`client_for(user)` (staff gets view-as Exempelrör), `make_contact(account, **data)`, phone constants
`PHONE_ANNA`, `PHONE_BO`, `PHONE_CILLA` (PTS fictional +4670174xxxx). `setUp` clears the per-process
fingerprint cache and the cache; tests that change `Switchboard` fingerprints must call
`keys.forget_verified()`.

## Routes (all resolve today)

App, namespace `flamingo` (prefix `/flamingo/app/`), all behind `@utskick_view`:

| Name | Path | Module.function |
|---|---|---|
| `app_contacts` | `kontakter/` | `contacts.contact_list` |
| `app_contacts_bulk` | `kontakter/massandring/` (POST) | `contacts.contacts_bulk` |
| `app_contact_new` | `kontakter/ny/` | `contacts.contact_new` |
| `app_contacts_export` | `kontakter/export/` | `contacts.contacts_export` |
| `app_contacts_prune` | `kontakter/rensa/` | `contacts.contacts_prune` |
| `app_contact` | `kontakter/<pk>/` | `contacts.contact_detail` |
| `app_contact_edit` | `kontakter/<pk>/andra/` | `contacts.contact_edit` |
| `app_contact_consent` | `kontakter/<pk>/samtycke/` | `contacts.contact_consent` |
| `app_contact_export` | `kontakter/<pk>/export/` (POST) | `contacts.contact_export` |
| `app_contact_delete` | `kontakter/<pk>/ta-bort/` | `contacts.contact_delete` |
| `app_import` | `kontakter/import/` | `imports.import_upload` |
| `app_import_job` | `kontakter/import/<pk>/` | `imports.import_job` |
| `app_import_errors` | `kontakter/import/<pk>/fel.csv` | `imports.import_errors` |
| `app_lists` | `kontakter/listor/` | `lists.list_index` |
| `app_list` | `kontakter/listor/<pk>/` | `lists.list_detail` |
| `app_fields` | `kontakter/falt/` | `fields.field_list` |
| `app_signup` | `kontakter/anmalan/` | `signup.signup_settings` |
| `app_contacts_settings` | `kontakter/installningar/` | `settings.contacts_settings` |
| `app_dpa` | `kontakter/avtal/` | `dpa.dpa` |

Public, namespace `utskick_public` (prefix `/utskick/`, `public_urls.py`): `confirm`
(`bekrafta/<token>/`), `preferences` (`val/<token>/`), `signup` (`<public_slug>/`),
`signup_thanks` (`<public_slug>/tack/`), `privacy` (`<public_slug>/integritet/`). Stubs: signup and
thanks are 404 unless `can_collect` and the form is active; privacy needs utskick on; confirm and
preferences are 404 until `tokens.py` exists. Every response sets `X-Robots-Tag: noindex, nofollow`.

Manage, namespace `manage` (`manage_urls.py`, `staff_required`): `utskick_overview`
(`/manage/utskick/`), `utskick_switch` (`/manage/utskick/nodstopp/`, POST), `utskick_dpa_publish`
(`/manage/utskick/avtal/`, POST), `utskick_customer_update` (`/manage/kunder/<pk>/utskick/`, POST),
`utskick_customer_end` (`/manage/kunder/<pk>/utskick/radera/`).

## Template conventions

- App pages under `templates/flamingo/app/kontakter/` extend `flamingo/app/kontakter/_layout.html`
  and fill `app_title`, `kt_head` (own CSS with `?v={{ static_version }}`), `kt_header` (the
  `.fl-app-head`) and `kt_content`. The tab row is drawn by the layout.
- Context and template names say `kontakt` / `kontakter`, never `contact` (Naming rule).
- Tables: `.fl-table`, `data-label` on every `<td>` (empty for button columns), name cell
  `is-primary`, numbers `is-num`. Manage tables need `<thead>`.
- No `style=` attributes, no inline `<script>`, no `{% csrf_token %}` on link-host templates (S2).
- Copy: du-form, no exclamation marks, no response-time promises, no en/em dashes, no typographic
  quotes, no ellipsis character, no square-bracket decoration. In `test_*.py` write `chr(0x2013)`
  instead of the character.
- Never write the sister-site markers checked by `apps/website/tests.py` `VvsLegacyGuardTests` in
  this folder (read its patterns: the pipe-trade words in Swedish and English, the Swedish
  tax-deduction word, and the name of the Swedish review site). `README.md` here is allowlisted for
  the review site (added by the foundation, since the contract names it in E.8 and F.1); any new
  file that must name it (S3 link hosts, Brev reviews block) has to be added by name to the
  allowlist in that test class.
- Every new CSS and JS file must also be added to `apps/manage/context_processors.py` static_version
  (integration agent; includes `flamingo-app-utskick.css` from the foundation).

## Not done by the foundation (owners above)

- `capture.py` and the LP checkboxes (C.2 public_views, `form.html`, `flamingo-lp.js` is S2 only).
- `tokens.py`, `optin.py`, `aws.py`, `email/transport.py`, DOI templates, public page templates.
- `importer.py`, `xlsx2csv.py`, example xlsx.
- `demo.py` and the `flamingo_demo` hooks; `test_demo.py` extensions.
- `management/commands/utskick_tick.py` (phases 1, 3, 8, 9) and `utskick_daily.py` (the empty
  `management/commands/` package exists).
- Outside `apps/utskick`: `apps/manage/forms.py` `RESERVED_SLUGS` += `utskick`, `bitradesavtal`;
  `apps/core/views.py` robots (`Disallow: /utskick/`, `Disallow: /api/`); analytics `_SKIP_PREFIXES`
  += `/utskick/`; `apps/common/sentry.py` (C.3); `apps/monitor` heartbeat; static_version list;
  `apps/assistant/tests.py` forbidden names; `templates/flamingo/_customer_panel.html`
  `{% utskick_card customer %}`; settings.html line 66 "Kontakter hos" to "Inloggningar hos";
  `apps/flamingo/README.md` pointer; `server/` crontab, logrotate, `aws-utskick-role.sh`,
  `deploy.sh` flock.
- Other S1 test files: `test_s1_import.py`, `test_s1_views.py`, `test_s1_public.py`,
  `test_s1_lp.py`, `test_s1_tick.py`, `test_s1_guards.py` (it must include the B.0 migration rule and
  the production key check).

## Deviations from the contract (foundation)

- `Consent.basis` and `ConsentLog.basis` are `max_length=17`, not 12: `existing_customer` is 17
  characters.
- Event has no `idempotency_key` yet (S5 adds it with `db_default=""`); `utskick`/`recipient` FKs on
  Event, ConsentLog and Suppression come in S2 as the contract says.
- `ImportJob.size` defaults to 0.
- The partial index for pending confirmations is on `changed_at` (where `status="pending"` and
  `confirm_sent_at` is null), so the tick can read the queue oldest first.
- `looks_like_personnummer` also requires the third digit to be 0 or 1 (month), so an org number in a
  field is not refused as a personnummer.
- A form submission over `yes`, `existing` or `company` is refused as `already` (no downgrade to
  `pending`); `company` counts as upgradeable to `yes`/`existing` for customer sources.
- Customer sources need `evidence` for `yes`/`existing` (B.1 "with evidence").

## Public pages and DOI (done by the public-pages agent)

Files: `public_views.py`, `tokens.py`, `optin.py`, `capture.py`, `aws.py`, `email/__init__.py`,
`email/mime.py`, `email/transport.py`, `app_views/signup.py`, `templates/utskick/public/*`,
`templates/utskick/mail/doi.{html,txt}`, `templates/flamingo/app/kontakter/signup.html`,
`static/css/utskick-public.css`, `static/js/utskick-public.js`, `test_s1_public.py`. The public
stub template is deleted.

```python
# Tick, phase 3 (utskick_tick): the DOI queue
optin.work_exists(now=None) -> bool            # False when the transport cannot deliver here
optin.send_due(now=None, deadline=None, limit=100) -> {"sent", "skipped", "failed", "waiting"}
#   deadline is time.monotonic() when the phase must stop; never sends for the demo or for
#   accounts that cannot send; checks keys.check_fingerprints() itself.
optin.doi_ready() -> bool                      # Switchboard.doi_ready_at set (read only)
optin.queued(now=None) -> QuerySet             # pending, unsent, sendable and not blocked accounts
optin.due(now=None, ready=None) -> QuerySet    # queued(); before doi_ready_at only staff test signups
optin.requeue(consent, now=None) -> bool       # a form asked again for an already pending email
optin.absolute(path) -> str                    # SITE_BASE_URL or https://adx.se + path

# LP form (integration wires these into flamingo.public_views.LeadForm / landing / form.html)
capture.lp_consent_channels(account, spec, row=None) -> ["sms", "email"] | ["sms"] | []
capture.consent_texts(account, row=None) -> {"sms": text, "email": text}   # exact label texts
capture.consent_errors(channels, cleaned_data) -> {"email"|"phone": message}
capture.from_lead_form(lead, cleaned, texts, page_path, ip_hash="", click=None) -> Captured
#   Captured(contact, email_pending); never raises; texts = only the channels that had a box.
capture.privacy_url(account, row=None, absolute=False) -> str   # "" when no notice exists
capture.privacy_available(account, row=None) -> bool
capture.PRIVACY_MISSING_TEXT, capture.TRACKING_SENTENCE, capture.EMAIL_NEEDED_TEXT,
capture.NOT_MOBILE_TEXT

# Tokens (all signed with UTSKICK_LINK_KEY, per-kind prefix)
tokens.preference_token(account_id, channel, value_hash)   # /utskick/val/<t>/, no expiry
tokens.doi_token(consent, now=None); tokens.read_doi(token, now=None)   # 14 days
tokens.thanks_token(consent_id); tokens.read_thanks(token)              # 1 hour

# Transport (S1: kind="doi" only)
transport.send(mail, kind="doi", account_id=None) -> Sent(ok, message_id, mode, error, retry,
                                                          stop, unknown)
transport.can_send(); transport.OutgoingMail; transport.FakeSes (tests: `with FakeSes() as ses`)
aws.session(); aws.client(service, send=False); aws.forget()
```

Behaviour worth knowing:
- The signup page offers email when `Switchboard.doi_ready_at` is set; staff (is_staff) see it
  before that with a preview strip, so the S1 checklist step 6 test signup works. The tick sends
  whenever the transport can (UTSKICK_EMAIL_LIVE, or `.eml` with DEBUG); it does not check
  `doi_ready_at` itself.
- `confirm_sent_at` means "the tick handled this row" (sent, or skipped by a limit, a missing DPA
  or a rejected mail); `confirm_count` counts mails actually handed to SES. Rows older than 7 days
  in the queue are not sent; signing up again requeues (`changed_at` is bumped).
- On Mina utskick, turning a channel off and "Avregistrera mig från allt" always go through (also
  for disabled accounts and when the botcheck fails); only turning email on needs the botcheck,
  `can_collect` and `offers_email`. Sms cannot be turned on there in S1.
- The privacy fallback page needs a confirmed Företaget fact whose key or label says org /
  organisationsnummer (10 or 12 digits) plus a confirmed phone or email fact. It works for
  disabled accounts too; with an https `privacy_url` it redirects there.

## Requests from public

- **Integration (blocking a test):** add `"/utskick/"` to `apps/analytics/middleware.py`
  `_SKIP_PREFIXES`. Today the public pages get the `av_id`/`as_id` cookies and the preference and
  confirm tokens would be stored in `PageView.path`. `test_s1_public.NoCookiesTests` fails until
  then.
- **Integration:** add `css/utskick-public.css` and `js/utskick-public.js` to the static_version
  list; robots `Disallow: /utskick/`; Sentry patterns for `/utskick/(bekrafta|val)/<token>` are in
  C.3.
- **Integration (tick):** phase 3 calls `optin.send_due(now, deadline=...)` and adds its summary;
  `work_exists` includes `optin.work_exists(now)`.
- **Integration (LP):** add `consent_sms`/`consent_email` BooleanFields when
  `capture.lp_consent_channels(...)` returns channels, labels from `capture.consent_texts(...)`,
  errors from `capture.consent_errors(...)`, "Så hanterar <display_name> dina uppgifter" linking
  `capture.privacy_url(account)`, then `capture.from_lead_form(lead, form.cleaned_data, texts,
  request.path, limits.ip_hash(client_ip(request)))` after `create_form_lead`; the neutral thanks
  text (`?epost=1`) whenever the email box was ticked (not only when `Captured.email_pending`:
  the page must not reveal existing subscribers).
- **Integration (demo):** `demo.seed` should give the demo a confirmed fact `orgnr`
  ("Organisationsnummer", a fictional number) or a `privacy_url`, otherwise the demo has no
  privacy notice and its signup page and LP boxes stay off. Optionally an inactive `SignupForm`.
- **Contacts UI (settings):** `privacy_page_url` is shown even when the generated page would 404;
  `capture.privacy_url(account)` returns "" in that case. The tracking sentence for S3 is
  `capture.TRACKING_SENTENCE`.
- **Guards:** `test_s1_guards` may allow `email.message` only in `email/mime.py` (done that way);
  `FakeSes.send_email` lives in `email/transport.py`, the only `send_email` caller.

## Contacts UI (done by the contacts-ui agent)

Files: `app_views/{contacts,lists,fields,settings,dpa}.py`, templates
`flamingo/app/kontakter/{list,form,detail,_timeline,_table,_field,delete,export,prune,lists,
list_detail,fields,settings,dpa}.html`, `static/css/flamingo-app-kontakter.css`,
`static/js/flamingo-app-kontakter.js`, `test_s1_views.py`. Shared modules extended (added, nothing
changed): `normalize.display_phone(e164)` and the filter `{{ kontakt.phone|nummer }}` in
`utskick_tags` ("070-123 45 67").

Helpers other views may reuse (all in `app_views/contacts.py`):

```python
parse_filters(account, data, strict=False) -> Filters   # ?q=&lista=&tagg=&samtycke=&kanal=&typ=
filtered(account, filters) -> Contact queryset            # always account=account
selection_from(account, request.POST) -> Selection        # alla=1 + filter, or ids (owned_ids, max 200)
with_rows(qs); decorate(rows, account)                    # what _table.html reads (kt_sub, kt_chips, kt_last, kt_lists)
contacts_csv(account, qs) -> (text, rows)                 # safe_cell; the view adds the BOM
list_target(request, account) / tag_target(request, account)   # lista_id / tagg_id or "ny" + name
date_text, day_text, stamp, count_text, export_text       # Swedish dates and counts
```

`{% include "flamingo/app/kontakter/_table.html" with selectable=True %}` draws the contacts table
(cards when the table area is under 820 px, a container query). Bulk forms carry
`data-kt-bulk`; `flamingo-app-kontakter.js` counts, shows "Markera alla som matchar" and the
fields for the chosen action. The card's timeline appends "Lades till i Kontakter" for manual and
API contacts (they have no Event of their own); later stages add sources in `timeline.SOURCES`
and summary parts in `contacts._summary`, and "Senast" labels in `contacts.LAST_LABELS`.

## Requests from contacts-ui

- **Integration:** add `css/flamingo-app-kontakter.css` and `js/flamingo-app-kontakter.js` to
  the static_version list in `apps/manage/context_processors.py` (until then a changed file can
  be served from the browser cache in development).
- **Integration (demo):** the local dev database has five fictional contacts, the list
  Däckhotell, the tag Bromma and the field Regnr on the demo account (pk 4), created for the
  375 px check; `demo.reset` may remove them.
- **Integration (guards):** `test_s1_guards` can reuse `test_s1_views.TemplateGuardTests` (no
  `style=`, no inline script, `data-label` on every `<td>`, no `!` or `[ ]` in the copy) for the
  whole folder; it checks only the contacts-ui templates today.
- **test_core.py:** the contract asks to extend `apps/flamingo/test_core.py` near line 477 with
  the nav test; it lives in `test_s1_views.TenancyTests.test_the_menu_shows_kontakter_only_when_utskick_is_on`
  instead (apps/flamingo is not this agent's).

## Import (done by the importer agent)

Files: `importer.py`, `xlsx2csv.py`, `app_views/imports.py`, templates
`flamingo/app/kontakter/import/{_head,_steps,_block,_nav,upload,map,consent,review,wait,done}.html`
(a subfolder, not `kontakter/import_*.html` as I.1 and the table above say: the importer was
assigned `kontakter/import/*`), `static/css/flamingo-app-import.css` (`.fl-im-`),
`static/js/flamingo-app-import.js`, `static/utskick/kontakter-mall.xlsx` (five fictional rows,
PTS 070-174 numbers, `.example` addresses), `test_s1_import.py`. No shared module was changed.

```python
importer.import_chunk(now=None, seconds=15) -> {"jobs", "rows"}   # tick phase 8 (counts only)
importer.work_exists(now=None) -> bool          # one EXISTS: converting/analysing/importing jobs
                                                # for the tick, plus request jobs orphaned > 10 min
importer.cleanup(now=None) -> {"abandoned", "files", "jobs", "orphans"}   # utskick_daily (E.7)
importer.cancel(job) -> bool                    # files deleted at once, imported rows stay
importer.result(job) / importer.preview(job)    # final counts / the review counts
#   keys: new, updated, suppressed (incl. marked), marked, errors (incl. limit), conflicts,
#   limit, company_freemail; ImportJob.counts also holds "columns" (stats, no values),
#   "preview" and "failure" (text)
```

Behaviour worth knowing:
- Files: the upload is saved under `ImportJob.file` storage (`PRIVATE_MEDIA_ROOT/utskick-import/`),
  converted once to a normalised CSV (row number first, no newlines in cells) and the original is
  deleted right away. Personnummer columns (header word, or most values pass the pnr + Luhn test)
  are blanked in the CSV and in `sample`. Done, failed and cancelled jobs lose their files after
  24 h (`cleanup`), abandoned ones are cancelled after 7 days, rows go after 90 days.
- xlsx: `zipfile` checks first (100 MB total, ratio 100, sharedStrings 30 MB), then
  `python -I <path>/xlsx2csv.py in out` with a 60 s timeout, an empty environment and
  `RLIMIT_AS` 512 MB set by the child itself (Linux; macOS refuses to lower it). It is a script
  path, not `-m apps.utskick.xlsx2csv`: with `-I` Python cannot find the `apps` package. xlsx over
  1 MB is converted by the tick (`converting`).
- In request: up to 2 000 rows, each step (analysis, import) gets at most `IN_REQUEST_SECONDS`
  (20 s); what is left goes to the tick (`in_request` becomes False) and the page polls
  `?status=json`. Measured locally: 2 000 new rows with consent on both channels, a list and a
  tag take about 14 s in the request.
- Rows without a mobile number and without an email are errors (they cannot be matched, so the
  same file twice would duplicate them). Rows whose every address is suppressed, and rows marked
  in an Avregistrerad column, are not imported; the marked ones are suppressed with reason
  `import` (`suppression.suppress`, which also unsubscribes an existing contact).
- The consent choice is applied with `consent.set_status(..., source="import",
  evidence=where)` only on the ticked channels; refusals (locked, suppressed) leave the status.
  The actor is whoever pressed "Importera" (`created_by` / `created_as_staff` are set then), so
  staff in view-as show as "ADX (Giovanni)" in the consent log, also for tick-run imports.
- Agency alert (once per job) when more than 1 000 rows are imported as "De har sagt ja" or
  "befintliga kunder"; no mail to the customer.

## Requests from importer

- **Integration (tick):** phase 8 calls `importer.import_chunk(now, seconds=min(15, time left))`
  after the sending phases and adds `jobs`/`rows` to the summary; `work_exists` includes
  `importer.work_exists(now)`. The tick's `RLIMIT_AS` does not cover the xlsx child (it sets its
  own).
- **Integration (daily):** `utskick_daily` calls `importer.cleanup(now)` and logs the counts.
- **Integration:** add `css/flamingo-app-import.css` and `js/flamingo-app-import.js` to the
  static_version list in `apps/manage/context_processors.py`.
- **Integration (guards):** the import templates are in the subfolder
  `templates/flamingo/app/kontakter/import/`; `test_s1_guards` should walk the folder
  recursively. `test_s1_import.ImportTemplateGuardTests` covers them meanwhile.
- **Contacts UI:** the "Importera" button and the empty-state link go to `flamingo:app_import`.
- **Lead / README:** I.1 names `kontakter/import_{upload,map,...}.html`; the files are
  `kontakter/import/*.html`. J S1 says `python -I -m apps.utskick.xlsx2csv`; it runs as a script
  path (see above).
- **Local dev database:** two cancelled import jobs (pk 1 and 2, no files left) on the demo
  account, from the 375 px check. Note for QA: the local server runs uvicorn `--reload`, which
  does not reload templates; touch a `.py` file after a template change.

## Integration (done by the integration agent)

Every "Requests from ..." item above is applied. New files: `manage_views.py` (replaced),
`sending/__init__.py`, `sending/tick.py`, `retention.py`, `demo.py`,
`management/commands/utskick_tick.py`, `utskick_daily.py`, `templates/manage/utskick/
{_customer_card,overview,end}.html`, `static/css/manage-utskick.css`, `test_s1_lp.py`,
`test_s1_tick.py`, `test_s1_guards.py`, `test_s1_manage.py`, `server/crontab.d/adx-utskick`,
`server/logrotate.d/adx-utskick`, `server/aws-utskick-role.sh` (not run). The three stub templates
are deleted. Shared modules extended: `importer.recover(now)` (tick phase 1) and
`importer.delete_account_jobs(account)` (end and demo reset).

Outside the app: `apps/flamingo/public_views.py` (LeadForm consent fields, `_lp_consent`,
`_capture`, thanks `?epost=1`), `pagebuilder/render.py` (`lp_consent` in the form context),
`templates/flamingo/lp/ren/blocks/form.html`, `thanks.html`, `static/css/flamingo-lp-ren.css`
(`.rn-consent`, `.rn-check`), `templates/flamingo/_customer_panel.html` (the card),
`templates/flamingo/app/onboarding/settings.html` ("Inloggningar hos"),
`templates/manage/flamingo/overview.html` (link), `flamingo_demo.py` (reset and seed hooks),
`apps/analytics/middleware.py`, `apps/core/views.py` (robots), `apps/common/sentry.py`,
`apps/manage/context_processors.py`, `apps/manage/forms.py`, `apps/monitor/.../monitor_check.py`,
`config/test_runner.py` (parallel workers start; `UTSKICK_TICK_MAX_MB=0`), `server/deploy.sh`
(tick lock), `server/README.md`, the tests named in README "S1 as built", both READMEs.

Delete this file when S1 ships; README "S1 as built" holds the deviations.
