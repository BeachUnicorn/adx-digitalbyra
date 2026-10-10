"""
Byråns sida av Kontakter och Utskick i /manage/ (README I.1 "Manage
views", B.1 Switchboard, D.8, E.7, H.9).

    card_context(customer)        kundkortets panel (utskick_tags.utskick_card)
    overview                      /manage/utskick/: kunderna, kön, ticken,
                                  nycklarna, nödstoppet och avtalet
    switch                        /manage/utskick/nodstopp/ (POST): klarmarkeringar,
                                  brytarna för sms och e-post, nödstoppet
    dpa_publish                   /manage/utskick/avtal/ (POST): en ny version av
                                  biträdesavtalet ur sidan /bitradesavtal/
    customer_update               /manage/kunder/<pk>/utskick/ (POST): på/av,
                                  namn, adress, gränser och stopp för kunden
    customer_end                  /manage/kunder/<pk>/utskick/radera/: "Avsluta
                                  utskick och radera allt", med kundens namn skrivet

Inget här mejlar kunden: "Utskick är aktiverat för Exempelrör. Kunden har
inte mejlats." Att slå på kräver Flamingo på. Klarmarkeringarna kräver en
anteckning; sms_enabled kräver links_ready_at och sms_inbound_ready_at,
email_enabled kräver email_ready_at och UTSKICK_EMAIL_LIVE. Att stänga av
går alltid, utan anteckning (det är nödstoppet). "Stoppa all sändning"
tar också bort klarmarkeringen för bekräftelsemejlen (optin.due skickar
bara med den), och kundens "Stoppa all sändning för kunden" stoppar
kundens bekräftelsemejl.

En avstängning (utskick av, eller "Stoppa all sändning") pausar från S2
kundens schemalagda och pågående utskick (D.8); i S1 finns inga utskick, så
den stänger bara av Kontakter och anmälan för kunden. Inget återupptas av
sig självt när utskick slås på igen.
"""

import hashlib
import html
import logging
import re

from django import forms
from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.projects.access import staff_required
from apps.projects.models import Customer

from . import importer, keys, optin
from .access import (
    current_dpa,
    is_enabled,
    latest_acceptance,
    settings_for,
    suggest_public_slug,
    validate_public_slug,
)
from .email import transport
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    ConsentLog,
    Contact,
    ContactList,
    DpaVersion,
    ExportLog,
    FieldDef,
    ImportJob,
    SignupForm,
    Switchboard,
    Tag,
    UtskickSettings,
    default_consent_text,
)

logger = logging.getLogger(__name__)

#: Sidan som biträdesavtalet publiceras från (README D6, H.9).
DPA_SLUG = "bitradesavtal"
#: Kontakter som tas bort per omgång i "Avsluta utskick och radera allt".
DELETE_BATCH = 2000

#: Klarmarkeringarna: fältet, rubriken och vad som ska vara gjort (README J).
READY = (
    (
        "doi_ready_at",
        "Bekräftelsemejl",
        "SES i eu-west-1 har gett produktionsåtkomst, och en provanmälan på en intern "
        "kund kom fram med dkim=pass för utskick.adx.se och gick att bekräfta.",
    ),
    (
        "links_ready_at",
        "Länkvärdarna",
        "k.adx.se och klick.adx.se svarar med giltigt certifikat.",
    ),
    (
        "sms_inbound_ready_at",
        "Inkommande sms",
        "46elks skickar svaren hit, och STOPP från en riktig telefon har provats.",
    ),
    (
        "email_ready_at",
        "E-postutskick",
        "Konfigurationsset, köer, inkommande post och kontrollerna i drift är klara.",
    ),
)
READY_FIELDS = {field for field, _, _ in READY}

NOTE_TEXT = "Skriv en anteckning: vad som är kontrollerat, och hur."
SMS_NOT_READY_TEXT = "Sms-utskick kräver att länkvärdarna och inkommande sms är klarmarkerade."
EMAIL_NOT_READY_TEXT = (
    "E-postutskick kräver att e-posten är klarmarkerad och att UTSKICK_EMAIL_LIVE är på."
)
FLAMINGO_OFF_TEXT = "Slå på ADX Flamingo för kunden först. Utskick kräver det."
STOP_ALL_TEXT = (
    "All sändning är stoppad, bekräftelsemejlen också. Inget skickas förrän brytarna "
    "slås på och bekräftelsemejlen klarmarkeras igen."
)


def _back(customer_pk):
    return redirect(reverse("manage:customer_detail", args=[customer_pk]) + "#utskick")


def _who(user):
    if user is None:
        return ""
    return user.first_name or user.get_username()


# ---------------------------------------------------------------------------
# Kundkortet
# ---------------------------------------------------------------------------


def _sms_line(customer):
    """Sms-kanalens läge för kundkortet."""
    from apps.sms.models import SmsAccount

    sms = SmsAccount.objects.filter(customer=customer).first()
    if sms is None or not sms.is_enabled:
        return "Sms är inte aktiverat för kunden (SMS-panelen nedan)."
    names = ", ".join(sms.senders) or "inget avsändarnamn"
    return f"Sms: aktiverat ({names})."


def _dpa_line(account):
    if account.is_demo:
        return "Demokontot behöver inget biträdesavtal."
    acceptance = latest_acceptance(account)
    if acceptance is None:
        return "Biträdesavtalet är inte godkänt än."
    from .app_views.contacts import date_text
    from .app_views.dpa import who_accepted

    text = (
        f"Biträdesavtal {acceptance.version.version} godkänt av {who_accepted(acceptance)} "
        f"{date_text(acceptance.accepted_at)}."
    )
    if acceptance.staff_statement:
        text += f" {acceptance.staff_statement}"
    if not acceptance.version.is_current:
        text += " En nyare version finns: kunden behöver godkänna den för nya kontakter."
    return text


def card_context(customer):
    """Det kundkortets utskickspanel behöver (utskick_tags.utskick_card,
    manage/utskick/_customer_card.html)."""
    from apps.flamingo.models import FlamingoAccount

    from .app_views.contacts import export_text

    account = (
        FlamingoAccount.objects.filter(customer=customer)
        .select_related("customer", "utskick__enabled_by")
        .first()
    )
    context = {
        "customer": customer,
        "account": account,
        "settings": None,
        "saved": False,
        "enabled": False,
        "flamingo_on": bool(account and account.is_enabled),
    }
    if account is None:
        return context
    row = settings_for(account)
    context.update(
        {
            "settings": row,
            "saved": bool(row.pk),
            "enabled": is_enabled(account, row),
            "suggested_slug": row.public_slug or suggest_public_slug(customer.name),
            "display_name": row.display_name or customer.name[:80],
        }
    )
    if row.pk:
        last_export = ExportLog.objects.filter(account=account).select_related("user").first()
        context.update(
            {
                "contact_count": Contact.objects.filter(account=account).count(),
                "dpa_line": _dpa_line(account),
                "sms_line": _sms_line(customer),
                "export_line": export_text(last_export),
                "enabled_by": _who(row.enabled_by),
            }
        )
    return context


class CustomerCardForm(forms.Form):
    """Kundkortets fält. En ruta som inte skickas är av (som Flamingos)."""

    is_enabled = forms.BooleanField(required=False)
    display_name = forms.CharField(max_length=80, required=False)
    public_slug = forms.CharField(max_length=40, required=False)
    contact_limit = forms.IntegerField(min_value=1, max_value=1_000_000, required=False)
    email_daily_cap = forms.IntegerField(min_value=0, max_value=1_000_000, required=False)
    sending_blocked = forms.BooleanField(required=False)
    blocked_reason = forms.CharField(max_length=200, required=False)

    def __init__(self, *args, row=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.row = row

    def clean_display_name(self):
        return " ".join(str(self.cleaned_data.get("display_name") or "").split())

    def clean_public_slug(self):
        raw = str(self.cleaned_data.get("public_slug") or "").strip().lower()
        if not raw:
            return ""
        return validate_public_slug(raw, exclude_pk=self.row.pk if self.row else None)

    def clean(self):
        data = super().clean()
        reason = " ".join(str(data.get("blocked_reason") or "").split())
        data["blocked_reason"] = reason
        if data.get("sending_blocked") and not reason:
            self.add_error("blocked_reason", "Skriv varför sändningen stoppas.")
        return data


def _first_error(form):
    for errors in form.errors.values():
        for error in errors:
            return error
    return "Något i formuläret stämmer inte."


def _consent_texts_follow(row, old_name, new_name):
    """Samtyckestexterna följer med ett nytt namn, om kunden inte skrivit
    egna (texten är fortfarande standardtexten med det gamla namnet)."""
    for channel, attr in ((CHANNEL_SMS, "consent_text_sms"), (CHANNEL_EMAIL, "consent_text_email")):
        current = getattr(row, attr)
        if not current or current == default_consent_text(channel, old_name):
            setattr(row, attr, default_consent_text(channel, new_name))


@staff_required
@require_POST
def customer_update(request, pk):
    """Kundkortets panel: slå på eller av utskick och spara namn, adress,
    kontaktgräns, mejltak och stoppet. Kunden mejlas inte."""
    customer = get_object_or_404(Customer, pk=pk)
    from apps.flamingo.models import FlamingoAccount

    account = FlamingoAccount.objects.filter(customer=customer).first()
    row = settings_for(account) if account else None
    form = CustomerCardForm(request.POST, row=row)
    if not form.is_valid():
        messages.error(request, _first_error(form))
        return _back(customer.pk)
    data = form.cleaned_data
    enable = data["is_enabled"]
    if row is None or not row.pk:
        if not enable:
            messages.info(request, f"Utskick är inte aktiverat för {customer.name}.")
            return _back(customer.pk)
        if account is None or not account.is_enabled:
            messages.error(request, FLAMINGO_OFF_TEXT)
            return _back(customer.pk)
    elif enable and not row.is_enabled and not account.is_enabled:
        messages.error(request, FLAMINGO_OFF_TEXT)
        return _back(customer.pk)

    now = timezone.now()
    new_name = data["display_name"] or row.display_name or customer.name[:80]
    with transaction.atomic():
        if not row.pk:
            name = new_name[:80]
            slug = data["public_slug"] or suggest_public_slug(customer.name)
            row = UtskickSettings(
                account=account,
                display_name=name,
                public_slug=slug,
                consent_text_sms=default_consent_text(CHANNEL_SMS, name),
                consent_text_email=default_consent_text(CHANNEL_EMAIL, name),
            )
        else:
            row = UtskickSettings.objects.select_for_update().get(pk=row.pk)
            if new_name != row.display_name:
                _consent_texts_follow(row, row.display_name, new_name)
                row.display_name = new_name[:80]
            if data["public_slug"]:
                row.public_slug = data["public_slug"]
        if data["contact_limit"]:
            row.contact_limit = data["contact_limit"]
        if data["email_daily_cap"] is not None:
            row.email_daily_cap = data["email_daily_cap"]
        was_blocked = bool(row.pk and row.sending_blocked)
        row.sending_blocked = data["sending_blocked"]
        row.blocked_reason = data["blocked_reason"] if data["sending_blocked"] else ""
        was_on = row.is_enabled
        row.is_enabled = enable
        if enable and not was_on:
            row.enabled_at = now
            row.enabled_by = request.user
            row.disabled_at = None
        elif was_on and not enable:
            row.disabled_at = now
        row.save()
        # --- S2, sändningsmotorn (D.8): ett avstängt eller stoppat konto pausar
        # sina schemalagda, frysande och pågående utskick i samma transaktion.
        # Att slå på igen fortsätter ingenting av sig självt.
        from .models import Utskick
        from .sending import state as sending_state

        if was_on and not enable:
            sending_state.pause_account(account, Utskick.PauseReason.ACCOUNT_DISABLED, now)
        elif row.sending_blocked and not was_blocked:
            sending_state.pause_account(account, Utskick.PauseReason.BLOCKED, now)
        # --- slut S2
    logger.info(
        "Utskick för konto %s sparat av användare %s (på: %s, stoppat: %s)",
        account.pk,
        request.user.pk,
        row.is_enabled,
        row.sending_blocked,
    )
    if enable and not was_on:
        messages.success(
            request, f"Utskick är aktiverat för {customer.name}. Kunden har inte mejlats."
        )
    elif was_on and not enable:
        messages.success(
            request,
            f"Utskick är avstängt för {customer.name}. Kontakterna finns kvar. "
            "Kunden har inte mejlats.",
        )
    else:
        messages.success(request, f"Utskick för {customer.name} är sparat.")
    return _back(customer.pk)


# ---------------------------------------------------------------------------
# Avsluta utskick och radera allt (E.7)
# ---------------------------------------------------------------------------


def _same_name(typed, name):
    def norm(value):
        return " ".join(str(value or "").split()).casefold()

    return bool(norm(typed)) and norm(typed) == norm(name)


def end_counts(account):
    return {
        "kontakter": Contact.objects.filter(account=account).count(),
        "listor": ContactList.objects.filter(account=account).count(),
        "taggar": Tag.objects.filter(account=account).count(),
        "falt": FieldDef.objects.filter(account=account).count(),
        "importer": ImportJob.objects.filter(account=account).count(),
    }


def _end_utskick(account):
    """S2-delen av "Avsluta utskick och radera allt": mottagarna i omgångar
    (en stor kaskad på en liten server låser länge), sedan resten. Svaren
    tas bort med sina förfrågningar i Inkorgen (Lead med source reply bär
    numret och senaste svaret), inte bara trådarna."""
    from apps.flamingo.models import Lead

    from .models import Click, LinkCode, Recipient, Thread, Utskick

    Click.objects.filter(account=account).delete()
    LinkCode.objects.filter(account=account).delete()
    while True:
        batch = list(
            Recipient.objects.filter(utskick__account=account).values_list("pk", flat=True)[
                :DELETE_BATCH
            ]
        )
        if not batch:
            break
        Recipient.objects.filter(pk__in=batch).delete()
    Thread.objects.filter(account=account).delete()
    Lead.objects.filter(account=account, source=Lead.SOURCE_REPLY).delete()
    Utskick.objects.filter(account=account).delete()


# --- S3 (sändnings-byggaren) ------------------------------------------------


def _end_email(account):
    """S3-delen av "Avsluta utskick och radera allt": mejlens bilder (filerna
    går med raderna efter commit) och avsändardomänerna. Identiteten hos SES
    tas bort efter commit och bara när appen skapade den (B.3); en domän som
    redan gått ut eller tagits bort har ingen identitet kvar."""
    from .email import domains
    from .models import EmailImage, SenderDomain

    EmailImage.objects.filter(account=account).delete()
    rows = list(SenderDomain.objects.filter(account=account))
    for row in rows:
        if row.ses_created and row.status not in (
            SenderDomain.Status.REMOVED,
            SenderDomain.Status.EXPIRED,
        ):
            transaction.on_commit(lambda row=row: domains.delete_identity(row))
    SenderDomain.objects.filter(account=account).delete()


# --- slut S3 ---------------------------------------------------------------------


def end_account(account, user=None):
    """Ta bort kontots kontakter, listor, taggar, fält, importer (med filer)
    och anmälningssidan, och stäng av utskick. Spärrlistan och
    samtyckesloggen finns kvar som pseudonymt bevis (kontakten blir null,
    kundens anteckningar töms). Från S2 också utskicken, mottagarna,
    sms-koderna, klicken och svarstrådarna. Returnerar antalen som togs bort."""
    counts = end_counts(account)
    with transaction.atomic():
        # --- S2, sändningsmotorn (E.7): utskick, mottagare, sms-koder, klick
        # och svarstrådar. Spärrlistan och samtyckesloggen finns kvar.
        _end_utskick(account)
        # --- slut S2
        # --- S3 (sändnings-byggaren): mejlens bilder och avsändardomänerna,
        # efter utskicken (sender_domain är RESTRICT).
        _end_email(account)
        # --- slut S3
        importer.delete_account_jobs(account)
        SignupForm.objects.filter(account=account).delete()
        # Kundens anteckningar på beviset töms, som vid en GDPR-borttagning (H.4).
        ConsentLog.objects.filter(account=account).exclude(evidence="").update(evidence="")
        while True:
            batch = list(
                Contact.objects.filter(account=account).values_list("pk", flat=True)[:DELETE_BATCH]
            )
            if not batch:
                break
            Contact.objects.filter(pk__in=batch).delete()
        ContactList.objects.filter(account=account).delete()
        Tag.objects.filter(account=account).delete()
        FieldDef.objects.filter(account=account).delete()
        UtskickSettings.objects.filter(account=account, is_enabled=True).update(
            is_enabled=False, disabled_at=timezone.now()
        )
    logger.info(
        "Utskick avslutat för konto %s av användare %s: %s",
        account.pk,
        getattr(user, "pk", None),
        counts,
    )
    return counts


@staff_required
def customer_end(request, pk):
    """Avsluta utskick och radera allt. Byrån skriver kundens namn för att
    bekräfta. Kunden mejlas inte."""
    customer = get_object_or_404(Customer, pk=pk)
    from apps.flamingo.models import FlamingoAccount

    account = FlamingoAccount.objects.filter(customer=customer).first()
    if account is None or not UtskickSettings.objects.filter(account=account).exists():
        messages.info(request, f"{customer.name} har inga utskick att avsluta.")
        return _back(customer.pk)
    error = ""
    if request.method == "POST":
        if _same_name(request.POST.get("namn"), customer.name):
            counts = end_account(account, request.user)
            from .app_views.contacts import count_text

            removed = count_text(counts["kontakter"], "kontakt", "kontakter")
            messages.success(
                request,
                f"Utskick är avslutat för {customer.name}: {removed} togs bort. Spärrlistan "
                "och samtyckesloggen finns kvar. Kunden har inte mejlats.",
            )
            return _back(customer.pk)
        error = "Namnet stämmer inte. Skriv kundens namn precis som det står."
    return render(
        request,
        "manage/utskick/end.html",
        {
            "active": "flamingo",
            "title": f"Avsluta utskick för {customer.name}",
            "customer": customer,
            "counts": end_counts(account),
            "error": error,
            "typed": request.POST.get("namn", "") if request.method == "POST" else "",
        },
        status=400 if error else 200,
    )


# ---------------------------------------------------------------------------
# Översikten
# ---------------------------------------------------------------------------


def tick_state(now=None):
    """Ticken för översikten: senast, sammanfattningen, om något väntar och
    om den har stannat."""
    from .sending import tick

    now = now or timezone.now()
    switch = Switchboard.get_solo()
    work = tick.work_exists(now)
    return {
        "last_at": switch.last_tick_at,
        "summary": switch.last_tick_summary or {},
        "work": work,
        "stale": tick.is_stale(switch, now),
    }


def _ready_rows(switch):
    return [
        {"field": field, "label": label, "help": help_text, "at": getattr(switch, field)}
        for field, label, help_text in READY
    ]


def _accounts():
    return (
        UtskickSettings.objects.select_related("account__customer", "enabled_by")
        .annotate(kontakter=Count("account__utskick_contacts", distinct=True))
        .order_by("account__is_demo", "-is_enabled", "account__customer__name")
    )


def _dpa_page():
    from apps.website.models import BlockPage

    return BlockPage.objects.filter(slug=DPA_SLUG, design=BlockPage.DESIGN_ADX).first()


@staff_required
def overview(request):
    now = timezone.now()
    switch = Switchboard.get_solo()
    rows = list(_accounts())
    for row in rows:
        row.dpa_text = (
            "Behövs inte"
            if row.account.is_demo
            else (
                acceptance.version.version
                if (acceptance := latest_acceptance(row.account))
                else "Inte godkänt"
            )
        )
    current = current_dpa()
    page = _dpa_page()
    context = {
        "active": "flamingo",
        "title": "Utskick",
        "switch": switch,
        "changed_by": _who(switch.changed_by),
        "ready_rows": _ready_rows(switch),
        "tick": tick_state(now),
        "doi_queue": optin.queued(now).count(),
        "import_jobs": ImportJob.objects.filter(status__in=importer.BACKGROUND).count(),
        "accounts": rows,
        "enabled_count": sum(1 for r in rows if r.is_enabled and not r.account.is_demo),
        "contact_total": sum(r.kontakter for r in rows if not r.account.is_demo),
        "keys_ok": keys.check_fingerprints(alert=False),
        "email_live": bool(getattr(settings, "UTSKICK_EMAIL_LIVE", False)),
        "can_send_mail": transport.can_send(),
        "aws_role": bool(getattr(settings, "UTSKICK_AWS_ROLE_ARN", "")),
        "dpa": current,
        "dpa_versions": DpaVersion.objects.select_related("published_by")[:5],
        "dpa_accepted": (
            current.acceptances.values("account").distinct().count() if current else 0
        ),
        "dpa_page": page,
        "next_version": timezone.localtime(now).strftime("%Y-%m"),
    }
    # S2: varje byggares del av översikten har sin egen modul och mall
    # (manage_sending, manage_inbound, manage_links; S2-HANDOFF.md). S3:
    # manage_email (S3-HANDOFF.md).
    from . import manage_email, manage_inbound, manage_links, manage_sending

    for part in (manage_sending, manage_inbound, manage_links, manage_email):
        context.update(part.panel_context(now))
    return render(request, "manage/utskick/overview.html", context)


# ---------------------------------------------------------------------------
# Nödstoppet och klarmarkeringarna
# ---------------------------------------------------------------------------


def _note(request):
    return " ".join(str(request.POST.get("note") or "").split())[:200]


@staff_required
@require_POST
def switch(request):
    """Klarmarkeringar och brytare. Allt sparas med vem, när och anteckningen."""
    action = request.POST.get("action", "")
    note = _note(request)
    field = request.POST.get("field", "")
    now = timezone.now()
    back = redirect(reverse("manage:utskick_overview") + "#nodstopp")
    with transaction.atomic():
        Switchboard.get_solo()
        row = Switchboard.objects.select_for_update().get(pk=Switchboard.SOLO_PK)
        changed = []
        text = ""
        if action in ("ready", "unready"):
            if field not in READY_FIELDS:
                messages.error(request, "Okänd klarmarkering.")
                return back
            if not note:
                messages.error(request, NOTE_TEXT)
                return back
            label = dict((f, lbl) for f, lbl, _ in READY)[field]
            if action == "ready":
                if getattr(row, field) is None:
                    setattr(row, field, now)
                    changed.append(field)
                text = f"{label} är klarmarkerat."
            else:
                setattr(row, field, None)
                changed.append(field)
                text = f"{label} är inte längre klarmarkerat."
                # Brytaren som vilar på markeringen går av med den.
                if field in ("links_ready_at", "sms_inbound_ready_at") and row.sms_enabled:
                    row.sms_enabled = False
                    changed.append("sms_enabled")
                    text += " Sms-utskick är avstängda."
                if field == "email_ready_at" and row.email_enabled:
                    row.email_enabled = False
                    changed.append("email_enabled")
                    text += " E-postutskick är avstängda."
        elif action == "sms_on":
            if not (row.links_ready_at and row.sms_inbound_ready_at):
                messages.error(request, SMS_NOT_READY_TEXT)
                return back
            if not note:
                messages.error(request, NOTE_TEXT)
                return back
            row.sms_enabled = True
            changed.append("sms_enabled")
            text = "Sms-utskick är påslagna."
        elif action == "email_on":
            live = bool(getattr(settings, "UTSKICK_EMAIL_LIVE", False))
            if not (row.email_ready_at and live):
                messages.error(request, EMAIL_NOT_READY_TEXT)
                return back
            if not note:
                messages.error(request, NOTE_TEXT)
                return back
            row.email_enabled = True
            changed.append("email_enabled")
            text = "E-postutskick är påslagna."
        elif action in ("sms_off", "email_off", "stop_all"):
            if action in ("sms_off", "stop_all"):
                row.sms_enabled = False
                changed.append("sms_enabled")
            if action in ("email_off", "stop_all"):
                row.email_enabled = False
                changed.append("email_enabled")
            if action == "stop_all":
                # Bekräftelsemejlen skickas bara med markeringen (optin.due).
                row.doi_ready_at = None
                changed.append("doi_ready_at")
            note = note or "Avstängt"
            text = {
                "sms_off": "Sms-utskick är avstängda.",
                "email_off": "E-postutskick är avstängda.",
                "stop_all": STOP_ALL_TEXT,
            }[action]
        else:
            messages.error(request, "Okänd åtgärd.")
            return back
        row.changed_by = request.user
        row.changed_at = now
        row.note = note
        row.save(update_fields=[*set(changed), "changed_by", "changed_at", "note"])
    logger.warning(
        "Utskickens brytare: %s %s av användare %s", action, field or "", request.user.pk
    )
    messages.success(request, text)
    return back


# ---------------------------------------------------------------------------
# Biträdesavtalet
# ---------------------------------------------------------------------------

#: Fälttyperna i sidbyggarens schema som är text (apps/manage/block_schema.py).
_TEXT_TYPES = ("plain", "text", "rich")
_BREAKS = re.compile(r"(?i)<\s*(?:br\s*/?|/p|/h[1-6]|/li|/div|/blockquote)\s*>")
_TAGS = re.compile(r"<[^>]+>")


def _plain(value):
    text = _BREAKS.sub("\n", str(value or ""))
    text = html.unescape(_TAGS.sub("", text))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _nested(data, key):
    for part in key.split("."):
        if not isinstance(data, dict):
            return ""
        data = data.get(part, "")
    return data


def page_text(page):
    """Sidans text som ren text, block för block i ordning: rubriker,
    ingresser, brödtext och listornas rader. Bildtexter, länkadresser och
    inställningar kommer inte med. Stycken skiljs med en tom rad."""
    from apps.manage.block_schema import BLOCK_EDIT_SCHEMA

    parts = [_plain(page.title)]
    for block in page.blocks.filter(is_visible=True).order_by("order", "pk"):
        schema = BLOCK_EDIT_SCHEMA.get(block.block_type) or {}
        data = block.data or {}
        for spec in schema.get("fields", []):
            if spec.get("type") in _TEXT_TYPES:
                parts.append(_plain(_nested(data, spec["key"])))
        for lst in schema.get("lists", []):
            for item in data.get(lst["key"]) or []:
                if isinstance(item, str):
                    parts.append(_plain(item))
                    continue
                if not isinstance(item, dict):
                    continue
                for spec in lst.get("fields", []):
                    if spec.get("type") in _TEXT_TYPES:
                        parts.append(_plain(_nested(item, spec["key"])))
    return "\n\n".join(part for part in parts if part)


class DpaPublishForm(forms.Form):
    version = forms.RegexField(
        regex=r"^[0-9A-Za-z.\-]{1,20}$",
        error_messages={
            "required": "Skriv versionen, till exempel 2026-10.",
            "invalid": "Versionen får ha siffror, bokstäver, punkt och bindestreck, högst 20.",
        },
    )
    confirm = forms.BooleanField(
        error_messages={"required": "Kryssa i att sidan är granskad och klar."}
    )

    def clean_version(self):
        version = self.cleaned_data["version"]
        if DpaVersion.objects.filter(version=version).exists():
            raise forms.ValidationError(f"Version {version} finns redan. Välj en ny.")
        return version


@staff_required
@require_POST
def dpa_publish(request):
    """Publicera en ny version av biträdesavtalet: texten på sidan
    /bitradesavtal/ som den ser ut nu sparas (DpaVersion.text och sha256)
    och blir den aktuella. Kunderna godkänner den nästa gång de tar in
    kontakter (access.can_collect); det som redan finns fungerar som vanligt.
    Ingen mejlas."""
    back = redirect(reverse("manage:utskick_overview") + "#avtal")
    form = DpaPublishForm(request.POST)
    if not form.is_valid():
        messages.error(request, _first_error(form))
        return back
    page = _dpa_page()
    if page is None or not page.is_published:
        messages.error(
            request,
            f"Sidan /{DPA_SLUG}/ finns inte eller är inte publicerad. "
            "Skapa och publicera den först.",
        )
        return back
    text = page_text(page)
    if len(text) < 200:
        messages.error(request, f"Sidan /{DPA_SLUG}/ har nästan ingen text. Inget publicerades.")
        return back
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    current = current_dpa()
    if current is not None and current.sha256 == digest:
        messages.error(
            request,
            f"Texten är densamma som i version {current.version}. Ändra sidan först.",
        )
        return back
    version = form.cleaned_data["version"]
    with transaction.atomic():
        DpaVersion.objects.filter(is_current=True).update(is_current=False)
        DpaVersion.objects.create(
            version=version,
            text=text,
            sha256=digest,
            published_by=request.user,
            is_current=True,
        )
    logger.warning("Biträdesavtal %s publicerat av användare %s", version, request.user.pk)
    messages.success(
        request,
        f"Version {version} av biträdesavtalet är publicerad. Kunderna godkänner den nästa "
        "gång de lägger till kontakter. Ingen har mejlats.",
    )
    return back
