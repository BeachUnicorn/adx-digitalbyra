"""
Sidorna och kampanjerna: vilken sida en kampanj visar, att spara ett utkast
och att publicera.

    pages_for(account)                  kontots sidor, med antal kampanjer
    campaigns_using(page)               kampanjerna som visar sidan
    ensure_own_page(campaign, user=None)
                                        kampanjens egen sida (skapas vid behov)
    use_shared_page(campaign, page, user=None)
                                        kampanjen visar en annan sida på kontot
    create_page_for_campaign(campaign, content=None, user=None)
                                        en ny sida ur förslagets innehåll
    refresh_from_proposal(campaign, content, user=None)
                                        ett nytt förslag bygger om en orörd sida
    save_draft(page, blocks, *, rev, user=None) -> nytt rev
    publish_page(page, user=None, *, rev=None) -> Published
    publish_for_campaign(campaign, user=None)
                                        innan en kampanj går live: publicera
                                        en sida som aldrig publicerats
    new_page(account, **fields)         en ny sida, högst MAX_PAGES per konto
    alert_live_change(account, campaigns, subject, lines, user=None)
                                        byråns larm om en ändring som syns
                                        direkt på en live-sida

En ändring på en sida som är live går live direkt när kontrollerna är gröna
(beslut 2026-10-03): ingen granskning, men byrån får ett larm. Det gäller
publiceringen, paletten (app_views/pages.page_settings), logotypen
(media.set_logo, unset_logo) och en live-kampanj som byter sida
(use_shared_page). Kunden mejlas aldrig.

Ett nytt förslag bygger om en sida bara när förslaget själv byggde den för
just den kampanjen (LandingPage.built_for) och ingen sparat något sedan
(built_rev är fortfarande sidans rev). En sida som kunden valt (en
befintlig sida vid en ny kampanj, eller fliken Sidan), kopierat eller
ändrat byggs aldrig om.
"""

import logging
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Count, F
from django.utils import timezone

from .. import alerts, exports
from ..models import Campaign, FlamingoAccount, LandingPage
from .blocks import (
    BlockError,
    blocks_from_content,
    build_ctx,
    copy_blocks,
    sign_blocks,
    validate_blocks,
)
from .problems import page_context, page_problems, problem_text

logger = logging.getLogger(__name__)

#: Kampanjstatusar där sidan syns för besökare eller kan bli synlig igen.
PUBLISHED_STATUSES = (Campaign.STATUS_LIVE, Campaign.STATUS_PAUSED)
#: Högst så många sidor per konto (new_page).
MAX_PAGES = 50


class PageError(ValueError):
    """Något stoppade ändringen. message är en svensk text för kunden."""

    def __init__(self, message):
        self.message = str(message)
        super().__init__(self.message)


class PageProblems(PageError):
    """Kontrollerna hittade problem på sidan (checks.Problem i problems)."""

    def __init__(self, problems):
        self.problems = list(problems)
        first = problem_text(self.problems[0]) if self.problems else ""
        count = len(self.problems)
        word = "problem" if count != 1 else "problem"
        super().__init__(f"Kontrollerna hittade {count} {word} på sidan. Det första: {first}")


class PageLimit(PageError):
    """Kontot har redan MAX_PAGES sidor."""

    def __init__(self):
        super().__init__(
            f"Kontot har redan {MAX_PAGES} sidor, och det är gränsen. Ta bort en sida som "
            "ingen kampanj använder först."
        )


class StaleRevision(PageError):
    """Utkastet har sparats från ett annat ställe sedan det lästes."""

    def __init__(self, current_rev):
        self.current_rev = current_rev
        super().__init__(
            "Sidan har ändrats på ett annat ställe sedan du öppnade den. Ladda om den och "
            "gör ändringen igen."
        )


def pages_for(account):
    """Kontots sidor med antal kampanjer (campaign_count), i namnordning."""
    return (
        LandingPage.objects.filter(account=account)
        .annotate(campaign_count=Count("campaigns"))
        .order_by("name", "pk")
    )


def campaigns_using(page):
    """Kampanjerna som visar sidan, i namnordning."""
    return page.campaigns.select_related("service", "account__customer").order_by("name", "pk")


def _unique_name(account, name):
    base = (name or "Sida").strip()[:110] or "Sida"
    taken = set(LandingPage.objects.filter(account=account).values_list("name", flat=True))
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base} ({n})"
        n += 1
    return candidate


def new_page(account, **fields):
    """En ny sida på kontot (LandingPage.objects.create), men högst MAX_PAGES
    per konto. Kontots rad låses medan sidorna räknas, så två klick samtidigt
    kan inte båda ta den sista platsen. Kastar PageLimit."""
    with transaction.atomic():
        FlamingoAccount.objects.select_for_update().only("pk").get(pk=account.pk)
        if LandingPage.objects.filter(account=account).count() >= MAX_PAGES:
            raise PageLimit()
        return LandingPage.objects.create(account=account, **fields)


def template_blocks(campaign, *, user=None, content=None):
    """Block för en kampanj: förslagets sidinnehåll (generator.build_page)
    mappat till block (blocks_from_content). Bara bekräftade uppgifter."""
    from .. import checks, generator

    if content is None:
        info = generator.info_for(campaign)
        content = generator.build_page(info, checks.context_for(campaign))
    return blocks_from_content(
        content, campaign.service.sales_mode, campaign.account, user=user, ctx=build_ctx(campaign)
    )


def create_page_for_campaign(campaign, content=None, *, user=None, name=None):
    """En ny sida för kampanjen, byggd ur förslagets innehåll, i designen
    Ren med blå palett. Kampanjen pekar på den efteråt (sparad). Sidan
    minns att förslaget byggde den för kampanjen (built_for, built_rev), så
    att ett nytt förslag får bygga om den så länge ingen ändrat den.
    Kastar PageLimit när kontot redan har MAX_PAGES sidor."""
    page = new_page(
        campaign.account,
        name=_unique_name(campaign.account, name or campaign.name),
        draft={"blocks": template_blocks(campaign, user=user, content=content)},
        created_by=user if getattr(user, "pk", None) else None,
        built_for=campaign,
        built_rev=1,
    )
    campaign.landing_page = page
    campaign.save(update_fields=["landing_page", "updated_at"])
    return page


def is_untouched(page, campaign=None):
    """Får ett nytt förslag bygga om sidan? Bara när förslaget byggde den
    (create_page_for_campaign) för campaign (eller någon kampanj, utan
    campaign), ingen sparat något sedan dess (rev är built_rev), den aldrig
    publicerats, och varje version kommer från mallen."""
    if page.built_for_id is None or page.built_rev is None or page.rev != page.built_rev:
        return False
    if campaign is not None and page.built_for_id != campaign.pk:
        return False
    if page.is_published:
        return False
    for block in page.draft_blocks:
        if any(v.get("source") != "template" for v in block.get("versions") or []):
            return False
    return True


def refresh_from_proposal(campaign, content, user=None):
    """Ett nytt förslag (generatorn) bygger om kampanjens sida, men bara när
    förslaget byggde den för just den kampanjen och den är orörd sedan dess
    (is_untouched), och ingen annan kampanj använder den. En sida som kunden
    valt eller ändrat byggs aldrig om. True om utkastet byggdes om."""
    with transaction.atomic():
        page = LandingPage.objects.select_for_update().filter(pk=campaign.landing_page_id).first()
        if page is None or not is_untouched(page, campaign):
            return False
        if page.campaigns.exclude(pk=campaign.pk).exists():
            return False
        page.draft = {"blocks": template_blocks(campaign, user=user, content=content)}
        page.rev += 1
        page.built_rev = page.rev
        page.save(update_fields=["draft", "rev", "built_rev", "updated_at"])
    campaign.landing_page = page
    return True


@transaction.atomic
def ensure_own_page(campaign, user=None):
    """Kampanjens egen sida. Har kampanjen redan en sida som ingen annan
    kampanj använder returneras den. Delar den en sida med andra kampanjer
    blir den en kopia av den (samma block, samma palett), och den
    publicerade versionen följer med för en kampanj som är live eller
    pausad, så att besökarna inte ser någon skillnad. Utan sida byggs en ur
    mallarna."""
    campaign = (
        Campaign.objects.select_for_update(of=("self",))
        .select_related("service", "account__customer", "landing_page")
        .get(pk=campaign.pk)
    )
    current = campaign.landing_page
    if current is not None and not current.campaigns.exclude(pk=campaign.pk).exists():
        return current
    if current is None:
        return create_page_for_campaign(campaign, user=user)
    fields = {
        "name": _unique_name(campaign.account, campaign.name),
        "design": current.design,
        "palette": current.palette,
        "logo_colors": dict(current.logo_colors or {}),
        "draft": {"blocks": copy_blocks(current.draft_blocks)},
        "created_by": user if getattr(user, "pk", None) else None,
    }
    if campaign.status in PUBLISHED_STATUSES and current.is_published:
        fields["published"] = {"blocks": copy_blocks(current.published_blocks)}
        fields["published_at"] = timezone.now()
        fields["published_by"] = user if getattr(user, "pk", None) else None
    page = new_page(campaign.account, **fields)
    campaign.landing_page = page
    campaign.save(update_fields=["landing_page", "updated_at"])
    return page


def use_shared_page(campaign, page, user=None):
    """Kampanjen visar sidan i stället för sin egen. Bara en sida på samma
    konto. Den förra sidan står kvar i listan över sidor. Den valda sidan
    byggs aldrig om av ett nytt förslag (built_for töms).

    En kampanj som är live eller pausad kan bara byta till en sida som är
    publicerad (besökarna ska aldrig se ett utkast), och bara när den
    publicerade versionen klarar kontrollerna (page_problems, PageProblems
    annars). Bytet syns direkt, så byrån larmas (aldrig kunden)."""
    if page.account_id != campaign.account_id:
        raise PageError("Sidan hör till ett annat konto.")
    with transaction.atomic():
        campaign = (
            Campaign.objects.select_for_update(of=("self",))
            .select_related("account__customer", "landing_page")
            .get(pk=campaign.pk)
        )
        before = campaign.landing_page
        visible = campaign.status in PUBLISHED_STATUSES
        if visible and before is not None and before.pk == page.pk:
            return campaign
        if visible:
            locked = LandingPage.objects.select_for_update().get(pk=page.pk)
            if not locked.is_published:
                raise PageError(
                    "Sidan är inte publicerad än, och kampanjen är live eller pausad. "
                    "Publicera sidan först."
                )
            context = page_context(locked, also=[campaign])
            problems = page_problems(locked, context, blocks=locked.published_blocks)
            if problems:
                raise PageProblems(problems)
        campaign.landing_page = page
        campaign.save(update_fields=["landing_page", "updated_at"])
        if before is None or before.pk != page.pk:
            # En sida som kunden valt byggs aldrig om av ett förslag, inte
            # ens den som förslaget en gång byggde (refresh_from_proposal).
            LandingPage.objects.filter(pk=page.pk).update(built_for=None, built_rev=None)
            page.built_for, page.built_rev = None, None
    if visible:
        state = "live" if campaign.status == Campaign.STATUS_LIVE else "pausad"
        who = who_text(user)
        alert_live_change(
            campaign.account,
            [campaign],
            f"Flamingo: {campaign.name} visar sidan {page.name} ({campaign.account.customer.name})",
            [
                f"Kampanjen {campaign.name} ({state}) visar nu sidan {page.name} i stället för "
                f"{before.name if before is not None else 'ingen sida'}"
                + (f". Bytet gjordes av {who}." if who else "."),
                "Den publicerade versionen av sidan klarade kontrollerna, så bytet syns direkt "
                "för besökarna" + (" när kampanjen återupptas:" if state == "pausad" else ":"),
                f"- {campaign.name}: {exports.landing_page_url(campaign)}",
            ],
        )
    return campaign


def who_text(user):
    if user is not None and getattr(user, "pk", None):
        return user.get_full_name() or user.get_username()
    return ""


def alert_live_change(account, campaigns, subject, lines, user=None):
    """Byråns larm om en ändring som syns direkt för besökarna i
    kampanjerna (live eller pausade som visar sidan): publiceringen,
    paletten, logotypen, en kampanj som byter sida. Aldrig kunden, aldrig
    för ett demokonto. Ämnesraden ska säga vad det nya läget är (sidans rev,
    paletten, logotypen), så att två olika ändringar inom en timme båda
    larmar (alerts.py släpper bara igenom samma ämnesrad en gång i timmen
    per kampanj). True om larmet skickades."""
    campaigns = [c for c in campaigns if c.status in PUBLISHED_STATUSES]
    if not campaigns or account.is_demo:
        return False
    return alerts.send_agency_alert(campaigns[0], subject, [*lines, "", "Kunden har inte mejlats."])


def live_campaigns(pages):
    """Kampanjerna som är live och visar någon av sidorna."""
    return list(
        Campaign.objects.filter(landing_page__in=pages, status=Campaign.STATUS_LIVE)
        .select_related("account__customer")
        .order_by("name", "pk")
    )


def save_draft(page, blocks, *, rev, user=None, account=None):
    """Spara utkastet om ingen annan hunnit före: blocken prövas mot schemat
    (validate_blocks, med kontots mediaarkiv), och sparningen görs bara om
    sidans rev fortfarande är rev. Varje version får serverns signatur
    (blocks.sign_version): det som sparas här är det servern står för, så
    redigeraren ska ha stämplat kundens versioner först
    (app_views/pages.stamp_authorship). Returnerar det nya rev.

    Kastar BlockError (schemat) eller StaleRevision (någon annan sparade
    emellan). Kontrollerna (page_problems) körs inte här: ett utkast får ha
    problem; publiceringen stoppar dem."""
    clean = sign_blocks(validate_blocks(blocks, account=account or page.account))
    now = timezone.now()
    updated = LandingPage.objects.filter(pk=page.pk, rev=rev).update(
        draft={"blocks": clean}, rev=F("rev") + 1, updated_at=now
    )
    if not updated:
        current = LandingPage.objects.filter(pk=page.pk).values_list("rev", flat=True).first()
        raise StaleRevision(current)
    page.draft = {"blocks": clean}
    page.rev = rev + 1
    page.updated_at = now
    return page.rev


@dataclass
class Published:
    page: LandingPage
    #: Kampanjerna som är live och visar sidan (de som fick ändringen direkt).
    live_campaigns: list = field(default_factory=list)
    #: Byrån larmades (alerts.send_agency_alert).
    alerted: bool = False


def _alert_lines(page, live, user):
    who = who_text(user)
    lines = [
        f"Sidan {page.name} för {page.account.customer.name} publicerades"
        + (f" av {who}." if who else "."),
        "Kontrollerna var gröna, så ändringen syns direkt för besökarna i de här "
        "kampanjerna, som är live:",
    ]
    for campaign in live:
        lines.append(f"- {campaign.name}: {exports.landing_page_url(campaign)}")
    return lines


def publish_page(page, user=None, *, rev=None, now=None):
    """Publicera utkastet: kontrollerna (page_problems) ska vara tomma, annars
    PageProblems och inget ändras. Med rev (redigeraren skickar det rev den
    senast sparade) publiceras bara om utkastet fortfarande är det:
    StaleRevision annars, så att en gammal flik aldrig publicerar något den
    inte har sett. Utkastet kopieras till published och published_at sätts.
    Visar en kampanj som är live sidan larmas byrån, en gång per
    publicering (aldrig kunden; inte för ett demokonto). Returnerar
    Published."""
    now = now or timezone.now()
    with transaction.atomic():
        locked = (
            LandingPage.objects.select_for_update()
            .select_related("account__customer")
            .get(pk=page.pk)
        )
        if rev is not None and locked.rev != rev:
            raise StaleRevision(locked.rev)
        try:
            validate_blocks(locked.draft_blocks, account=locked.account)
        except BlockError as exc:
            raise PageError("Sidan klarar inte schemat: " + "; ".join(exc.errors[:3])) from None
        problems = page_problems(locked)
        if problems:
            raise PageProblems(problems)
        locked.published = {"blocks": copy_blocks(locked.draft_blocks)}
        locked.published_at = now
        locked.published_by = user if getattr(user, "pk", None) else None
        locked.save(update_fields=["published", "published_at", "published_by", "updated_at"])
        live = list(campaigns_using(locked).filter(status=Campaign.STATUS_LIVE))
    for name in ("published", "published_at", "published_by", "updated_at"):
        setattr(page, name, getattr(locked, name))
    customer = locked.account.customer.name
    # Utkastets rev i ämnesraden: varje publicering larmar, också två inom
    # samma timme (alerts.py släpper bara igenom samma ämnesrad en gång i
    # timmen per kampanj).
    subject = (
        f"Flamingo: sidan {locked.name} ändrades på en live-kampanj "
        f"({customer}, version {locked.rev})"
    )
    alerted = alert_live_change(locked.account, live, subject, _alert_lines(locked, live, user))
    logger.info(
        "Flamingo: sidan %s publicerad (live i %s kampanjer, larm %s)",
        locked.pk,
        len(live),
        "ja" if alerted else "nej",
    )
    return Published(page=page, live_campaigns=live, alerted=alerted)


def publish_for_campaign(campaign, user=None):
    """Innan kampanjen går live: har dess sida aldrig publicerats publiceras
    utkastet nu, om kontrollerna går igenom. Kastar PageError (eller
    PageProblems med orsakerna) och då ska kampanjen inte gå live. En sida
    som redan är publicerad lämnas som den är. Inget larm: ingen kampanj
    som visar sidan är live än."""
    page = campaign.landing_page if campaign.landing_page_id else None
    if page is None:
        raise PageError("Kampanjen har ingen landningssida.")
    if page.is_published:
        return page
    publish_page(page, user)
    return page
