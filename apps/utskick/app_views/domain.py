"""
Egen domän och svaren på mejl (README J S3 "Domain flow", B.3, I.9, D.6).

    utskick_domain   utskick/installningar/doman/                     app_utskick_domain
                     GET sidan, POST action=claim | sender | check | help | remove |
                     reply_mode | reply_send
    reply_confirm    utskick/installningar/svarsadress/<token>/       app_utskick_reply_confirm
                     GET en knapp, POST bekräftar den egna svarsadressen

utskick_domain (flamingo/app/utskick/domain.html, fliken "settings"):
avsändaren just nu (egen domän eller ADX-domänen med månadens tak),
domänen och avsändaradressen in (anspråksreglerna i email.domains),
posterna som .fl-kv-block med en Kopiera per värde (tre DKIM-CNAME, MX och
SPF för studs, DMARC-rekommendationen) och läget per post, "Kontrollera
igen" (en gång i minuten), "Be ADX om hjälp" (larm till byrån, inget mejl
till kunden) och "Ta bort". Svar på mejl: Inkorgen (standard) eller en
egen adress, som måste ligga på en verifierad domän eller bekräftas med en
engångslänk. Länken skickas bara från en knapp som säger att kunden mejlas
("Skicka bekräftelselänken till anna@exempelror.example"); byrån i
kundvyn kryssar i att den skickar som ADX (I.4), och varje utskick av
länken loggas med användaren.

reply_confirm: GET bekräftar aldrig (förhandsvisningar öppnar länkar); POST
med Djangos CSRF sätter own_reply_to_confirmed_at när token gäller
tokens.read_reply_confirm(token, own_reply_to) för det här kontot.
Länken bevisar brevlådan, inloggningen kontot (S3-avvikelse 7).
"""

import logging

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .. import alerts, keys, limits, normalize, optin, tokens
from ..access import actor_for, utskick_view
from ..email import domains, transport
from ..models import Utskick, UtskickSettings
from ..sending import email as email_loop
from ..sending import health, state
from . import render_utskick

logger = logging.getLogger(__name__)

#: Bekräftelselänkar för en egen svarsadress per konto och dygn.
REPLY_CONFIRMS_PER_DAY = 3

STAFF_TEXT = "Jag skickar det här som ADX åt {name}."
STAFF_MISSING = "Kryssa i att du skickar som ADX åt {name}."
ASKED_TEXT = "ADX har fått din fråga."
CHECK_WAIT_TEXT = "Vänta en minut innan du kontrollerar igen."
CLAIMED_TEXT = "Domänen är tillagd. Lägg in posterna nedan hos den som sköter din DNS."
VERIFIED_TEXT = "Domänen är verifierad. Mejlen kan komma från {address}."
PENDING_TEXT = "Posterna stämmer inte än. Det kan ta en stund innan en ändring syns."
REMOVED_TEXT = "Domänen är borttagen. Mejlen kommer från ADX-domänen."
SENDER_TEXT = "Avsändaren är sparad."
REPLY_INBOX_TEXT = "Svar på mejl hamnar i Inkorgen."
REPLY_OWN_OK_TEXT = "Svar på mejl går till {address}."
REPLY_OWN_CONFIRM_TEXT = (
    "Adressen är sparad. Skicka bekräftelselänken och klicka på den, så går svaren dit."
)
REPLY_ADDRESS_TEXT = "Skriv en giltig e-postadress."
REPLY_SENT_TEXT = "Bekräftelselänken är skickad till {address}. Den gäller i sju dagar."
REPLY_LIMIT_TEXT = "Du har skickat länken tre gånger i dag. Försök igen i morgon."
REPLY_NONE_TEXT = "Skriv den egna adressen först."
CONFIRMED_TEXT = "Svarsadressen är bekräftad. Svar på mejl går till {address}."
CONFIRM_BAD_TEXT = (
    "Länken gäller inte längre. Skicka en ny bekräftelselänk under Avsändare och svar."
)
DEMO_TEXT = "Demokontot visar bara hur sidan ser ut. Inget här ändras eller skickas."
CHECK_LABELS = {
    "ok": ("OK", "ok"),
    "missing": ("Saknas", "warn"),
    "wrong": ("Fel värde", "stop"),
    "unknown": ("Gick inte att läsa", "muted"),
}


def _name(request):
    row = getattr(request, "utskick_settings", None)
    return (row.display_name if row else "") or "kunden"


def _page():
    return redirect(reverse("flamingo:app_utskick_domain"))


def _record_rows(row):
    """Posterna med läget från senaste kontrollen, för mallen."""
    checks = row.checks if isinstance(row.checks, dict) else {}
    out = []
    for record in domains.records(row):
        state_key = (checks.get(record.purpose) or {}).get("state", "")
        label, tone = CHECK_LABELS.get(state_key, ("Inte kontrollerad", "muted"))
        if record.purpose == "dmarc" and state_key != "ok":
            label, tone = ("Rekommenderas", "info")
        out.append({"record": record, "label": label, "tone": tone})
    return out


def _context(request, account, now, form=None, errors=None):
    row_settings = UtskickSettings.objects.get(pk=request.utskick_settings.pk)
    summary = domains.summary(account, now)
    row = summary["domain"]
    actor = actor_for(request)
    adx_paused = list(
        Utskick.objects.listed()
        .filter(
            account=account,
            status=Utskick.Status.PAUSED_CAP,
            pause_reason=Utskick.PauseReason.ADX_MAIL_CAP,
        )
        .order_by("-status_changed_at")[:5]
    )
    own = str(row_settings.own_reply_to or "")
    own_ok = bool(own) and email_loop.own_reply_ok(account, row_settings)
    can_check = bool(row) and (
        row.checked_at is None or now - row.checked_at >= domains.CHECK_EVERY
    )
    return {
        "summary": summary,
        "row": row,
        "records": _record_rows(row) if row else [],
        "can_check": can_check,
        "adx_paused": adx_paused,
        "form": form or {},
        "errors": errors or {},
        "display_name": row_settings.display_name,
        "reply_mode": row_settings.email_reply_mode,
        "own_reply_to": own,
        "own_ok": own_ok,
        "own_confirmed": bool(row_settings.own_reply_to_confirmed_at),
        "email_live": state.email_live(),
        "blocked": health.is_blocked(account),
        "is_staff_actor": actor.staff,
        "staff_text": STAFF_TEXT.format(name=_name(request)),
        "pending_days": domains.SenderDomain.PENDING_DAYS,
    }


def _render(request, account, now, form=None, errors=None, status=200):
    return render_utskick(
        request,
        "flamingo/app/utskick/domain.html",
        "settings",
        _context(request, account, now, form, errors),
        status=status,
    )


# ---------------------------------------------------------------------------
# Domänen
# ---------------------------------------------------------------------------


def _claim(request, account, now):
    form = {
        "domain": str(request.POST.get("domain") or "").strip()[:253],
        "from_local": str(request.POST.get("from_local") or "").strip()[:64],
        "from_name": str(request.POST.get("from_name") or "").strip()[:80],
    }
    try:
        row = domains.claim(
            account,
            form["domain"],
            from_local=form["from_local"] or "hej",
            from_name=form["from_name"] or _name(request),
            user=request.user,
            now=now,
        )
    except domains.DomainRefused as exc:
        return _render(request, account, now, form, {"domain": exc.text}, status=400)
    logger.info(
        "Utskick: domän %s lades till av användare %s (byrån: %s)",
        row.pk,
        request.user.pk,
        actor_for(request).staff,
    )
    messages.success(request, CLAIMED_TEXT)
    return _page()


def _sender(request, account, now):
    row = domains.current(account)
    if row is None:
        return _page()
    try:
        domains.update_sender(
            row,
            from_local=request.POST.get("from_local") or row.from_local,
            from_name=request.POST.get("from_name") or row.from_name,
        )
    except domains.DomainRefused as exc:
        form = {
            "from_local": request.POST.get("from_local", ""),
            "from_name": request.POST.get("from_name", ""),
        }
        return _render(request, account, now, form, {"sender": exc.text}, status=400)
    messages.success(request, SENDER_TEXT)
    return _page()


def _check(request, account, now):
    row = domains.current(account)
    if row is None:
        return _page()
    if row.checked_at and now - row.checked_at < domains.CHECK_EVERY:
        messages.info(request, CHECK_WAIT_TEXT)
        return _page()
    row = domains.check(row, now=now)
    if row.status == domains.STATUS.VERIFIED:
        messages.success(request, VERIFIED_TEXT.format(address=row.from_address))
    elif row.status == domains.STATUS.EXPIRED:
        messages.info(request, "Domänen hann gå ut. Lägg till den igen.")
    else:
        messages.info(request, PENDING_TEXT)
    return _page()


def _help(request, account, now):
    from django.urls import reverse

    row = domains.current(account)
    actor = actor_for(request)
    # Larmen bär pk, aldrig personers namn (alerts.py); domänen är kundens.
    where = (
        reverse("manage:utskick_domain_admin", args=[row.pk])
        if row is not None
        else reverse("manage:utskick_overview") + "#epost-domaner"
    )
    lines = [
        f"Konto {account.pk} ber om hjälp med sin avsändardomän.",
        f"Domän: {row.domain if row else 'ingen än'}"
        + (f" ({row.get_status_display()})" if row else "")
        + ".",
        f"Av användare {getattr(actor.user, 'pk', '')}"
        + (" (byrån i kundvyn)." if actor.staff else "."),
        f"Posterna och läget: {where}",
    ]
    alerts.agency(
        "Utskick: kunden ber om hjälp med sin domän",
        lines,
        once=f"domain_help:{account.pk}",
        window="day",
        now=now,
    )
    messages.success(request, ASKED_TEXT)
    return _page()


def _remove(request, account, now):
    row = domains.current(account)
    if row is None:
        return _page()
    try:
        domains.remove(row, user=request.user, now=now)
    except domains.DomainRefused as exc:
        messages.error(request, exc.text)
        return _page()
    messages.success(request, REMOVED_TEXT)
    return _page()


# ---------------------------------------------------------------------------
# Svaren
# ---------------------------------------------------------------------------


def _reply_mode(request, account, now):
    mode = request.POST.get("reply_mode")
    if mode not in (UtskickSettings.REPLY_INBOX, UtskickSettings.REPLY_OWN):
        return _page()
    row = UtskickSettings.objects.get(account=account)
    if mode == UtskickSettings.REPLY_INBOX:
        UtskickSettings.objects.filter(pk=row.pk).update(email_reply_mode=mode)
        messages.success(request, REPLY_INBOX_TEXT)
        return _page()
    try:
        address = normalize.email(request.POST.get("own_reply_to"))
    except normalize.InvalidValue:
        address = ""
    if not address:
        form = {"own_reply_to": str(request.POST.get("own_reply_to") or "")[:254]}
        return _render(request, account, now, form, {"own_reply_to": REPLY_ADDRESS_TEXT}, 400)
    values = {"email_reply_mode": mode, "own_reply_to": address}
    if address != (row.own_reply_to or "").lower():
        values["own_reply_to_confirmed_at"] = None
    UtskickSettings.objects.filter(pk=row.pk).update(**values)
    row.refresh_from_db()
    if email_loop.own_reply_ok(account, row):
        messages.success(request, REPLY_OWN_OK_TEXT.format(address=address))
    else:
        messages.info(request, REPLY_OWN_CONFIRM_TEXT)
    return _page()


def _confirm_mail(account, address, token):
    name, from_addr = email_loop.from_for(account)
    url = optin.absolute(reverse("flamingo:app_utskick_reply_confirm", args=[token]))
    text = (
        f"Hej,\n\nnågon har angett {address} som adress för svar på mejl från "
        f"{name} i ADX Flamingo.\n\n"
        f"Bekräfta adressen här (du loggar in i Flamingo först): {url}\n\n"
        "Länken gäller i sju dagar. Om det inte var du kan du låta bli att klicka.\n"
    )
    return transport.OutgoingMail(
        to=address,
        from_name=name,
        from_addr=from_addr,
        subject="Bekräfta svarsadressen för dina utskick",
        text=text,
    )


def _reply_send(request, account, now):
    """Skicka bekräftelselänken till den egna adressen. Bara från knappen
    som säger att kunden mejlas; byrån i kundvyn kryssar i rutan (I.4)."""
    actor = actor_for(request)
    row = UtskickSettings.objects.get(account=account)
    address = str(row.own_reply_to or "").strip().lower()
    if not address:
        messages.error(request, REPLY_NONE_TEXT)
        return _page()
    if actor.staff and request.POST.get("som_adx") != "1":
        messages.error(request, STAFF_MISSING.format(name=_name(request)))
        return _page()
    if limits.hit("reply_confirm", str(account.pk), limits.day_window(now), REPLY_CONFIRMS_PER_DAY):
        messages.error(request, REPLY_LIMIT_TEXT)
        return _page()
    token = tokens.reply_confirm_token(account.pk, address, now)
    try:
        sent = email_loop.deliver(
            account, _confirm_mail(account, address, token), kind=transport.REPLY_CONFIRM, now=now
        )
    except keys.KeyMismatch:
        messages.error(request, email_loop.ERROR_TEXTS["keys"])
        return _page()
    logger.info(
        "Utskick: bekräftelselänk för svarsadressen, konto %s, användare %s (byrån: %s): %s",
        account.pk,
        request.user.pk,
        actor.staff,
        "skickad" if sent.ok else (sent.error or "oklart"),
    )
    if sent.ok:
        messages.success(request, REPLY_SENT_TEXT.format(address=address))
    else:
        messages.error(request, email_loop.error_text(sent))
    return _page()


ACTIONS = {
    "claim": _claim,
    "sender": _sender,
    "check": _check,
    "help": _help,
    "remove": _remove,
    "reply_mode": _reply_mode,
    "reply_send": _reply_send,
}


@utskick_view
@require_http_methods(["GET", "HEAD", "POST"])
def utskick_domain(request, account):
    now = timezone.now()
    if request.method == "POST":
        handler = ACTIONS.get(str(request.POST.get("action") or ""))
        if handler is None:
            return _page()
        if account.is_demo:
            # Demot når aldrig SES, DNS eller byråns larm (D12).
            messages.info(request, DEMO_TEXT)
            return _page()
        return handler(request, account, now)
    return _render(request, account, now)


@utskick_view
@require_http_methods(["GET", "HEAD", "POST"])
def reply_confirm(request, account, token):
    """Bekräfta den egna svarsadressen (I.9). GET visar en knapp; bara POST
    bekräftar, och bara för det här kontots adress."""
    row = UtskickSettings.objects.get(account=account)
    address = str(row.own_reply_to or "").strip().lower()
    valid = bool(address) and tokens.read_reply_confirm(token, address) == account.pk
    if request.method == "POST":
        if not valid:
            messages.error(request, CONFIRM_BAD_TEXT)
            return _page()
        now = timezone.now()
        UtskickSettings.objects.filter(pk=row.pk).update(own_reply_to_confirmed_at=now)
        logger.info(
            "Utskick: svarsadressen bekräftad för konto %s av användare %s",
            account.pk,
            request.user.pk,
        )
        messages.success(request, CONFIRMED_TEXT.format(address=address))
        return _page()
    return render_utskick(
        request,
        "flamingo/app/utskick/reply_confirm.html",
        "settings",
        {"valid": valid, "address": address, "token": token, "bad_text": CONFIRM_BAD_TEXT},
    )
