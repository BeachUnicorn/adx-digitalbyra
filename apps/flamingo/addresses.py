"""
Landningssidornas egna adresser (README, Steg 2): /lp/<konto>/<sida>/.

    ensure_account_slug(account) -> str     kontots del (FlamingoAccount.lp_slug), satt
                                            första gången den behövs
    suggest_account_slug(account) -> str    utskickens public_slug när kontot har en och
                                            den är ledig, annars ett förslag ur kundens namn
    account_slug_taken(slug, account_id) -> bool
    page_slug_for(account_id, name, exclude_pk=None) -> str
                                            sidans del ur namnet, ledig hos kontot
    change_page_slug(page, raw, now=None) -> str
                                            "Ändra adressen" före första publiceringen;
                                            den gamla blir en LandingPageOldSlug
    page_path(page) -> "/lp/<konto>/<sida>/"
    page_url(page) -> "https://adx.se/lp/<konto>/<sida>/"
    page_text(page) -> "adx.se/lp/<konto>/<sida>"   som den står i listan och i Kopiera
    page_for_address(account_slug, page_slug) -> (sida, gammal adress?) eller (None, False)
    assign_missing(apps=None)               migreringen: konton och sidor utan adress

Kontots del är en egen kolumn och inte utskickens UtskickSettings.public_slug:
den raden finns bara för konton där byrån slagit på utskick. Ordet blir ändå
detsamma när det går. Ett konto som har en public_slug får den (om ingen annan
har den som lp_slug), och reglerna är utskickens (utskick.access): inte ett
reserverat ord, inte början på en sökväg som länkvärdarna aldrig släpper
fram (mcp, token och så vidare), inte ett annat kontos public_slug, lp_slug
eller tidigare adress (OldPublicSlug). Åt andra hållet räknar
utskick.access ett annat kontos lp_slug som upptaget. lp_slug ändras inte i
verktyget: den står i varje sidas adress.

Sidans del kommer ur namnet ("Rörjour Nacka" blir rorjour-nacka), högst
PAGE_SLUG_MAX tecken, unik hos kontot, -2, -3 vid krock. Aldrig tack, ring
eller besok: /lp/<x>/tack/ är kampanjens tacksida (public_urls.py). En
tidigare adress tillhör sin sida (LandingPageOldSlug): en annan sida får den
inte, men sidan själv får ta tillbaka den. Adressen ändras bara tills sidan
publicerats första gången.

Ett annat kontos sida hittas aldrig: page_for_address slår upp sidan genom
kontots del och sidans del i samma fråga.
"""

import re

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

#: Kampanjadressernas underled (/lp/<kampanj>/tack/ och så vidare).
RESERVED_PAGE_SLUGS = frozenset({"tack", "ring", "besok"})
PAGE_SLUG_MAX = 60
ACCOUNT_SLUG_MAX = 40
PAGE_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?$")

SLUG_FORMAT_TEXT = "Adressen får bara ha små bokstäver, siffror och bindestreck."
SLUG_RESERVED_TEXT = "Adressen är reserverad. Välj en annan."
SLUG_TAKEN_TEXT = "Adressen används redan av en annan sida."
SLUG_LOCKED_TEXT = "Sidan är publicerad, så adressen ändras inte längre."
SLUG_EMPTY_TEXT = "Skriv en adress."


class SlugRefused(ValueError):
    """Adressen går inte att använda; str(exc) är texten för kunden."""


def _models(apps=None):
    """(FlamingoAccount, LandingPage, LandingPageOldSlug, UtskickSettings,
    OldPublicSlug): de riktiga modellerna, eller migreringens."""
    if apps is not None:
        return (
            apps.get_model("flamingo", "FlamingoAccount"),
            apps.get_model("flamingo", "LandingPage"),
            apps.get_model("flamingo", "LandingPageOldSlug"),
            apps.get_model("utskick", "UtskickSettings"),
            apps.get_model("utskick", "OldPublicSlug"),
        )
    from apps.utskick.models import OldPublicSlug, UtskickSettings

    from .models import FlamingoAccount, LandingPage, LandingPageOldSlug

    return FlamingoAccount, LandingPage, LandingPageOldSlug, UtskickSettings, OldPublicSlug


# ---------------------------------------------------------------------------
# Kontots del
# ---------------------------------------------------------------------------


def account_slug_taken(slug, account_id, apps=None):
    """Kan kontot inte få slug som sin del? Reserverad, eller ett annat
    kontos lp_slug, public_slug eller tidigare adress."""
    from apps.utskick.access import is_reserved_slug

    Account, _page, _old, Settings, OldPublic = _models(apps)
    if not slug or is_reserved_slug(slug):
        return True
    if Account.objects.filter(lp_slug=slug).exclude(pk=account_id).exists():
        return True
    if Settings.objects.filter(public_slug=slug).exclude(account_id=account_id).exists():
        return True
    return OldPublic.objects.filter(slug=slug).exclude(account_id=account_id).exists()


def suggest_account_slug(account, apps=None):
    """Kontots public_slug när den finns och är ledig, annars ett ledigt
    förslag ur kundens namn (som utskick.access.suggest_public_slug)."""
    from apps.utskick.models import RESERVED_PUBLIC_SLUG_PREFIXES

    from .models import company_slug

    _account, _page, _old, Settings, _old_public = _models(apps)
    own = (
        Settings.objects.filter(account_id=account.pk).values_list("public_slug", flat=True).first()
    )
    if own and not account_slug_taken(own, account.pk, apps):
        return own
    name = account.customer.name if account.customer_id else ""
    base = company_slug(name)[:ACCOUNT_SLUG_MAX].strip("-") or "foretaget"
    if base.startswith(RESERVED_PUBLIC_SLUG_PREFIXES):
        base = f"kund-{base}"[:ACCOUNT_SLUG_MAX].rstrip("-")
    slug, n = base, 2
    while account_slug_taken(slug, account.pk, apps):
        suffix = f"-{n}"
        slug = base[: ACCOUNT_SLUG_MAX - len(suffix)].rstrip("-") + suffix
        n += 1
    return slug


def ensure_account_slug(account, apps=None):
    """Kontots lp_slug; sätts första gången (en villkorlig UPDATE, så att två
    processer inte ger kontot två olika). Sätter också account.lp_slug."""
    if account.lp_slug:
        return account.lp_slug
    Account = _models(apps)[0]
    current = Account.objects.filter(pk=account.pk).values_list("lp_slug", flat=True).first()
    if current:
        account.lp_slug = current
        return current
    for _attempt in range(5):
        slug = suggest_account_slug(account, apps)
        try:
            with transaction.atomic():
                updated = Account.objects.filter(pk=account.pk, lp_slug__isnull=True).update(
                    lp_slug=slug
                )
        except IntegrityError:
            continue
        if not updated:
            slug = Account.objects.filter(pk=account.pk).values_list("lp_slug", flat=True).first()
        account.lp_slug = slug
        return slug
    raise RuntimeError(f"Kontot {account.pk} fick ingen adress för landningssidorna")


# ---------------------------------------------------------------------------
# Sidans del
# ---------------------------------------------------------------------------


def _taken_page_slugs(account_id, exclude_pk=None, apps=None):
    _account, Page, OldSlug, _settings, _old_public = _models(apps)
    pages = Page.objects.filter(account_id=account_id).exclude(slug="")
    old = OldSlug.objects.filter(account_id=account_id)
    if exclude_pk:
        pages = pages.exclude(pk=exclude_pk)
        old = old.exclude(page_id=exclude_pk)
    return set(pages.values_list("slug", flat=True)) | set(old.values_list("slug", flat=True))


def page_slug_for(account_id, name, exclude_pk=None, apps=None):
    """Sidans del ur namnet: ledig hos kontot (också bland andra sidors
    tidigare adresser), aldrig RESERVED_PAGE_SLUGS."""
    base = slugify(name or "")[:PAGE_SLUG_MAX].strip("-") or "sida"
    taken = _taken_page_slugs(account_id, exclude_pk, apps) | RESERVED_PAGE_SLUGS
    slug, n = base, 2
    while slug in taken:
        suffix = f"-{n}"
        slug = base[: PAGE_SLUG_MAX - len(suffix)].rstrip("-") + suffix
        n += 1
    return slug


def clean_page_slug(page, raw):
    """Den nya adressen för sidan, i gemener, eller SlugRefused med texten
    för kunden."""
    text = str(raw or "").strip().strip("/").lower()
    if not text:
        raise SlugRefused(SLUG_EMPTY_TEXT)
    slug = slugify(text)
    if slug != text or not PAGE_SLUG_RE.match(slug):
        raise SlugRefused(SLUG_FORMAT_TEXT)
    if slug in RESERVED_PAGE_SLUGS:
        raise SlugRefused(SLUG_RESERVED_TEXT)
    if slug in _taken_page_slugs(page.account_id, exclude_pk=page.pk):
        raise SlugRefused(SLUG_TAKEN_TEXT)
    return slug


def change_page_slug(page, raw, now=None):
    """Byt sidans adress (bara tills sidan publicerats första gången). Den
    gamla sparas som LandingPageOldSlug och skickar vidare; tar sidan
    tillbaka en egen tidigare adress tas den raden bort. Returnerar den nya
    adressen. Kastar SlugRefused."""
    from .models import LandingPage, LandingPageOldSlug

    now = now or timezone.now()
    with transaction.atomic():
        locked = LandingPage.objects.select_for_update().get(pk=page.pk)
        if locked.published_at is not None:
            raise SlugRefused(SLUG_LOCKED_TEXT)
        slug = clean_page_slug(locked, raw)
        if slug == locked.slug:
            return slug
        LandingPageOldSlug.objects.filter(account_id=locked.account_id, slug=slug).delete()
        if locked.slug:
            LandingPageOldSlug.objects.create(
                account_id=locked.account_id, page=locked, slug=locked.slug, retired_at=now
            )
        locked.slug = slug
        locked.save(update_fields=["slug", "updated_at"])
    page.slug = slug
    return slug


# ---------------------------------------------------------------------------
# Adresserna
# ---------------------------------------------------------------------------


def page_path(page):
    """/lp/<konto>/<sida>/. En sida eller ett konto som saknar sin del (en
    äldre version skapade raden) får den nu."""
    if not page.slug:
        page.save(update_fields=["slug"])
    account_slug = ensure_account_slug(page.account)
    return reverse("flamingo_public:page", args=[account_slug, page.slug])


def page_url(page):
    """Sidans adress med schema och domän (exports.landing_base_url)."""
    from .exports import landing_base_url

    return landing_base_url().rstrip("/") + page_path(page)


def page_text(page):
    """Adressen utan schema och snedstreck sist: adx.se/lp/exempelror/avloppsspolning."""
    return page_url(page).split("://", 1)[-1].rstrip("/")


def page_for_address(account_slug, page_slug):
    """(sidan, gammal adress?) för /lp/<account_slug>/<page_slug>/, eller
    (None, False). Sidan hittas bara hos kontot med just den delen."""
    from .models import LandingPage, LandingPageOldSlug

    page = (
        LandingPage.objects.select_related("account__customer")
        .filter(account__lp_slug=account_slug, slug=page_slug)
        .first()
    )
    if page is not None:
        return page, False
    old = (
        LandingPageOldSlug.objects.select_related("page__account__customer")
        .filter(account__lp_slug=account_slug, slug=page_slug, page__account__lp_slug=account_slug)
        .first()
    )
    if old is not None:
        return old.page, True
    return None, False


# ---------------------------------------------------------------------------
# Migreringen
# ---------------------------------------------------------------------------


def assign_missing(apps=None):
    """Ge varje konto utan lp_slug och varje sida utan slug sin adress.
    Bara rader som saknar den, så att det kan köras igen. (konton, sidor)."""
    Account, Page, _old, _settings, _old_public = _models(apps)
    accounts = 0
    for account in Account.objects.filter(lp_slug__isnull=True).select_related("customer"):
        ensure_account_slug(account, apps)
        accounts += 1
    pages = 0
    for page in Page.objects.filter(slug="").order_by("account_id", "created_at", "pk"):
        slug = page_slug_for(page.account_id, page.name, exclude_pk=page.pk, apps=apps)
        Page.objects.filter(pk=page.pk, slug="").update(slug=slug)
        pages += 1
    return accounts, pages
