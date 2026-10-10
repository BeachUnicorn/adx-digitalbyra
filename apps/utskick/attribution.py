"""
Spåret från ett utskick till en förfrågan (README D10, D11, E.3, E.4):
klicket på k.adx.se, besöket och tiden på landningssidan, och förfrågan.
Inga kakor och ingen lagring i webbläsaren: allt bärs av ut-token i
adressen (tokens.ut_token) och loggas här, på servern, nycklat på klicket.

    classify(request, recipient, link, now) -> "bot" | "human" | "scanner"
    record_click(code, kind, request, ip_hash, now) -> Click
    resolve(ut, campaign) -> Click | None       samma konto som sidan, annars None
    record_lp_visit(click, campaign, now)       besöket (lp_visits, händelsen lp_visit)
    record_beacon(click, seconds, now) -> bool  tiden på sidan (/lp/<slug>/besok/)
    record_named_click, record_site_visit, record_site_goal
                                                S4: namngivna länkar och skriptet på egen sajt
    usable_for_lead(click, now) -> bool         högst LEADS_PER_CLICK_HOUR i timmen
    attach(lead, click, now)                    förfrågan får utskicket och spåret
    mark_called(click)                          klick på numret med token

Regler:

- Bottar och förhandsvisningar (iMessage, WhatsApp, Slack, e-postskannrar
  och liknande) räknas på mottagaren och länken men sparas aldrig som klick.
  Tre olika länkar till samma mottagare inom två sekunder är en skanner:
  raden sparas (i 14 dagar) men räknas inte som klick förrän sidan skickar
  ett besöksanrop från en riktig webbläsare (då blir den mänsklig).
- Högst CLICK_ROWS_PER_HOUR rader per mottagare och länk och timme; fler
  träffar räknas i repeat_count på den senaste raden.
- En token från konto A på konto B:s sida ger ingenting: inget besök, ingen
  koppling och inga högre gränser (resolve kräver samma konto).
- Förfrågan får utskicket, mottagaren och en ögonblicksbild av spåret
  (Lead.attribution), men kopplas till mottagarens kontakt bara när numret
  eller e-posten i formuläret är kontaktens: en vidarebefordrad länk slår
  aldrig ihop två personer. Och bara när kontot får samla kontakter
  (access.can_collect). Ett klick äldre än LATE_AFTER ger "late".
- Sådana förfrågningar går aldrig till Google (Lead.can_send_to_google, D11).
"""

import logging
from datetime import timedelta

from django.db.models import F, Q, Value
from django.db.models.functions import Coalesce, Greatest, Least
from django.utils import timezone

from . import limits, tokens
from .models import CHANNEL_EMAIL, Click, Event, Recipient

logger = logging.getLogger(__name__)

BOT = "bot"
HUMAN = Click.Kind.HUMAN
SCANNER = Click.Kind.SCANNER

#: Rader per mottagare, länk och timme; fler träffar blir repeat_count (E.3).
CLICK_ROWS_PER_HOUR = 20
#: Tre olika länkar till samma mottagare inom så här kort tid: en skanner.
SCANNER_WINDOW = timedelta(seconds=2)
SCANNER_LINKS = 3
#: E-post (S3): ett klick så här snart efter leveransen är en skanner.
EMAIL_SCANNER_AFTER_DELIVERY = timedelta(seconds=15)
#: Förfrågningar per klick och timme som får utskickets spår och högre gränser.
LEADS_PER_CLICK_HOUR = 3
#: Ett klick äldre än så räknas som sent i rapporten (E.4).
LATE_AFTER = timedelta(days=30)
#: Högst en händelse lp_visit per klick på så här lång tid (E.4).
VISIT_EVENT_EVERY = timedelta(minutes=30)
#: Högst en skrivning från besöksanropen per klick på så här lång tid.
BEACON_EVERY = timedelta(seconds=10)
SMALL_MAX = 32767

#: Förhandsvisningar och skannrar utöver analytics.utils.is_bot (E.3).
#: Gemener; jämförs mot User-Agent i gemener.
EXTRA_BOTS = (
    "whatsapp",
    "telegrambot",
    "slackbot",
    "slack-imgproxy",
    "discordbot",
    "skypeuripreview",
    "linkedinbot",
    "twitterbot",
    "google-pagerenderer",
    "bingpreview",
    "microsoft office",
    "ms-office",
    "msoffice",
    "linkpreview",
    "link preview",
    "barracuda",
    "mimecast",
    "proofpoint",
    "python-requests",
    "curl",
    "go-http-client",
)


def _user_agent(request):
    return str(request.META.get("HTTP_USER_AGENT", "") or "")[:500]


def is_preview(user_agent):
    """En bot eller en förhandsvisning (tom User-Agent räknas: E.3)."""
    from apps.analytics.utils import is_bot

    if is_bot(user_agent):
        return True
    lowered = user_agent.lower()
    return any(sig in lowered for sig in EXTRA_BOTS)


def classify(request, recipient, link, now=None):
    """ "bot", "human" eller "scanner" för en träff på en klicklänk (E.3)."""
    if is_preview(_user_agent(request)):
        return BOT
    if recipient is None:
        return HUMAN
    now = now or timezone.now()
    if (
        recipient.channel == CHANNEL_EMAIL
        and recipient.delivered_at
        and now - recipient.delivered_at < EMAIL_SCANNER_AFTER_DELIVERY
    ):
        return SCANNER
    others = (
        Click.objects.filter(recipient=recipient, at__gte=now - SCANNER_WINDOW)
        .exclude(link=link)
        .values("link_id")
        .distinct()
        .count()
    )
    return SCANNER if others >= SCANNER_LINKS - 1 else HUMAN


def count_bot(code):
    """En bot eller förhandsvisning: räknas på mottagaren och länken (F()),
    sparas inte."""
    from .models import TrackedLink

    if code.recipient_id:
        Recipient.objects.filter(pk=code.recipient_id).update(
            bot_hits=Least(F("bot_hits") + 1, Value(SMALL_MAX))
        )
    if code.link_id:
        TrackedLink.objects.filter(pk=code.link_id).update(bot_hits=F("bot_hits") + 1)


def record_click(code, kind, request, ip_hash="", now=None):
    """Spara ett mänskligt klick eller en skanner för klickkoden code
    (LinkCode med link och recipient). Över CLICK_ROWS_PER_HOUR räknas
    träffen på den senaste raden. Ett mänskligt klick räknas också på
    mottagaren (click_count, first_clicked_at). Returnerar raden som ut
    pekar på, eller None."""
    from apps.analytics.utils import parse_user_agent

    now = now or timezone.now()
    link = code.link
    recipient = code.recipient
    subject = f"r{recipient.pk}" if recipient is not None else f"c{code.pk}"
    row = None
    if limits.hit("click", f"{subject}:{link.pk}", limits.hour_window(now), CLICK_ROWS_PER_HOUR):
        row = (
            Click.objects.filter(account_id=code.account_id, recipient=recipient, link=link)
            .order_by("-at", "-pk")
            .first()
        )
        if row is not None:
            Click.objects.filter(pk=row.pk).update(
                repeat_count=Least(F("repeat_count") + 1, Value(SMALL_MAX))
            )
    if row is None:
        agent = parse_user_agent(_user_agent(request))
        row = Click.objects.create(
            account_id=code.account_id,
            utskick_id=link.utskick_id,
            recipient=recipient,
            link=link,
            contact_id=getattr(recipient, "contact_id", None),
            channel=Click.Channel.EMAIL
            if getattr(recipient, "channel", "") == CHANNEL_EMAIL
            else Click.Channel.SMS,
            kind=kind,
            at=now,
            device=str(agent.get("device_type") or "")[:8],
            os=str(agent.get("os") or "")[:20],
            browser=str(agent.get("browser") or "")[:20],
            ip_hash=(ip_hash or "")[:64],
        )
    if kind == HUMAN and recipient is not None:
        _count_human(recipient.pk, now)
    return row


def _count_human(recipient_id, now):
    Recipient.objects.filter(pk=recipient_id).update(
        click_count=Least(F("click_count") + 1, Value(SMALL_MAX)),
        first_clicked_at=Coalesce(F("first_clicked_at"), Value(now)),
    )


# ---------------------------------------------------------------------------
# Landningssidan
# ---------------------------------------------------------------------------


def resolve(ut, campaign):
    """Klicket som ut pekar på, bara när signaturen stämmer och klicket hör
    till samma konto som kampanjen (E.4). Annars None: ingen loggning,
    ingen koppling och inga högre gränser."""
    if not ut or campaign is None:
        return None
    click_id = tokens.read_ut(ut)
    if not click_id:
        return None
    return (
        Click.objects.select_related("utskick", "link", "recipient")
        .filter(pk=click_id, account_id=campaign.account_id)
        .first()
    )


def record_lp_visit(click, campaign, now=None):
    """Ett besök på landningssidan från klicket: lp_visits och första
    besöket, och händelsen lp_visit på kontakten högst en gång per
    VISIT_EVENT_EVERY. Byrån, förhandsvisningen och demot loggas inte (vyn
    anropar inte då)."""
    now = now or timezone.now()
    Click.objects.filter(pk=click.pk).update(
        lp_visits=Least(F("lp_visits") + 1, Value(SMALL_MAX)),
        first_visit_at=Coalesce(F("first_visit_at"), Value(now)),
    )
    if not click.contact_id:
        return
    recent = Event.objects.filter(
        contact_id=click.contact_id,
        kind=Event.LP_VISIT,
        at__gte=now - VISIT_EVENT_EVERY,
        data__klick=click.pk,
    ).exists()
    if recent:
        return
    Event.objects.create(
        account_id=click.account_id,
        contact_id=click.contact_id,
        kind=Event.LP_VISIT,
        at=now,
        utskick_id=click.utskick_id,
        recipient_id=click.recipient_id,
        data={"klick": click.pk, "sida": str(campaign.page_slug or "")[:80]},
    )


def record_beacon(click, seconds, now=None):
    """Tiden på sidan från besöksanropet (E.4): engaged_seconds blir det
    största som kommit, högst Click.MAX_ENGAGED_SECONDS, och högst en
    skrivning per BEACON_EVERY. En skanner som skickar ett anrop från en
    riktig webbläsare blir ett mänskligt klick (och räknas på mottagaren).
    True när något skrevs."""
    now = now or timezone.now()
    try:
        value = int(float(seconds))
    except (TypeError, ValueError, OverflowError):
        # OverflowError: "inf" i formuläret, eller 1e999 och Infinity i
        # skriptets JSON (json.loads ger float("inf")).
        return False
    value = max(0, min(value, Click.MAX_ENGAGED_SECONDS))
    updated = Click.objects.filter(
        Q(beacon_at__isnull=True) | Q(beacon_at__lte=now - BEACON_EVERY), pk=click.pk
    ).update(engaged_seconds=Greatest(F("engaged_seconds"), Value(value)), beacon_at=now)
    if not updated:
        return False
    if click.kind == SCANNER:
        upgraded = Click.objects.filter(pk=click.pk, kind=SCANNER).update(kind=HUMAN)
        if upgraded and click.recipient_id:
            _count_human(click.recipient_id, click.at or now)
    return True


# --- S4 (länk-byggaren): de namngivna länkarna och skriptet på egen sajt ---
#
#   record_named_click(link, kind, request, ip_hash, now) -> Click
#       ett klick på klick.adx.se/<public_slug>/<slug>: kanalen named, ingen
#       mottagare och ingen kontakt (affischer och QR-koder är anonyma)
#   record_site_visit(click, path, now, host="")
#       besöket på kundens egen sajt via skriptet (E.6): lp_visits och första
#       besöket som för en landningssida, och händelsen site_visit på kontakten
#       högst en gång per klick och VISIT_EVENT_EVERY
#   record_site_goal(click, path, goal, now, host="")
#       adxFlamingo.track(namn) på samma sida: händelsen site_visit med "mal",
#       en gång per klick och namn (bara med en kontakt)
#
# Tiden på sidan och skannern som blir en människa sköts av record_beacon,
# precis som för landningssidan.


def record_named_click(link, kind, request, ip_hash="", now=None):
    """Spara ett klick på en namngiven länk (S4). Högst CLICK_ROWS_PER_HOUR
    rader per besökare (ip_hash) och länk och timme; fler träffar räknas i
    repeat_count på besökarens senaste rad. Returnerar raden som ut pekar på."""
    from apps.analytics.utils import parse_user_agent

    now = now or timezone.now()
    subject = f"n{ip_hash or '-'}"[:60]
    row = None
    if limits.hit("click", f"{subject}:{link.pk}", limits.hour_window(now), CLICK_ROWS_PER_HOUR):
        row = (
            Click.objects.filter(
                account_id=link.account_id,
                link=link,
                channel=Click.Channel.NAMED,
                ip_hash=(ip_hash or "")[:64],
            )
            .order_by("-at", "-pk")
            .first()
        )
        if row is not None:
            Click.objects.filter(pk=row.pk).update(
                repeat_count=Least(F("repeat_count") + 1, Value(SMALL_MAX))
            )
    if row is None:
        agent = parse_user_agent(_user_agent(request))
        row = Click.objects.create(
            account_id=link.account_id,
            utskick_id=None,
            recipient=None,
            link=link,
            contact_id=None,
            channel=Click.Channel.NAMED,
            kind=kind,
            at=now,
            device=str(agent.get("device_type") or "")[:8],
            os=str(agent.get("os") or "")[:20],
            browser=str(agent.get("browser") or "")[:20],
            ip_hash=(ip_hash or "")[:64],
        )
    return row


def _site_event_data(click, path, host):
    return {
        "klick": click.pk,
        "sida": str(path or "/")[:80],
        "varde": str(host or "")[:253],
    }


def record_site_visit(click, path, now=None, host=""):
    """Besöket på kundens egen webbplats från klicket (skriptet, E.6):
    lp_visits och first_visit_at som på en landningssida, och händelsen
    site_visit på kontakten högst en gång per klick och VISIT_EVENT_EVERY.
    Ett klick utan kontakt (en namngiven länk) räknas bara på klicket."""
    now = now or timezone.now()
    Click.objects.filter(pk=click.pk).update(
        lp_visits=Least(F("lp_visits") + 1, Value(SMALL_MAX)),
        first_visit_at=Coalesce(F("first_visit_at"), Value(now)),
    )
    if not click.contact_id:
        return
    recent = Event.objects.filter(
        contact_id=click.contact_id,
        kind=Event.SITE_VISIT,
        at__gte=now - VISIT_EVENT_EVERY,
        data__klick=click.pk,
    ).exists()
    if recent:
        return
    Event.objects.create(
        account_id=click.account_id,
        contact_id=click.contact_id,
        kind=Event.SITE_VISIT,
        at=now,
        utskick_id=click.utskick_id,
        recipient_id=click.recipient_id,
        data=_site_event_data(click, path, host),
    )


def record_site_goal(click, path, goal, now=None, host=""):
    """adxFlamingo.track(namn) (E.6): händelsen site_visit med "mal" på
    kontakten, en gång per klick och namn. True när den skrevs."""
    if not goal or not click.contact_id:
        return False
    now = now or timezone.now()
    if Event.objects.filter(
        contact_id=click.contact_id, kind=Event.SITE_VISIT, data__klick=click.pk, data__mal=goal
    ).exists():
        return False
    Event.objects.create(
        account_id=click.account_id,
        contact_id=click.contact_id,
        kind=Event.SITE_VISIT,
        at=now,
        utskick_id=click.utskick_id,
        recipient_id=click.recipient_id,
        data={**_site_event_data(click, path, host), "mal": str(goal)[:40]},
    )
    return True


# --- slut S4 ---


# ---------------------------------------------------------------------------
# Förfrågan
# ---------------------------------------------------------------------------


def usable_for_lead(click, now=None):
    """Får klicket ge en förfrågan spåret och de högre gränserna? Högst
    LEADS_PER_CLICK_HOUR förfrågningar per klick och timme (en token som
    spelas upp om och om igen ska inte kunna fylla inkorgen)."""
    from apps.flamingo.models import Lead

    if click is None:
        return False
    now = now or timezone.now()
    used = Lead.objects.filter(
        account_id=click.account_id,
        attribution__click=click.pk,
        created_at__gte=now - timedelta(hours=1),
    ).count()
    return used < LEADS_PER_CLICK_HOUR


def _matches(contact, lead):
    """Är förfrågans nummer eller e-post kontaktens? Jämförs normaliserat."""
    from . import normalize

    if contact is None:
        return False
    phone = normalize.phone(lead.phone).e164 if lead.phone else ""
    if phone and contact.phone and phone == contact.phone:
        return True
    email = str(lead.email or "").strip().lower()
    return bool(email and contact.email and email == contact.email.lower())


def snapshot(click, lead, now=None, contact_matched=False):
    """Lead.attribution (B.7): det rapporten och inkorgen behöver, kvar
    också när klicket och mottagaren tagits bort av retentionen."""
    now = now or timezone.now()
    utskick = click.utskick
    link = click.link
    clicked = click.at or now
    return {
        "click": click.pk,
        "utskick": click.utskick_id,
        "name": utskick.name if utskick is not None else "",
        "channel": click.channel,
        "link": click.link_id,
        "label": (link.label or link.key) if link is not None else "",
        "clicked_at": clicked.isoformat(),
        "contact_matched": bool(contact_matched),
        "late": now - clicked > LATE_AFTER,
    }


def attach(lead, click, now=None):
    """Förfrågan kom via klicket: Lead.utskick, Lead.utskick_recipient och
    Lead.attribution, och Lead.contact när formulärets nummer eller e-post
    är mottagarens kontakt (och kontot får samla kontakter). Anropas av
    flamingo.leads under spärrens lås, innan konverteringen köas (en sådan
    förfrågan går aldrig till Google)."""
    from apps.flamingo.models import Lead

    from .access import can_collect

    if click is None or lead is None or click.account_id != lead.account_id:
        return lead
    now = now or timezone.now()
    recipient = click.recipient
    contact = getattr(recipient, "contact", None) if recipient is not None else None
    matched = bool(
        contact is not None
        and contact.account_id == lead.account_id
        and _matches(contact, lead)
        and can_collect(lead.account)
    )
    values = {
        "utskick_id": click.utskick_id,
        "utskick_recipient_id": click.recipient_id,
        "attribution": snapshot(click, lead, now, contact_matched=matched),
    }
    if matched and lead.contact_id is None:
        values["contact_id"] = contact.pk
    Lead.objects.filter(pk=lead.pk).update(**values)
    for name, value in values.items():
        setattr(lead, name, value)
    logger.info("Utskick: förfrågan %s via klick %s", lead.pk, click.pk)
    return lead


def mark_called(click):
    """Ett klick på telefonnumret med token (E.4)."""
    if click is not None:
        Click.objects.filter(pk=click.pk).update(called=True)
