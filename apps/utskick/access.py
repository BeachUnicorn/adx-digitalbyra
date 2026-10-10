"""
Vem som får vad i Kontakter och Utskick (README H.1). Reglerna bor här.

    settings_for(account)        kundens UtskickSettings, eller en osparad standard
    is_enabled(account)          Flamingo och utskick på för kontot
    dpa_ok(account)              senaste godkännandet gäller aktuell version (demo: alltid)
    can_collect(account)         is_enabled och dpa_ok: krävs för varje ny kontakt
    collect_block_reason(account)  texten som förklarar varför det inte går, eller ""
    utskick_view                 dekoratorn för varje vy under kontakter/ och utskick/
    owned(model, account, pk)    ett id ur adressen, 404 för ett annat kontos
    owned_ids(model, account, ids)  id:n ur ett formulär eller JSON, ForeignIds för främmande
    actor_for(request)           vem som gör ändringen (för loggarna)
    validate_public_slug, suggest_public_slug   adressen för anmälan (upptagen också om
                                 ett annat konto hade den förut, OldPublicSlug)
    account_for_public_slug(slug) -> account_id | None   nuvarande adress, annars en
                                 tidigare adress som kontot fortfarande har
    retire_public_slug(row, new_slug, now)   ett byte: den gamla adressen följer kontot

Varje vy i verktyget går via utskick_view: Flamingos app_view (behörighet
och kundens konto) plus kravet att utskick är aktiverat, annars 404, också
för byrån i kundvyn. Byrån i kundvyn gör det kunden gör, på riktigt och i
byråns namn (actor_for ger staff=True).

Varje id som kommer från en förfrågans kropp eller JSON går genom
owned_ids; ett enda främmande id ger 400 för hela förfrågan (utskick_view
fångar ForeignIds). Publika vyer tar aldrig kontot ur en parameter, bara ur
en signerad token eller en sparad kod.
"""

from dataclasses import dataclass
from functools import wraps

from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404

from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    RESERVED_PUBLIC_SLUG_PREFIXES,
    RESERVED_PUBLIC_SLUGS,
    DpaVersion,
    OldPublicSlug,
    UtskickSettings,
    default_consent_text,
)

DPA_MISSING_TEXT = "Avtalssidan saknas. Kontakta ADX."
DPA_NEEDED_TEXT = "Lägg till kontakter: godkänn biträdesavtalet först."
NOT_ENABLED_TEXT = "Utskick är inte aktiverat för dig. Be ADX slå på det."
FOREIGN_IDS_TEXT = "Urvalet innehåller något som inte finns hos dig."


class ForeignIds(Exception):
    """Ett id i förfrågan hör inte till kontot (eller är inget id)."""


# ---------------------------------------------------------------------------
# Kontots inställningar och aktivering
# ---------------------------------------------------------------------------


def settings_for(account):
    """Kontots UtskickSettings, eller en osparad standard med kundens namn
    (avstängd). Läses om från databasen varje gång: en avstängning gäller
    direkt."""
    row = UtskickSettings.objects.filter(account=account).first()
    if row is not None:
        return row
    name = (account.customer.name if account.customer_id else "")[:80]
    return UtskickSettings(
        account=account,
        is_enabled=False,
        display_name=name,
        public_slug="",
        consent_text_sms=default_consent_text(CHANNEL_SMS, name),
        consent_text_email=default_consent_text(CHANNEL_EMAIL, name),
    )


def is_enabled(account, utskick_settings=None):
    """Utskick på: byrån har aktiverat det, Flamingo är på och kunden aktiv."""
    if account is None or not account.is_enabled:
        return False
    if account.customer_id and not account.customer.is_active:
        return False
    row = utskick_settings if utskick_settings is not None else settings_for(account)
    return bool(row.pk and row.is_enabled)


def current_dpa():
    """Den aktuella versionen av biträdesavtalet, eller None."""
    return DpaVersion.objects.filter(is_current=True).first()


def latest_acceptance(account):
    return (
        account.utskick_dpa.select_related("version", "accepted_by")
        .order_by("-accepted_at", "-pk")
        .first()
    )


def dpa_ok(account):
    """True när kontots senaste godkännande gäller den aktuella versionen.
    Alltid True för demokontot (README L, Ops 34)."""
    if account.is_demo:
        return True
    acceptance = latest_acceptance(account)
    return bool(acceptance and acceptance.version.is_current)


def can_collect(account):
    """Får kontot ta in nya kontakter (alla vägar: manuellt, import,
    anmälan, landningssidan, svar, API)? Utskick på och avtalet godkänt."""
    return is_enabled(account) and dpa_ok(account)


def collect_block_reason(account):
    """Varför kontot inte får ta in kontakter, i klartext, eller ""."""
    if not is_enabled(account):
        return NOT_ENABLED_TEXT
    if dpa_ok(account):
        return ""
    if current_dpa() is None:
        return DPA_MISSING_TEXT
    return DPA_NEEDED_TEXT


# ---------------------------------------------------------------------------
# Vyer och id:n
# ---------------------------------------------------------------------------


def utskick_view(view):
    """Vy under /flamingo/app/kontakter/ och /flamingo/app/utskick/.

    Anropas som view(request, account, *args, **kwargs), precis som
    app_view. 404 när utskick inte är på för kontot (också för byrån i
    kundvyn); 400 när owned_ids hittar ett främmande id.
    request.utskick_settings är kontots inställningar."""
    from apps.flamingo.app_views import app_view

    @app_view
    @wraps(view)
    def wrapper(request, account, *args, **kwargs):
        row = settings_for(account)
        if not is_enabled(account, row):
            raise Http404
        request.utskick_settings = row
        try:
            return view(request, account, *args, **kwargs)
        except ForeignIds:
            return HttpResponseBadRequest(
                FOREIGN_IDS_TEXT, content_type="text/plain; charset=utf-8"
            )

    return wrapper


def owned(model, account, pk, via="account"):
    """Raden med pk som hör till kontot, annars 404. via är vägen till
    kontot för en rad utan eget konto ("list__account" för en plats i en
    lista, "contact__account" för ett samtycke)."""
    return get_object_or_404(model, **{"pk": pk, via: account})


def _as_ids(ids):
    if ids is None:
        return []
    if isinstance(ids, (str, int)):
        ids = [ids]
    out = []
    for raw in ids:
        if isinstance(raw, bool):
            raise ForeignIds
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, str) and raw.strip().isdigit():
            value = int(raw.strip())
        else:
            raise ForeignIds
        if value <= 0:
            raise ForeignIds
        if value not in out:
            out.append(value)
    return out


def owned_ids(model, account, ids, via="account", limit=None):
    """Id:n ur en förfrågans kropp eller JSON, prövade mot kontot: listan
    (i samma ordning, utan dubbletter) när alla hör till kontot, annars
    ForeignIds (vyn svarar 400). Tom lista in ger tom lista ut. Med limit
    nekas fler id:n än så."""
    wanted = _as_ids(ids)
    if not wanted:
        return []
    if limit is not None and len(wanted) > limit:
        raise ForeignIds
    found = set(
        model.objects.filter(**{via: account, "pk__in": wanted}).values_list("pk", flat=True)
    )
    if len(found) != len(wanted):
        raise ForeignIds
    return wanted


# ---------------------------------------------------------------------------
# Vem som gör ändringen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    """Vem som ändrar: sparas på samtyckesloggen, importen, exporten och
    godkännandet. staff är byrån i kundvyn."""

    user: object = None
    label: str = ""
    staff: bool = False


#: Personen själv på en publik sida (anmälan, bekräftelse, Mina utskick).
PERSON = Actor(label="Personen själv")
#: Systemet (ticken, importen i bakgrunden när ingen användare finns).
SYSTEM = Actor(label="ADX Flamingo")


def user_label(user):
    if user is None:
        return ""
    name = (user.get_full_name() or "").strip()
    return (name or user.email or user.get_username())[:120]


def actor_for(request):
    """Actor för en förfrågan i verktyget: kontakten, eller byrån i
    kundvyn som "ADX (Giovanni)"."""
    from apps.flamingo.access import VIEWING_AS

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return PERSON
    access = getattr(request, "flamingo", None)
    staff = bool(access is not None and access.mode == VIEWING_AS) or bool(user.is_staff)
    if staff:
        first = (user.first_name or user.get_username()).strip()
        return Actor(user=user, label=f"ADX ({first})"[:120], staff=True)
    return Actor(user=user, label=user_label(user), staff=False)


# ---------------------------------------------------------------------------
# Adressen för anmälan (public_slug)
# ---------------------------------------------------------------------------


def reserved_slugs():
    """Adresser som aldrig blir en kunds public_slug."""
    reserved = set(RESERVED_PUBLIC_SLUGS)
    try:
        from apps.manage.forms import BlockPageForm

        for slugs in BlockPageForm.RESERVED_SLUGS.values():
            reserved.update(slugs)
    except ImportError:  # pragma: no cover - panelen finns alltid i den här sajten
        pass
    return reserved


def is_reserved_slug(slug, reserved=None):
    """Är adressen reserverad: i listan, eller (S4) början på en sökväg som
    länkvärdarna aldrig släpper fram till appen (RESERVED_PUBLIC_SLUG_PREFIXES)?"""
    if reserved is None:
        reserved = reserved_slugs()
    return slug in reserved or slug.startswith(RESERVED_PUBLIC_SLUG_PREFIXES)


OLD_SLUG_TEXT = (
    "Adressen har använts av en annan kund. Länkar och QR-koder med den kan finnas "
    "kvar, så den går inte att använda."
)


def _account_of_row(exclude_pk):
    if not exclude_pk:
        return None
    return (
        UtskickSettings.objects.filter(pk=exclude_pk).values_list("account_id", flat=True).first()
    )


def _old_slug_taken(slug, account_id):
    """Har ett annat konto (eller ett borttaget) haft adressen förut?"""
    rows = OldPublicSlug.objects.filter(slug=slug)
    if account_id:
        rows = rows.exclude(account_id=account_id)
    return rows.exists()


def validate_public_slug(slug, exclude_pk=None, account_id=None):
    """ValidationError om adressen är reserverad, felaktig eller upptagen:
    ett annat kontos nuvarande adress, eller en adress som ett annat konto
    hade förut (OldPublicSlug: tryckta länkar och QR-koder pekar dit).
    Kontot (account_id, annars raden exclude_pk:s) får ta tillbaka sin egen."""
    from django.core.validators import validate_slug

    slug = (slug or "").strip().lower()
    if not slug:
        raise ValidationError("Skriv en adress.")
    validate_slug(slug)
    if len(slug) > 40:
        raise ValidationError("Adressen får vara högst 40 tecken.")
    if is_reserved_slug(slug):
        raise ValidationError("Adressen är reserverad. Välj en annan.")
    taken = UtskickSettings.objects.filter(public_slug=slug)
    if exclude_pk:
        taken = taken.exclude(pk=exclude_pk)
    if taken.exists():
        raise ValidationError("Adressen används redan av en annan kund.")
    if _old_slug_taken(slug, account_id or _account_of_row(exclude_pk)):
        raise ValidationError(OLD_SLUG_TEXT)
    return slug


def suggest_public_slug(name, exclude_pk=None, account_id=None):
    """Ett förslag ur företagsnamnet (flamingo.models.company_slug), ledigt
    och inte reserverat (en annan kunds tidigare adress är inte ledig);
    krockar får -2, -3 och så vidare."""
    from apps.flamingo.models import company_slug

    base = company_slug(name)[:40].strip("-") or "foretaget"
    if base.startswith(RESERVED_PUBLIC_SLUG_PREFIXES):
        # S4: -2 räddar inte en reserverad början (tokenbolaget-2).
        base = f"kund-{base}"[:40].rstrip("-")
    reserved = reserved_slugs()
    taken = UtskickSettings.objects.all()
    if exclude_pk:
        taken = taken.exclude(pk=exclude_pk)
    account_id = account_id or _account_of_row(exclude_pk)
    slug, n = base, 2
    while (
        is_reserved_slug(slug, reserved)
        or taken.filter(public_slug=slug).exists()
        or _old_slug_taken(slug, account_id)
    ):
        suffix = f"-{n}"
        slug = base[: 40 - len(suffix)].rstrip("-") + suffix
        n += 1
    return slug


def account_for_public_slug(slug):
    """Kontots id för adressen: den nuvarande (UtskickSettings.public_slug),
    annars en tidigare adress som kontot fortfarande har (OldPublicSlug med
    ett konto). None när ingen har den."""
    slug = str(slug or "")
    found = (
        UtskickSettings.objects.filter(public_slug=slug)
        .values_list("account_id", flat=True)
        .first()
    )
    if found:
        return found
    return (
        OldPublicSlug.objects.filter(slug=slug, account__isnull=False)
        .values_list("account_id", flat=True)
        .first()
    )


def retire_public_slug(row, new_slug, now=None):
    """Kontots adress byts från row.public_slug till new_slug (anropas i
    samma transaktion som sparningen). Den gamla adressen sparas som
    OldPublicSlug för kontot, så att tryckta länkar och QR-koder fortsätter
    till samma kund; tar kontot tillbaka en egen tidigare adress tas den
    raden bort."""
    from django.utils import timezone

    old = str(row.public_slug or "")
    if not row.pk or not old or old == new_slug:
        return
    now = now or timezone.now()
    OldPublicSlug.objects.filter(slug=new_slug, account_id=row.account_id).delete()
    OldPublicSlug.objects.update_or_create(
        slug=old, defaults={"account_id": row.account_id, "retired_at": now}
    )
