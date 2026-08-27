"""
Kontextoperationer: skrivguiden, väntande utkast och besöksstatistik.

Rena läsoperationer som ger modellen underlag - tonläge att följa, vad som
redan väntar på godkännande, och vilka sidor som går dåligt.
"""

from datetime import timedelta

from django.db.models import Avg, Count, Sum
from django.utils import timezone

from apps.analytics.models import PageView
from apps.assistant.models import DraftChange, Risk
from apps.website.models import SiteSettings

from .base import Operation, OperationError, Prepared, register

DEFAULT_STYLE_GUIDE = (
    "Skriv enkelt och rakt på svenska. Du-tilltal. Aldrig säljigt språk, inga "
    "utropstecken. Förklara tekniska termer kort när de används. Korta stycken. "
    "Inga tankstreck (—) eller andra AI-typiska skrivtecken; systemet ersätter "
    "dem ändå automatiskt."
)


def _skrivguide(user):
    settings_obj = SiteSettings.load()
    return {
        "skrivguide": (settings_obj.ai_style_guide or "").strip() or DEFAULT_STYLE_GUIDE,
        "ar_standard": not (settings_obj.ai_style_guide or "").strip(),
        "foretag": settings_obj.name,
        "variabler": {
            "{{ phone }}": "telefonnummer",
            "{{ email }}": "e-postadress",
            "{{ ort }}": "områdets namn (bara på områdessidor)",
            "{{ kommun }}": "kommunens namn (bara på områdessidor)",
            "{{ lan }}": "länets namn (bara på områdessidor)",
        },
        "regler": [
            "Hitta aldrig på priser, telefonnummer eller andra fakta.",
            "ADX utgår från Stockholm och jobbar på distans - hitta aldrig på "
            "lokala kontor eller kunder.",
            "Paketpriserna bor på paketsidan; allt annat är offert - skriv "
            "aldrig priser i löptext.",
            "All text du föreslår blir ett utkast som kunden godkänner.",
        ],
    }


def _lista_utkast(user):
    pending = (
        DraftChange.objects.filter(job__user=user, status=DraftChange.Status.PENDING)
        .select_related("job")
        .order_by("job_id", "created_at")
    )
    jobs = {}
    for change in pending:
        jobs.setdefault(change.job_id, {"jobb": str(change.job), "andringar": []})
        jobs[change.job_id]["andringar"].append(
            {
                "id": change.pk,
                "operation": change.operation,
                "riskklass": change.get_risk_display(),
                "sammanfattning": change.summary,
            }
        )
    return {
        "antal_vantande": pending.count(),
        "jobb": list(jobs.values()),
        "not": "Kunden godkänner utkasten i /manage/. Du kan inte godkänna dem själv.",
    }


def _statistik(user, dagar=30, antal=10):
    dagar = max(1, min(int(dagar), 365))
    antal = max(1, min(int(antal), 50))
    since = timezone.now() - timedelta(days=dagar)

    rows = (
        PageView.objects.filter(viewed_at__gte=since)
        .values("path")
        .annotate(
            visningar=Count("id"),
            engagerad_tid_snitt=Avg("engaged_seconds"),
            engagerad_tid_total=Sum("engaged_seconds"),
        )
        .order_by("-visningar")[:antal]
    )
    sidor = [
        {
            "sokvag": r["path"],
            "visningar": r["visningar"],
            "engagerad_tid_snitt_s": round(r["engagerad_tid_snitt"] or 0),
        }
        for r in rows
    ]
    # Svagast engagemang bland sidor med tillräckligt underlag för att
    # siffran ska betyda något.
    med_underlag = [s for s in sidor if s["visningar"] >= 5]
    svagast = sorted(med_underlag, key=lambda s: s["engagerad_tid_snitt_s"])[:5]
    return {
        "period_dagar": dagar,
        "mest_besokta": sidor,
        "svagast_engagemang": svagast,
        "not": (
            "Engagerad tid räknar bara tid då fliken var synlig. Sidor med "
            "under 5 visningar är utelämnade ur svagast_engagemang."
        ),
    }


register(
    Operation(
        name="hamta_skrivguide",
        description=(
            "Hämta kundens skrivguide, tillgängliga textvariabler och de hårda "
            "reglerna. Anropa denna FÖRST i varje session innan du skriver text."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk=Risk.READ,
        read=_skrivguide,
    )
)
register(
    Operation(
        name="lista_utkast",
        description="Lista dina utkast som väntar på kundens godkännande.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk=Risk.READ,
        read=_lista_utkast,
    )
)
register(
    Operation(
        name="hamta_statistik",
        description=(
            "Besöksstatistik per sida: mest besökta och de med svagast engagerad "
            "tid. Underlag för att föreslå vilka sidor som bör skrivas om."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "dagar": {"type": "integer"},
                "antal": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        risk=Risk.READ,
        read=_statistik,
        # Statistikmodulen är inte köpt - slås på med ASSISTANT_FEATURE_STATISTIK.
        feature="statistik",
    )
)


def _prepare_dra_tillbaka(job, user, utkast_id):
    """
    Dra tillbaka ett eget väntande utkast.

    Så här "ändrar" modellen ett liggande förslag: den drar tillbaka det
    gamla och lägger ett nytt. Att mutera ett utkast på plats vore värre -
    `before`-ögonblicksbilden och kundens redan lästa diff skulle sluta
    stämma. Bara EGNA väntande utkast; godkända ändringar ångras i
    /manage/, aldrig av modellen.
    """
    from apps.assistant.models import DraftChange

    change = DraftChange.objects.filter(
        pk=utkast_id, job__user=user, status=DraftChange.Status.PENDING
    ).first()
    if change is None:
        raise OperationError(
            f"Inget väntande utkast med id {utkast_id}. Använd lista_utkast för id:n."
        )
    if change.job_id != job.id:
        raise OperationError("Utkastet hör till ett annat uppdrag och rörs inte här.")
    return Prepared(
        payload={"utkast_id": change.pk},
        summary=f"Drar tillbaka: {change.summary}",
    )


def _apply_dra_tillbaka(user, payload, target):
    from apps.assistant import draft as draft_mod
    from apps.assistant.models import DraftChange

    change = DraftChange.objects.filter(
        pk=payload["utkast_id"], job__user=user, status=DraftChange.Status.PENDING
    ).first()
    if change is None:
        raise OperationError("Utkastet är redan avgjort.")
    draft_mod.reject(change, user)
    return None


register(
    Operation(
        name="dra_tillbaka_utkast",
        description=(
            "Dra tillbaka ett eget väntande utkast. Använd det när du vill "
            "ÄNDRA ett förslag du redan lagt: dra tillbaka det gamla och "
            "lägg ett nytt, i stället för att lägga två som motsäger "
            "varandra. Id:n kommer från lista_utkast. Godkända ändringar kan "
            "du inte röra."
        ),
        input_schema={
            "type": "object",
            "properties": {"utkast_id": {"type": "integer"}},
            "required": ["utkast_id"],
            "additionalProperties": False,
        },
        risk=Risk.TEXT,
        wants_job=True,
        prepare=_prepare_dra_tillbaka,
        apply=_apply_dra_tillbaka,
    )
)
