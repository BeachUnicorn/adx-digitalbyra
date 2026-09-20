"""
Operationer för offerter. Direktverktyg - men bara utkast.

Det som INTE finns här är poängen: inget verktyg skickar, mejlar, ändrar
status eller tar bort. En offert som skapas härifrån är ett utkast tills
Giovanni själv trycker Skicka i offertbyggaren, och en accepterad offert är
en affärshandling som inte går att röra alls.
"""

from datetime import timedelta

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from apps.assistant.models import Risk
from apps.offers.models import PricePeriod, Product, Quote, QuoteLine, QuoteStatus

from .arenden_ops import base_url, clean_text, find_project, parse_date, require_agency, when
from .base import Operation, OperationError, register

#: Statusar där innehållet fortfarande får ändras. Accepterad är låst;
#: förlorad är avslutad - en ny affär är en ny offert.
_EDITABLE = (QuoteStatus.DRAFT, QuoteStatus.SENT, QuoteStatus.OPENED)


def _quote(quote_id):
    quote = Quote.objects.filter(pk=quote_id).first()
    if quote is None:
        raise OperationError(f"Okänd offert: {quote_id}. Använd lista_offerter för id:n.")
    return quote


def _edit_url(quote):
    return f"{base_url()}/manage/offerter/{quote.pk}/"


def _quote_has_project():
    """
    Kollas vid körning, inte vid import: fältet läggs till i offers-appen
    parallellt, och den här modulen ska fungera både före och efter.
    """
    try:
        Quote._meta.get_field("project")
    except FieldDoesNotExist:
        return False
    return True


def _quote_row(quote):
    sums = quote.totals()
    row = {
        "id": quote.pk,
        "kund": quote.customer_name,
        "projekt_titel": quote.project_title,
        "status": quote.status,
        "status_text": quote.get_status_display(),
        "summa_engang": sums[PricePeriod.ONE_TIME],
        "summa_manad": sums[PricePeriod.MONTHLY],
        "summa_ar": sums[PricePeriod.YEARLY],
        "giltig_till": quote.valid_until.isoformat() if quote.valid_until else None,
        "uppdaterad": when(quote.updated_at),
        "lank": base_url() + quote.get_public_url(),
        "redigera": _edit_url(quote),
    }
    if _quote_has_project():
        project = getattr(quote, "project", None)
        row["projekt"] = getattr(project, "key", None)
    return row


def _price(value, where):
    # Tål "12 500" som byggaren gör, men aldrig True/False som råkar vara int.
    if isinstance(value, bool):
        raise OperationError(f"{where}: pris ska vara ett heltal i kronor exkl. moms.")
    if isinstance(value, str):
        value = value.replace(" ", "")
    try:
        price = int(value)
    except (TypeError, ValueError):
        raise OperationError(f"{where}: pris ska vara ett heltal i kronor exkl. moms.") from None
    if price < 0 or price > 99_999_999:
        raise OperationError(f"{where}: pris måste vara mellan 0 och 99 999 999 kr.")
    return price


def _line_values(spec, where):
    """
    En rads fält ur modellens specifikation. Med produkt_id kopieras namn,
    beskrivning, pris och period från katalogen till raden - därefter äger
    raden sina värden, precis som i byggaren.
    """
    if not isinstance(spec, dict):
        raise OperationError(f"{where}: ska vara ett objekt med rad, pris och period.")
    product = None
    if spec.get("produkt_id") is not None:
        product = Product.objects.filter(pk=spec["produkt_id"], is_active=True).first()
        if product is None:
            known = (
                ", ".join(f"{p.name} (id {p.pk})" for p in Product.objects.filter(is_active=True))
                or "(inga)"
            )
            raise OperationError(
                f"{where}: okänd produkt {spec['produkt_id']}. Aktiva produkter: {known}"
            )

    label = clean_text(spec.get("rad"), "rad", 200) or (product.name if product else "")
    if not label:
        raise OperationError(f"{where}: ange rad (rubriken) eller produkt_id.")
    description = clean_text(spec.get("beskrivning"), "beskrivning", 2000, multiline=True) or (
        product.description if product else ""
    )
    price = spec.get("pris")
    if price is None:
        if product is None:
            raise OperationError(f"{where}: ange pris (hela kronor exkl. moms) eller produkt_id.")
        price = product.default_price
    period = spec.get("period") or (product.default_period if product else PricePeriod.ONE_TIME)
    if period not in PricePeriod.values:
        raise OperationError(f"{where}: period ska vara one_time, monthly eller yearly.")
    return {
        "product": product,
        "label": label,
        "description": description,
        "price": _price(price, where),
        "period": period,
        "is_optional": bool(spec.get("tillval", False)),
    }


# --- Läsoperationer ---------------------------------------------------------


def _lista_offerter(user, status=None):
    require_agency(user)
    qs = Quote.objects.prefetch_related("lines")
    if status:
        if status not in QuoteStatus.values:
            raise OperationError("Okänd status. Välj: " + ", ".join(QuoteStatus.values))
        qs = qs.filter(status=status)
    return {"offerter": [_quote_row(q) for q in qs[:200]]}


def _hamta_offert(user, id):
    require_agency(user)
    quote = _quote(id)
    row = _quote_row(quote)
    row.update(
        {
            "kund_epost": quote.customer_email,
            "halsning": quote.intro,
            "skickad": when(quote.sent_at),
            "oppnad": when(quote.opened_at),
            "accepterad": when(quote.accepted_at),
            "rader": [
                {
                    "id": line.pk,
                    "rad": line.label,
                    "beskrivning": line.description,
                    "pris": line.price,
                    "period": line.period,
                    "tillval": line.is_optional,
                    "vald": line.is_selected,
                    "produkt_id": line.product_id,
                }
                for line in quote.lines.all()
            ],
            "summor": quote.totals(),
            "last": quote.status == QuoteStatus.ACCEPTED,
        }
    )
    return row


def _lista_produkter(user):
    require_agency(user)
    return {
        "produkter": [
            {
                "id": p.pk,
                "namn": p.name,
                "riktpris": p.default_price,
                "period": p.default_period,
                "beskrivning": p.description,
            }
            for p in Product.objects.filter(is_active=True)
        ]
    }


# --- Direktoperationer ------------------------------------------------------


def _skapa_offert(
    user,
    kund_namn,
    kund_epost="",
    projekt_titel="",
    halsning="",
    giltig_till=None,
    rader=None,
    projekt=None,
):
    require_agency(user)
    name = clean_text(kund_namn, "kund_namn", 200)
    if not name:
        raise OperationError("kund_namn får inte vara tomt.")
    email = str(kund_epost or "").strip()
    if email:
        try:
            validate_email(email)
        except ValidationError:
            raise OperationError("kund_epost är ingen giltig e-postadress.") from None
    if rader is not None and not isinstance(rader, list):
        raise OperationError("rader ska vara en lista med radobjekt.")
    lines = [_line_values(spec, f"Rad {i}") for i, spec in enumerate(rader or [], start=1)]

    quote = Quote(
        customer_name=name,
        customer_email=email,
        project_title=clean_text(projekt_titel, "projekt_titel", 200),
        intro=clean_text(halsning, "halsning", 5000, multiline=True),
        # Alltid utkast. Det finns ingen parameter för status, och ska inte finnas.
        status=QuoteStatus.DRAFT,
        valid_until=parse_date(giltig_till, "giltig_till")
        or (timezone.localdate() + timedelta(days=30)),
        created_by=user,
    )
    if projekt:
        if not _quote_has_project():
            raise OperationError("Offerter kan inte kopplas till projekt ännu - utelämna projekt.")
        quote.project = find_project(projekt)
    quote.save()
    for order, values in enumerate(lines, start=1):
        QuoteLine.objects.create(quote=quote, order=order, **values)

    return {
        "status": "utkast",
        "offert_id": quote.pk,
        "kund": quote.customer_name,
        "projekt": getattr(getattr(quote, "project", None), "key", None),
        "rader": len(lines),
        "summor": quote.totals(),
        "giltig_till": quote.valid_until.isoformat(),
        "redigera": _edit_url(quote),
        "not": (
            "Offerten är ett utkast och är INTE skickad. Giovanni skickar den själv "
            "från offertbyggaren."
        ),
    }


def _lagg_till_offertrad(
    user,
    offert_id,
    rad=None,
    pris=None,
    period=None,
    beskrivning="",
    tillval=False,
    produkt_id=None,
):
    require_agency(user)
    quote = _quote(offert_id)
    if quote.status == QuoteStatus.ACCEPTED:
        raise OperationError("Accepterad offert är låst.")
    if quote.status not in _EDITABLE:
        raise OperationError(
            f"Offerten är {quote.get_status_display().lower()} och kan inte ändras - "
            f"skapa en ny offert."
        )
    values = _line_values(
        {
            "rad": rad,
            "pris": pris,
            "period": period,
            "beskrivning": beskrivning,
            "tillval": tillval,
            "produkt_id": produkt_id,
        },
        "Raden",
    )
    last = quote.lines.order_by("-order").first()
    line = QuoteLine.objects.create(quote=quote, order=(last.order + 1) if last else 1, **values)
    quote.save(update_fields=["updated_at"])
    return {
        "status": "tillagd",
        "rad_id": line.pk,
        "offert_id": quote.pk,
        "offertstatus": quote.status,
        "summor": quote.totals(),
        "redigera": _edit_url(quote),
        "not": "Raden är tillagd. Offerten skickas inte om härifrån - det gör Giovanni själv.",
    }


# --- Registrering -----------------------------------------------------------

_S = {"type": "string"}
_I = {"type": "integer"}
_B = {"type": "boolean"}
_PERIOD = {"type": "string", "enum": [p.value for p in PricePeriod]}


def _schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_LINE = {
    "type": "object",
    "properties": {
        "rad": {**_S, "description": "Radens rubrik. Kan utelämnas om produkt_id anges."},
        "beskrivning": _S,
        "pris": {**_I, "description": "Hela kronor exkl. moms. Kan utelämnas med produkt_id."},
        "period": _PERIOD,
        "tillval": {**_B, "description": "Kunden väljer själv om raden ska ingå."},
        "produkt_id": {**_I, "description": "Kopierar namn, beskrivning, pris och period."},
    },
    "additionalProperties": False,
}

register(
    Operation(
        name="lista_offerter",
        description=(
            "Lista offerter med status, summor per pristyp (engång/månad/år, kr exkl. "
            "moms), kundens länk och länk till byggaren. Filtrera på status: draft, "
            "sent, opened, accepted, declined."
        ),
        input_schema=_schema({"status": {"type": "string", "enum": list(QuoteStatus.values)}}),
        risk=Risk.READ,
        read=_lista_offerter,
    )
)
register(
    Operation(
        name="hamta_offert",
        description=(
            "Hämta en offert med alla rader (id, rad, pris, period, tillval, vald), "
            "summor, status och giltighet."
        ),
        input_schema=_schema({"id": _I}, ["id"]),
        risk=Risk.READ,
        read=_hamta_offert,
    )
)
register(
    Operation(
        name="lista_produkter",
        description=(
            "Lista aktiva produkter i katalogen med riktpris och pristyp. Använd "
            "produkt_id i skapa_offert/lagg_till_offertrad för att kopiera in dem."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk=Risk.READ,
        read=_lista_produkter,
    )
)
register(
    Operation(
        name="skapa_offert",
        description=(
            "Skapa ett OFFERTUTKAST med rader. Offerten får alltid status utkast och "
            "skickas inte - det gör Giovanni från offertbyggaren (länk i svaret). "
            "giltig_till är 30 dagar fram om den utelämnas. Rader med produkt_id "
            "kopierar katalogens namn, beskrivning, pris och period; annars anges rad "
            "och pris (hela kronor exkl. moms). Hitta aldrig på priser."
        ),
        input_schema=_schema(
            {
                "kund_namn": _S,
                "kund_epost": _S,
                "projekt_titel": _S,
                "halsning": {**_S, "description": "Visas överst på kundens offertsida."},
                "giltig_till": {**_S, "description": "YYYY-MM-DD"},
                "rader": {"type": "array", "items": _LINE},
                "projekt": {
                    **_S,
                    "description": "Projektnyckel att koppla offerten till (valfritt).",
                },
            },
            ["kund_namn"],
        ),
        risk=Risk.ACTION,
        run=_skapa_offert,
    )
)
register(
    Operation(
        name="lagg_till_offertrad",
        description=(
            "Lägg till en rad sist på en offert som inte är accepterad. Med produkt_id "
            "kopieras katalogposten; annars krävs rad och pris. Offerten skickas inte "
            "om."
        ),
        input_schema=_schema(
            {
                "offert_id": _I,
                "rad": _S,
                "pris": _I,
                "period": _PERIOD,
                "beskrivning": _S,
                "tillval": _B,
                "produkt_id": _I,
            },
            ["offert_id"],
        ),
        risk=Risk.ACTION,
        run=_lagg_till_offertrad,
    )
)
