"""
Översiktens regler: kom-igång-stegen, "tre saker" och rutan Att göra.

Rena funktioner över databasen, inga AI-anrop och inga påhittade siffror:
varje sak bygger på rader som finns, och varje sak har en länk som gör den.
Ordningen är prioriteten (README, steg 12):

    1. förfrågningar som väntat mer än två timmar ("Ring Anna L.")
    2. kampanjer som byrån granskat och som väntar på kundens godkännande
    3. förfrågningar som stått utan status i mer än sju dagar
    4. kom-igång-steg som inte är gjorda (hemsidan, uppgifterna, Google,
       ADX:s kopplingsförfrågan att godkänna och betalningen hos Google)

Högst tre saker visas. Varje regel ger högst en sak (med antal när det är
fler), så tre väntande förfrågningar tränger inte undan ett godkännande.

Att göra (todo_for, menyn med sex punkter 2026-10-28) är översiktens första
ruta: förfrågningar utan svar med den äldstas ålder, svar på kundens sms och
mejl som ingen tagit hand om, det kunden själv ska göra (godkännanden,
pausade utskick, kom-igång-stegen) och sist det som väntar på ADX
(en kampanj under granskning eller som ADX publicerar, Google-kontot som
ADX kopplar, länkar till nya webbplatser). Allt ur rader som redan finns.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from .google_accounts import MANAGERS_PATH
from .models import Campaign, FlamingoAccount, Lead

MAX_THINGS = 3
#: En ny förfrågan räknas som väntande efter så här lång tid.
WAITING_AFTER = timedelta(hours=2)
#: ... och som "utan status" (siffrorna blir fel) efter så här lång tid.
STALE_AFTER = timedelta(days=7)

_MONTHS = ("jan", "feb", "mars", "april", "maj", "juni", "juli", "aug", "sep", "okt", "nov", "dec")
_NUMBERS = {1: "En", 2: "Två", 3: "Tre"}


def when_text(moment, now=None):
    """'i dag 09:12', 'i går 14:10' eller '2 okt' i svensk tid."""
    now = timezone.localtime(now or timezone.now())
    local = timezone.localtime(moment)
    if local.date() == now.date():
        return f"i dag {local:%H:%M}"
    if local.date() == (now - timedelta(days=1)).date():
        return f"i går {local:%H:%M}"
    text = f"{local.day} {_MONTHS[local.month - 1]}"
    if local.year != now.year:
        text += f" {local.year}"
    return text


def count_word(n, one, many):
    """'1 förfrågan', '3 förfrågningar'."""
    return f"{n} {one if n == 1 else many}"


# ---------------------------------------------------------------------------
# Kom-igång-stegen: Förslaget, Företaget, Google, Första kampanjen
# ---------------------------------------------------------------------------

STEP_DONE = "done"
STEP_NOW = "now"
STEP_WAIT = "wait"  # kunden har gjort sitt, ADX jobbar
#: Google-stegets text när ADX skickat kopplingsförfrågan: då är det kunden
#: som ska godkänna den, inte ADX som jobbar (google_waiting_on_customer).
GOOGLE_ACCEPT_NOTE = f"Godkänn ADX:s förfrågan i Google Ads under {MANAGERS_PATH}."
STEP_TODO = "todo"


@dataclass(frozen=True)
class Step:
    number: int
    key: str
    label: str
    url_name: str
    state: str
    note: str

    @property
    def url(self):
        return reverse(f"flamingo:{self.url_name}")

    @property
    def is_done(self):
        return self.state == STEP_DONE


@dataclass(frozen=True)
class Onboarding:
    steps: tuple

    @property
    def complete(self):
        return all(step.is_done for step in self.steps)

    @property
    def next_step(self):
        return next((step for step in self.steps if step.state == STEP_NOW), None)

    @property
    def done_count(self):
        return sum(1 for step in self.steps if step.is_done)


def onboarding_for(account):
    """Var kunden är i kom-igång-flödet. Första steget som varken är gjort
    eller ligger hos ADX är "nu"; resten efter det är "kvar"."""
    has_services = account.services.exists()
    proposal_done = account.scan_status == FlamingoAccount.SCAN_DONE or has_services
    facts = account.facts.all()
    business_done = facts.exists() and not facts.filter(confirmed=False).exists()
    campaign_done = account.campaigns.exclude(status=Campaign.STATUS_DRAFT).exists()

    raw = [
        (
            "proposal",
            "Förslaget",
            "app_proposal",
            proposal_done,
            False,
            "Vi läser din hemsida och föreslår tjänster.",
        ),
        (
            "business",
            "Företaget",
            "app_business",
            business_done,
            False,
            "Bekräfta det vi får säga om dig.",
        ),
        (
            "google",
            "Google",
            "app_google",
            # Klart när kontot är kopplat under ADX. Betalningen stoppar inte
            # att en kampanj går live (beslut 2026-10-03), men annonserna
            # visas först när den finns: den står kvar bland "tre saker"
            # (onboarding_things) tills ADX bockat av den.
            account.google_linked,
            # Hos ADX tills förfrågan är skickad; sedan är det kundens tur.
            account.google_waiting_on_adx,
            GOOGLE_ACCEPT_NOTE
            if account.google_waiting_on_customer
            else "Ditt Google Ads-konto, kopplat under ADX.",
        ),
        (
            "campaign",
            "Första kampanjen",
            "app_campaign_new",
            campaign_done,
            False,
            "Välj tjänst, område och budget.",
        ),
    ]
    steps, found_now = [], False
    for number, (key, label, url_name, done, waiting, note) in enumerate(raw, start=1):
        if done:
            state = STEP_DONE
        elif waiting:
            state = STEP_WAIT
        elif not found_now:
            state, found_now = STEP_NOW, True
        else:
            state = STEP_TODO
        steps.append(Step(number, key, label, url_name, state, note))
    return Onboarding(tuple(steps))


# ---------------------------------------------------------------------------
# Tre saker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Thing:
    key: str
    #: Den fetstilta början: "Ring Anna L."
    title: str
    #: Resten av meningen.
    text: str
    url: str
    #: Knappens text: "Öppna", "Granska" ...
    action: str


def waiting_leads(account, now):
    """Nya förfrågningar som väntat mer än två timmar (men inte en vecka:
    då är de "utan status" i stället)."""
    leads = account.leads.filter(
        status=Lead.STATUS_NEW,
        created_at__lte=now - WAITING_AFTER,
        created_at__gt=now - STALE_AFTER,
    ).order_by("created_at", "id")
    oldest = leads.select_related("service", "campaign__service").first()
    if oldest is None:
        return None
    count = leads.count()
    verb = "Ring" if oldest.phone else "Svara"
    title = f"{verb} {oldest.display_name}"
    about = f" om {oldest.service_name.lower()}" if oldest.service_name else ""
    text = f"Förfrågan{about} kom {when_text(oldest.created_at, now)}."
    if oldest.source == Lead.SOURCE_CALL_CLICK:
        # Ett klick på numret: ägaren fick samtalet, det finns ingen att svara.
        title = "Sätt status på ett klick på numret"
        text = f"Någon tryckte på numret{about} {when_text(oldest.created_at, now)}."
    if count > 1:
        text += f" {count_word(count - 1, 'förfrågan till väntar', 'förfrågningar till väntar')}."
    return Thing(
        key="waiting_leads",
        title=title,
        text=text,
        url=reverse("flamingo:app_lead", args=[oldest.pk]),
        action="Öppna",
    )


def campaigns_waiting(account, now):
    """Kampanjer som ADX granskat och som väntar på kundens godkännande.
    En godkänd kampanj står kvar som "needs_customer" tills ADX publicerat
    den, men då väntar den inte längre på kunden (approved_at är satt)."""
    campaigns = account.campaigns.filter(
        status=Campaign.STATUS_NEEDS_CUSTOMER, approved_at__isnull=True
    ).order_by("updated_at", "id")
    first = campaigns.first()
    if first is None:
        return None
    count = campaigns.count()
    text = "ADX har granskat den. Inget publiceras förrän du godkänt."
    if count > 1:
        text += f" {count_word(count - 1, 'kampanj till väntar', 'kampanjer till väntar')}."
    return Thing(
        key="campaigns_waiting",
        title=f"Godkänn {first.name}",
        text=text,
        url=reverse("flamingo:app_campaign", args=[first.pk]),
        action="Granska",
    )


def stale_leads(account, now):
    """Förfrågningar som fortfarande är "Ny" efter en vecka."""
    count = account.leads.filter(status=Lead.STATUS_NEW, created_at__lte=now - STALE_AFTER).count()
    if not count:
        return None
    return Thing(
        key="stale_leads",
        title=count_word(count, "förfrågan saknar status", "förfrågningar saknar status"),
        text=(
            "Då blir siffrorna fel. Markera den som kontaktad, vunnen, förlorad eller skräp."
            if count == 1
            else "Då blir siffrorna fel. Markera dem som kontaktade, vunna, förlorade eller skräp."
        ),
        url=reverse("flamingo:app_inbox") + "?status=new",
        action="Visa",
    )


def onboarding_things(account, now):
    """Kom-igång-steg kunden själv kan göra något åt (inte de som ligger hos
    ADX, som en Google-koppling på gång). En kopplingsförfrågan från ADX är
    kundens att godkänna i Google Ads."""
    things = []
    if account.scan_status != FlamingoAccount.SCAN_DONE and not account.services.exists():
        failed = account.scan_status == FlamingoAccount.SCAN_FAILED
        things.append(
            Thing(
                key="proposal",
                title="Hämta förslaget" if not failed else "Försök hämta hemsidan igen",
                text=(
                    "Vi läser din hemsida och föreslår tjänster och uppgifter."
                    if not failed
                    else "Det gick inte att läsa den senast. Du kan också lägga in "
                    "tjänsterna själv."
                ),
                url=reverse("flamingo:app_proposal"),
                action="Börja" if not failed else "Försök igen",
            )
        )
    unconfirmed = account.facts.filter(confirmed=False).count()
    if unconfirmed:
        things.append(
            Thing(
                key="facts",
                title=f"Bekräfta {count_word(unconfirmed, 'uppgift', 'uppgifter')}",
                text="Annonserna får bara använda det du bekräftat om företaget.",
                url=reverse("flamingo:app_business"),
                action="Bekräfta",
            )
        )
    if account.google_waiting_on_customer:
        things.append(
            Thing(
                key="google_accept",
                title="Godkänn ADX:s förfrågan i Google Ads",
                text=f"Den finns under {MANAGERS_PATH}. Sedan kan kampanjerna gå live.",
                url=reverse("flamingo:app_google"),
                action="Visa",
            )
        )
    elif account.google_status == FlamingoAccount.GOOGLE_NOT_STARTED:
        things.append(
            Thing(
                key="google",
                title="Koppla Google Ads",
                text="Kontot är ditt. Ange dess id, eller be oss skapa ett åt dig.",
                url=reverse("flamingo:app_google"),
                action="Koppla",
            )
        )
    elif (
        account.google_status == FlamingoAccount.GOOGLE_LINKED
        and account.google_billing_status != "APPROVED"
    ):
        things.append(
            Thing(
                key="google_billing",
                title="Lägg in betalning hos Google",
                text=(
                    "Kontot är kopplat. Annonserna visas först när du lagt in betalning hos Google."
                ),
                url=reverse("flamingo:app_google"),
                action="Visa",
            )
        )
    return things


# --- Utskick (apps/utskick, S2): utskick-ui-byggaren ------------------------


def utskick_things(account, now):
    """Pausade utskick och länkvärdar som väntar på ADX (README C.2, I.5,
    E.8): en sak per pausat utskick, en för värdarna. Bara när utskick är
    på för kontot. Kunden mejlas aldrig om en paus, så det här och
    rapportens ruta är signalen."""
    things = utskick_paused_things(account, now)
    host = utskick_hosts_thing(account)
    if host is not None:
        things.append(host)
    return things


def utskick_paused_things(account, now):
    """En sak per pausat utskick (högst MAX_THINGS), bara med utskick på."""
    from apps.utskick.access import is_enabled
    from apps.utskick.models import Utskick

    if not is_enabled(account):
        return []
    things = []
    paused = (
        Utskick.objects.listed()
        .filter(account=account, status__in=Utskick.PAUSED_STATES)
        .order_by("status_changed_at", "pk")[:MAX_THINGS]
    )
    for utskick in paused:
        label = utskick.get_pause_reason_display() if utskick.pause_reason else "Pausat"
        things.append(
            Thing(
                key=f"utskick_paused_{utskick.pk}",
                title=f"Utskicket {utskick.name}",
                text=f"är {label[:1].lower()}{label[1:]}.",
                url=reverse("flamingo:app_utskick", args=[utskick.pk]),
                action="Öppna",
            )
        )
    return things


def utskick_hosts_thing(account):
    """Länkvärdar som väntar på ADX, som en sak, eller None. Bara med
    utskick på."""
    from apps.utskick.access import is_enabled
    from apps.utskick.models import AllowedHost

    if not is_enabled(account):
        return None
    hosts = list(
        AllowedHost.objects.filter(account=account, status=AllowedHost.Status.PENDING)
        .order_by("requested_at", "pk")
        .values_list("host", flat=True)[:4]
    )
    if not hosts:
        return None
    text = (
        f"Länken till {hosts[0]} godkänns av ADX innan utskicket kan skickas."
        if len(hosts) == 1
        else "Länkar till nya webbplatser godkänns av ADX innan utskicken kan skickas."
    )
    return Thing(
        key="utskick_hosts",
        title="Väntar på ADX:",
        text=text,
        url=reverse("flamingo:app_utskick_list"),
        action="Visa",
    )


# --- Att göra (menyn med sex punkter, 2026-10-28) ---------------------------


@dataclass(frozen=True)
class Todo:
    """Kundens saker (Thing) och det som väntar på ADX (Thing), i ordning."""

    mine: tuple
    adx: tuple

    @property
    def headline(self):
        return things_headline(self.mine)


def age_text(moment, now):
    """Hur länge något har väntat: "under en timme", "3 timmar", "2 dagar"."""
    delta = now - moment
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return "under en timme"
    hours = minutes // 60
    if hours < 24:
        return count_word(hours, "timme", "timmar")
    return count_word(hours // 24, "dag", "dagar")


def _snippet(text, limit=60):
    """Början av ett svar, kapad vid ett ord, med tre punkter."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut + "..."


def unanswered_leads(account, now):
    """Förfrågningar som fortfarande är "Ny" (inte svar på utskick), med
    den äldstas ålder."""
    leads = account.leads.filter(status=Lead.STATUS_NEW).exclude(source=Lead.SOURCE_REPLY)
    count = leads.count()
    if not count:
        return None
    oldest = (
        leads.select_related("service", "campaign__service").order_by("created_at", "id").first()
    )
    about = f" om {oldest.service_name.lower()}" if oldest.service_name else ""
    came = when_text(oldest.created_at, now)
    waited = age_text(oldest.created_at, now)
    if count == 1:
        # En enda: vem det är och vad som ska göras ("Ring Anna L."), som
        # waiting_leads. Ett klick på numret har ingen att svara.
        if oldest.source == Lead.SOURCE_CALL_CLICK:
            title = "Sätt status på ett klick på numret"
            text = f"Någon tryckte på numret{about} {came}."
        else:
            title = f"{'Ring' if oldest.phone else 'Svara'} {oldest.display_name}"
            text = f"Förfrågan{about} kom {came} och har väntat {waited}."
        return Thing(
            key="unanswered_leads",
            title=title,
            text=text,
            url=reverse("flamingo:app_lead", args=[oldest.pk]),
            action="Öppna",
        )
    text = f"Den äldsta kom {came} från {oldest.display_name}{about} och har väntat {waited}."
    from apps.utskick.access import is_enabled

    query = "?typ=forfragningar&status=new" if is_enabled(account) else "?status=new"
    return Thing(
        key="unanswered_leads",
        title=f"{count} förfrågningar utan svar",
        text=text,
        url=reverse("flamingo:app_inbox") + query,
        action="Visa",
    )


def unanswered_replies(account, now):
    """Svar på kundens sms och mejl som är "Ny" (inte STOPP), bara med
    utskick på: "2 svar på dina sms" med det äldsta svaret."""
    from apps.utskick.access import is_enabled
    from apps.utskick.models import CHANNEL_EMAIL, Thread

    if not is_enabled(account):
        return None
    replies = (
        account.leads.filter(status=Lead.STATUS_NEW, source=Lead.SOURCE_REPLY)
        .exclude(reply_thread__kind=Thread.Kind.STOP)
        .select_related("reply_thread__utskick")
        .order_by("activity_at", "id")
    )
    rows = list(replies[:50])
    if not rows:
        return None
    count = replies.count() if len(rows) == 50 else len(rows)
    channels = {
        getattr(getattr(lead, "reply_thread", None), "channel", "") == CHANNEL_EMAIL
        for lead in rows
    }
    if channels == {True}:
        what, kind = ("ditt mejl" if count == 1 else "dina mejl"), "e-postsvar"
    elif channels == {False}:
        what, kind = ("ditt sms" if count == 1 else "dina sms"), "sms-svar"
    else:
        what, kind = "dina sms och mejl", ""
    oldest = rows[0]
    thread = getattr(oldest, "reply_thread", None)
    text = f"Från {oldest.display_name}"
    if oldest.message:
        text = f'"{_snippet(oldest.message)}" från {oldest.display_name}'
    if thread is not None and thread.utskick is not None:
        text += f", på utskicket {thread.utskick.name}"
    text += "."
    if count == 1:
        url, action = reverse("flamingo:app_lead", args=[oldest.pk]), "Svara"
    else:
        query = f"?typ={kind}&status=new" if kind else "?status=new"
        url, action = reverse("flamingo:app_inbox") + query, "Visa"
    return Thing(
        key="unanswered_replies",
        title=f"{count} svar på {what}",
        text=text,
        url=url,
        action=action,
    )


WAITING_TEXT = "Du behöver inte göra något."


def adx_waiting(account, now):
    """Det som väntar på ADX: kampanjer under granskning eller som ADX
    publicerar, Google-kontot som ADX kopplar, och (med utskick) länkar
    till nya webbplatser som ADX godkänner."""
    out = []
    review = list(
        account.campaigns.filter(status=Campaign.STATUS_IN_REVIEW).order_by("updated_at", "id")[:2]
    )
    if review:
        count = account.campaigns.filter(status=Campaign.STATUS_IN_REVIEW).count()
        out.append(
            Thing(
                key="adx_review",
                title=(
                    f"Kampanjen {review[0].name} granskas av ADX"
                    if count == 1
                    else f"{count} kampanjer granskas av ADX"
                ),
                text=WAITING_TEXT,
                url=(
                    reverse("flamingo:app_campaign", args=[review[0].pk])
                    if count == 1
                    else reverse("flamingo:app_campaigns")
                ),
                action="Visa",
            )
        )
    publish = account.campaigns.filter(
        status=Campaign.STATUS_NEEDS_CUSTOMER, approved_at__isnull=False
    ).order_by("approved_at", "id")
    first = publish.first()
    if first is not None:
        count = publish.count()
        out.append(
            Thing(
                key="adx_publish",
                title=(
                    f"ADX publicerar kampanjen {first.name}"
                    if count == 1
                    else f"ADX publicerar {count} kampanjer"
                ),
                text=WAITING_TEXT,
                url=(
                    reverse("flamingo:app_campaign", args=[first.pk])
                    if count == 1
                    else reverse("flamingo:app_campaigns")
                ),
                action="Visa",
            )
        )
    if account.google_waiting_on_adx:
        out.append(
            Thing(
                key="adx_google",
                title="ADX kopplar ditt Google Ads-konto",
                text=WAITING_TEXT,
                url=reverse("flamingo:app_google"),
                action="Visa",
            )
        )
    host = utskick_hosts_thing(account)
    if host is not None:
        out.append(
            Thing(
                key="adx_hosts",
                title="ADX godkänner nya länkar",
                text=host.text,
                url=host.url,
                action=host.action,
            )
        )
    return out


def todo_for(account, now=None):
    """Översiktens ruta Att göra (Todo). Kundens saker först: förfrågningar
    och svar utan svar, sedan godkännanden, pausade utskick och
    kom-igång-stegen (som i three_things, utan de regler rutans två första
    rader redan täcker)."""
    now = now or timezone.now()
    mine = [
        thing
        for thing in (unanswered_leads(account, now), unanswered_replies(account, now))
        if thing is not None
    ]
    mine.extend(utskick_paused_things(account, now))
    thing = campaigns_waiting(account, now)
    if thing is not None:
        mine.append(thing)
    mine.extend(onboarding_things(account, now))
    return Todo(mine=tuple(mine), adx=tuple(adx_waiting(account, now)))


def three_things(account, now=None):
    """Högst tre saker att göra nu, viktigast först. Pausade utskick och
    länkar som väntar på ADX kommer före resten (README C.2)."""
    now = now or timezone.now()
    things = list(utskick_things(account, now))
    for rule in (waiting_leads, campaigns_waiting, stale_leads):
        thing = rule(account, now)
        if thing is not None:
            things.append(thing)
    things.extend(onboarding_things(account, now))
    return things[:MAX_THINGS]


def things_headline(things):
    """'Tre saker att göra nu.' eller 'Inget väntar på dig just nu.'"""
    if not things:
        return "Inget väntar på dig just nu."
    word = _NUMBERS.get(len(things), str(len(things)))
    return f"{word} {'sak' if len(things) == 1 else 'saker'} att göra nu."
