"""
Utkastflödet: från AI-förslag till godkänd ändring.

propose()  - kör operationens prepare och sparar en DraftChange (skriver
             inget till innehållet).
approve()  - kör apply inuti en revision med källa AI. Validerar om, eftersom
             läget kan ha ändrats sedan förslaget lades.
reject()   - avslår, och kaskad-avslår allt som berodde på ändringen.

Ingen annan kodväg får skriva AI-ändringar. Det är den här filen som gör
"AI:n kan bara föreslå" till en egenskap hos systemet i stället för en
förhoppning om modellens uppförande.
"""

import reversion
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from .models import AIJob, DraftChange, RevisionMeta, Risk
from .operations import REGISTRY, OperationError


def propose(job, operation_name, params):
    """
    Kör en operations prepare och spara resultatet som ett utkast.

    Höjer OperationError vid okänd operation eller ogiltig input - anroparen
    skickar tillbaka det som verktygsfel så modellen kan rätta sig.
    """
    op = REGISTRY.get(operation_name)
    if op is None:
        raise OperationError(f"Okänd operation: {operation_name}")
    if op.risk == Risk.READ:
        raise OperationError(f"{operation_name} är en läsoperation - använd den direkt.")

    # Operationer som behöver se jobbet får det; övriga signaturer rörs inte.
    prepared = (
        op.prepare(job, job.user, **params) if op.wants_job else op.prepare(job.user, **params)
    )

    change = DraftChange(
        job=job,
        operation=op.name,
        risk=op.risk,
        summary=prepared.summary,
        payload=prepared.payload,
        before=prepared.before,
    )
    if prepared.depends_on is not None:
        change.depends_on = prepared.depends_on
    if prepared.target is not None and getattr(prepared.target, "pk", None):
        change.target_ct = ContentType.objects.get_for_model(prepared.target)
        change.target_id = prepared.target.pk
    change.save()
    return change


def stale_fields(change, target):
    """
    Fält som någon annan hunnit ändra sedan utkastet lades.

    `before` är hur fältet såg ut när AI:n föreslog ändringen. Skiljer det
    sig från nuläget har någon redigerat under tiden, och ett godkännande
    skulle skriva över den redigeringen utan att kunden fick veta. Att
    upptäcka det är hela poängen med att spara `before`.
    """
    if not change.before or target is None:
        return set()

    changed = set()
    for field, was in change.before.items():
        if not hasattr(target, field):
            continue
        now = getattr(target, field)
        if hasattr(now, "pk"):
            now = now.pk
        # before lagras som JSON, så jämför i strängform - annars slår
        # Decimal mot str och datum mot ISO-sträng ut som falska träffar.
        if str(now if now is not None else "") != str(was if was is not None else ""):
            changed.add(field)
    return changed


@transaction.atomic
def approve(change, user, *, force=False):
    """
    Genomför ett utkast inuti en revision märkt som AI-ändring.

    Apply-steget kör samma formulär/sanering som manage-vyerna en gång till.
    Att validera om är avsiktligt: mellan förslag och godkännande kan kunden
    ha ändrat samma fält, eller raderat objektet.
    """
    if change.status != DraftChange.Status.PENDING:
        raise OperationError(f"Utkastet är redan {change.get_status_display().lower()}.")

    # Läs beroendets status ur DATABASEN, inte via change.depends_on: den
    # relationen kan bära ett cachat objekt vars status hunnit bli inaktuell
    # sedan det lästes in, och då nekas ett godkännande som borde gå igenom.
    if (
        change.depends_on_id
        and not DraftChange.objects.filter(
            pk=change.depends_on_id, status=DraftChange.Status.APPLIED
        ).exists()
    ):
        raise OperationError("Utkastet bygger på en tidigare ändring som inte är godkänd ännu.")

    op = REGISTRY.get(change.operation)
    if op is None:
        raise OperationError(f"Operationen {change.operation} finns inte längre.")

    target = change.target
    if change.target_ct_id and target is None:
        raise OperationError("Objektet som ändringen gäller finns inte längre.")

    stale = stale_fields(change, target)
    if stale and not force:
        raise OperationError(
            "Innehållet har ändrats sedan förslaget lades: "
            + ", ".join(sorted(stale))
            + ". Godkänner du nu skrivs den ändringen över. Granska diffen "
            "och bekräfta, eller avslå utkastet."
        )

    try:
        with reversion.create_revision():
            reversion.set_user(user)
            reversion.set_comment(f"AI: {change.summary}"[:200])
            reversion.add_meta(
                RevisionMeta,
                source=RevisionMeta.Source.AI,
                prompt=change.job.prompt or "",
                job=change.job,
            )
            obj = op.apply(user, dict(change.payload), target)
    except OperationError:
        raise
    except Exception as exc:  # noqa: BLE001 - felet ska synas för kunden, inte krascha vyn
        change.status = DraftChange.Status.FAILED
        change.error = f"{type(exc).__name__}: {exc}"
        change.decided_by = user
        change.decided_at = timezone.now()
        change.save(update_fields=["status", "error", "decided_by", "decided_at"])
        raise OperationError(f"Kunde inte genomföra ändringen: {exc}") from exc

    if obj is not None and getattr(obj, "pk", None) and not change.target_id:
        change.target_ct = ContentType.objects.get_for_model(obj)
        change.target_id = obj.pk
    change.status = DraftChange.Status.APPLIED
    change.decided_by = user
    change.decided_at = timezone.now()
    change.applied_at = timezone.now()
    change.save()

    _settle_job(change.job)
    return obj


def reject(change, user, cascade=True):
    """Avslå ett utkast. Ändringar som byggde på det avslås med."""
    if change.status != DraftChange.Status.PENDING:
        return change
    change.status = DraftChange.Status.REJECTED
    change.decided_by = user
    change.decided_at = timezone.now()
    change.save(update_fields=["status", "decided_by", "decided_at"])

    if cascade:
        for dependant in change.dependants.filter(status=DraftChange.Status.PENDING):
            reject(dependant, user, cascade=True)

    _settle_job(change.job)
    return change


def approve_many(changes, user):
    """
    Godkänn flera utkast i tur och ordning, beroenden först.

    Returnerar (antal_ok, [(change, felmeddelande)]). Ett fel stoppar inte de
    övriga - kunden ska inte förlora 19 godkännanden för att en rad krockade.
    """
    ordered = sorted(changes, key=lambda c: (c.depends_on_id or 0, c.pk))
    done, failed = 0, []
    for change in ordered:
        try:
            approve(change, user)
            done += 1
        except OperationError as exc:
            failed.append((change, str(exc)))
    return done, failed


def _settle_job(job):
    """Stäng jobbet när inget väntar längre."""
    if job.changes.filter(status=DraftChange.Status.PENDING).exists():
        return
    applied = job.changes.filter(status=DraftChange.Status.APPLIED).exists()
    job.status = AIJob.Status.APPLIED if applied else AIJob.Status.DISCARDED
    job.save(update_fields=["status", "updated_at"])


def undo_job(job, user):
    """
    Ångra ett helt AI-jobb: återställ varje berörd version i omvänd ordning.

    Bygger i första hand på revisionerna jobbet skapade - de vet hur hela
    objektet såg ut före. Saknas en tidigare version (objektet hade ingen
    historik när AI:n ändrade det) faller vi tillbaka på DraftChange.before,
    som håller de fält ändringen faktiskt rörde. Utan den fallbacken skulle
    "ångra" tyst göra ingenting, vilket är sämre än att göra en del.
    """
    from reversion.models import Version

    reverted = 0
    handled = set()

    with reversion.create_revision():
        reversion.set_user(user)
        reversion.set_comment(f"Ångrade AI-jobb: {job}")
        reversion.add_meta(RevisionMeta, source=RevisionMeta.Source.MANUAL)

        for meta in job.revisions.select_related("revision").order_by("-revision__id"):
            for version in meta.revision.version_set.all():
                previous = (
                    Version.objects.filter(
                        content_type=version.content_type,
                        object_id=version.object_id,
                        revision__id__lt=meta.revision_id,
                    )
                    .order_by("-revision__id")
                    .first()
                )
                if previous is not None:
                    previous._object_version.object.save()
                    handled.add((version.content_type_id, version.object_id))
                    reverted += 1

        # Fältvis fallback för det revisionerna inte kunde återställa.
        for change in job.changes.filter(status=DraftChange.Status.APPLIED):
            if not change.before or not change.target_id:
                continue
            if (change.target_ct_id, str(change.target_id)) in handled:
                continue
            obj = change.target
            if obj is None:
                continue
            for field, value in change.before.items():
                if hasattr(obj, field):
                    setattr(obj, field, value)
            obj.save()
            reverted += 1

    return reverted
