"""
Operationer för ärendesystemet: kunder, projekt, ärenden, tid.

Direktverktyg (Risk.ACTION): de skriver utan utkast, för det är byråns egna
arbetsdata och samma person som pratar med assistenten sitter med tavlan.
Se Risk i models.py för resonemanget.

Gränsen är byggd som FRÅNVARO av verktyg, inte som instruktioner till
modellen - en instruktion kan glömmas, ett verktyg som inte finns kan inte
anropas:

- Inget raderar. Inte ärenden, tidsposter, kommentarer, projekt eller kunder.
- Ingen timer. Tid loggas bara i efterhand med explicita minuter, så en
  modell som fastnat kan inte lämna en klocka tickande i tre dygn.
- Inget mejlar kunden. Kommentarer är interna om inget annat sägs, och inte
  ens en kundsynlig kommentar skickar mejl - det gör Giovanni från tavlan.
- synlig_for_kund sätts aldrig till sant utan explicit parameter.

Alla operationer börjar med require_agency: kundkontakter (Customer.users)
får aldrig läsa andra kunders ärenden via MCP, oavsett vad portalen visar.
"""

import re
from datetime import date

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.forms.models import model_to_dict
from django.utils import timezone

from apps.assistant.models import Risk
from apps.common.security import sanitize_multiline_text, sanitize_plain_text
from apps.projects.access import is_agency_user
from apps.projects.board import STAGES, move_issue, with_time
from apps.projects.forms import CustomerForm, IssueForm, ProjectForm
from apps.projects.models import (
    ChecklistItem,
    Comment,
    Customer,
    Issue,
    IssuePriority,
    IssueType,
    Label,
    Project,
    ProjectStatus,
    TimeEntry,
)

from .base import Operation, OperationError, register

#: Fler rader än så hjälper ingen modell - den ska avgränsa i stället.
MAX_ROWS = 200

#: Samma texter som tavlan loggar när fälten ändras där, så aktivitetsloggen
#: läser likadant oavsett om det var ett klick eller ett verktygsanrop.
_FIELD_LOG = {
    "priority": lambda i: f"prioritet: {i.get_priority_display()}",
    "assignee": lambda i: (
        f"satte ansvarig: {i.assignee.first_name or i.assignee.get_username()}"
        if i.assignee
        else "tog bort ansvarig"
    ),
    "due_on": lambda i: f"förfaller {i.due_on:%-d %b}" if i.due_on else "tog bort förfallodatum",
    "visible_to_customer": lambda i: (
        "synlig för kund: på" if i.visible_to_customer else "synlig för kund: av"
    ),
}

_PRIORITY = {
    "lag": IssuePriority.LOW,
    "normal": IssuePriority.NORMAL,
    "hog": IssuePriority.HIGH,
    "akut": IssuePriority.URGENT,
}

_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9]*)-(\d+)$")


# --- Gemensamma hjälpare (används även av offert_ops) -----------------------


def require_agency(user):
    """
    Byrån och bara byrån. Portalens kundkontakter är inloggade användare
    med giltiga tokens, men ärendesystemet är inte deras - och via MCP
    finns ingen portalvy som filtrerar bort andra kunders rader.
    """
    if user is None or not is_agency_user(user):
        raise OperationError("Ärendesystemet är bara för byrån.")


def base_url():
    return (getattr(settings, "SITE_BASE_URL", "") or "").rstrip("/")


def clean_text(value, field, max_length, multiline=False):
    """
    Sanera fritext som manage-vyerna gör, men FEL vid överlängd i stället
    för tyst kapning - modellen ser aldrig resultatet och skulle upprepa
    misstaget.
    """
    sanitiser = sanitize_multiline_text if multiline else sanitize_plain_text
    text = sanitiser("" if value is None else str(value), max_length=10**7)
    if len(text) > max_length:
        raise OperationError(
            f"{field} är {len(text) - max_length} tecken för lång (högst {max_length})."
        )
    return text


def parse_date(value, field):
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise OperationError(f"{field}: ange datum som YYYY-MM-DD.") from None


def when(dt):
    """Tidsstämpel i lokal tid, minutupplösning - JSON-vänligt och läsbart."""
    return timezone.localtime(dt).isoformat(timespec="minutes") if dt else None


def find_project(key):
    project = (
        Project.objects.select_related("customer")
        .filter(key=str(key or "").strip().upper())
        .first()
    )
    if project is None:
        known = ", ".join(Project.objects.values_list("key", flat=True)) or "(inga)"
        raise OperationError(f"Okänt projekt: {key}. Kända nycklar: {known}")
    return project


def _form(form_class, instance, data):
    """
    Kör manage-vyns formulär på färdigsanerade värden; fel blir OperationError.

    Inte run_form: dess "tyst dataförlust"-heuristik jämför råtext mot
    HTML-saneraren, och här är fälten ren text som redan sanerats med
    explicit längdfel. Dessutom ger model_to_dict M2M-fält som objekt, som
    formuläret inte kan validera - se _issue_data.
    """
    form = form_class(data=data, instance=instance)
    if not form.is_valid():
        lines = [f"{name}: {'; '.join(errs)}" for name, errs in form.errors.items()]
        raise OperationError("Ogiltig input - " + " | ".join(lines))
    return form


def _issue(ref):
    """Ärende via nyckel (NORD-3, tål gemener) eller id (123, #123)."""
    text = str(ref or "").strip().lstrip("#")
    qs = Issue.objects.select_related(
        "project", "project__customer", "customer", "column", "assignee", "reporter"
    )
    match = _KEY.match(text)
    if match:
        issue = qs.filter(project__key=match.group(1).upper(), number=int(match.group(2))).first()
    elif text.isdigit():
        issue = qs.filter(pk=int(text)).first()
    else:
        raise OperationError(f"Ogiltig ärendenyckel: {ref}. Ange NORD-3 eller ett id som 123.")
    if issue is None:
        raise OperationError(f"Okänt ärende: {text}. Använd lista_arenden för nycklar.")
    return issue


def _customer(ref):
    """Kund via id eller namn. Exakt namn först, annars entydig delträff."""
    text = str(ref or "").strip()
    if not text:
        raise OperationError("Ange kund som id eller namn.")
    if text.isdigit():
        customer = Customer.objects.filter(pk=int(text)).first()
    else:
        customer = Customer.objects.filter(name__iexact=text).first()
        if customer is None:
            hits = list(Customer.objects.filter(name__icontains=text)[:3])
            if len(hits) == 1:
                customer = hits[0]
            elif hits:
                names = ", ".join(f"{c.name} (id {c.pk})" for c in hits)
                raise OperationError(f"Flera kunder matchar '{text}': {names}. Ange id.")
    if customer is None:
        known = ", ".join(f"{c.name} (id {c.pk})" for c in Customer.objects.all()[:50]) or "(inga)"
        raise OperationError(f"Okänd kund: {text}. Kända: {known}")
    return customer


def _staff(ref):
    """Byråanvändare via användarnamn eller förnamn - samma urval som tavlans ansvarig-lista."""
    text = str(ref or "").strip()
    users = get_user_model().objects.filter(is_staff=True, is_active=True)
    user = (
        users.filter(username__iexact=text).first() or users.filter(first_name__iexact=text).first()
    )
    if user is None:
        known = ", ".join(
            f"{u.get_username()} ({u.first_name})" if u.first_name else u.get_username()
            for u in users
        )
        raise OperationError(f"Okänd ansvarig: {text}. Byråns användare: {known or '(inga)'}")
    return user


def _column(project, title):
    column = project.columns.filter(title__iexact=str(title or "").strip()).first()
    if column is None:
        known = ", ".join(project.columns.values_list("title", flat=True))
        raise OperationError(f"Okänd kolumn i {project.key}: {title}. Kolumner: {known}")
    return column


def _priority(value):
    key = str(value or "normal").strip().lower().replace("å", "a").replace("ö", "o")
    if key not in _PRIORITY:
        raise OperationError(f"Okänd prioritet: {value}. Välj lag, normal, hog eller akut.")
    return _PRIORITY[key]


def _labels(names):
    """Etiketter matchas mot befintliga. Okända skapas INTE - felet listar de kända."""
    if not isinstance(names, list):
        raise OperationError("etiketter ska vara en lista med etikettnamn.")
    labels = []
    for name in names:
        label = Label.objects.filter(name__iexact=str(name).strip()).first()
        if label is None:
            known = ", ".join(Label.objects.values_list("name", flat=True)) or "(inga)"
            raise OperationError(
                f"Okänd etikett: {name}. Etiketter skapas i tavlan, inte här. Kända: {known}"
            )
        labels.append(label)
    return labels


def _who(user):
    if user is None:
        return None
    return user.first_name or user.get_username()


def _issue_url(issue):
    return f"{base_url()}/manage/arenden/{issue.pk}/"


def _issue_row(issue):
    """Radformatet för listor. Förutsätter with_time (issue.seconds)."""
    done, total = issue.checklist_progress()
    customer = issue.effective_customer
    return {
        "id": issue.pk,
        "nyckel": issue.key,
        "rubrik": issue.title,
        "projekt": issue.project.key if issue.project_id else None,
        "kund": customer.name if customer else None,
        "kolumn": issue.column.title if issue.column_id else None,
        "steg": issue.stage,
        "prioritet": issue.get_priority_display(),
        "ansvarig": _who(issue.assignee),
        "forfaller": issue.due_on.isoformat() if issue.due_on else None,
        "synlig_for_kund": issue.visible_to_customer,
        "etiketter": [label.name for label in issue.labels.all()],
        "checklista": f"{done}/{total}",
        "loggad_min": issue.seconds // 60,
    }


def _issue_data(issue):
    """
    Formulärdata ur ett befintligt ärende, som grund för en partiell ändring.

    model_to_dict ger M2M-fält som modellobjekt, och ModelMultipleChoiceField
    kan inte validera dem (pk-uppslag med ett objekt ger TypeError). Därför
    skrivs etiketterna om till id:n här.
    """
    data = model_to_dict(issue)
    data["labels"] = [label.pk for label in issue.labels.all()]
    return data


# --- Läsoperationer ---------------------------------------------------------


def _lista_kunder(user):
    require_agency(user)
    rows = []
    for customer in Customer.objects.prefetch_related("projects"):
        rows.append(
            {
                "id": customer.pk,
                "namn": customer.name,
                "aktiv": customer.is_active,
                "projekt": [{"key": p.key, "namn": p.name} for p in customer.projects.all()],
                "oppna_arenden": Issue.objects.for_customer(customer).open().count(),
            }
        )
    return {"kunder": rows}


def _lista_projekt(user):
    require_agency(user)
    rows = []
    for project in Project.objects.select_related("customer").prefetch_related("columns"):
        rows.append(
            {
                "key": project.key,
                "namn": project.name,
                "kund": project.customer.name if project.customer_id else None,
                "status": project.status,
                "kolumner": [c.title for c in project.columns.all()],
                "oppna_arenden": project.issues.filter(closed_at__isnull=True).count(),
                "loggad_tid_h": round(project.total_seconds() / 3600, 1),
            }
        )
    return {"projekt": rows}


def _lista_arenden(user, projekt=None, kund=None, status="oppna", mina=False, sok=None):
    require_agency(user)
    qs = (
        Issue.objects.select_related(
            "project", "project__customer", "customer", "column", "assignee"
        )
        .prefetch_related("labels", "checklist")
        .with_logged_seconds()
    )
    if status == "oppna":
        qs = qs.open()
    elif status == "klara":
        qs = qs.filter(closed_at__isnull=False)
    elif status != "alla":
        raise OperationError("status ska vara oppna, klara eller alla.")
    if projekt:
        qs = qs.filter(project=find_project(projekt))
    if kund:
        qs = qs.for_customer(_customer(kund))
    if mina:
        qs = qs.filter(assignee=user)
    if sok:
        text = str(sok).strip()
        qs = qs.filter(Q(title__icontains=text) | Q(description__icontains=text))
    qs = qs.order_by("project__key", "column__position", "position", "id")

    # En rad extra avslöjar kapning utan en separat count-fråga.
    issues = with_time(qs[: MAX_ROWS + 1])
    truncated = len(issues) > MAX_ROWS
    rows = [_issue_row(i) for i in issues[:MAX_ROWS]]
    out = {"arenden": rows, "antal": len(rows)}
    if truncated:
        out["kapad"] = True
        out["not"] = (
            f"Listan är kapad till {MAX_ROWS} ärenden - avgränsa med projekt, kund, "
            f"status eller sok."
        )
    return out


def _hamta_arende(user, nyckel_eller_id):
    require_agency(user)
    issue = with_time([_issue(nyckel_eller_id)])[0]
    done, total = issue.checklist_progress()
    entries = list(issue.time_entries.select_related("user").order_by("-started_at")[:100])
    billable = sum(e.elapsed_seconds() for e in entries if e.is_billable)

    row = _issue_row(issue)
    row.update(
        {
            "beskrivning": issue.description,
            "typ": issue.get_issue_type_display(),
            "fakturerbart": issue.is_billable,
            "uppskattning_min": issue.estimate_minutes,
            "rapporterad_av": _who(issue.reporter),
            "skapad": when(issue.created_at),
            "uppdaterad": when(issue.updated_at),
            "stangd": when(issue.closed_at),
            "checklista": [
                {"id": c.pk, "text": c.text, "klar": c.is_done} for c in issue.checklist.all()
            ],
            "kommentarer": [
                {
                    "id": c.pk,
                    "vem": _who(c.author),
                    "nar": when(c.created_at),
                    "text": c.body,
                    "intern": c.is_internal,
                }
                for c in issue.comments.select_related("author")
            ],
            "tidsposter": [
                {
                    "id": e.pk,
                    "datum": timezone.localtime(e.started_at).date().isoformat(),
                    "minuter": e.elapsed_seconds() // 60,
                    "vem": _who(e.user),
                    "anteckning": e.note,
                    "fakturerbart": e.is_billable,
                    "pagar": e.is_running,
                }
                for e in entries
            ],
            "bilagor": [
                {"namn": a.original_name, "storlek": a.size_display}
                for a in issue.attachments.all()
            ],
            "aktivitet": [
                {"nar": when(a.created_at), "vem": a.who, "text": a.text}
                for a in issue.activity.select_related("user")[:30]
            ],
            "totaler": {
                "loggad_min": issue.seconds // 60,
                "fakturerbar_min": billable // 60,
                "antal_tidsposter": len(entries),
                "checklista": f"{done}/{total}",
            },
            "lank": _issue_url(issue),
        }
    )
    return row


def _tidrapport(user, fran=None, till=None, kund=None, projekt=None):
    require_agency(user)
    today = timezone.localdate()
    start = parse_date(fran, "fran") or today.replace(day=1)
    end = parse_date(till, "till") or today
    if end < start:
        raise OperationError("till ligger före fran.")

    # Bara avslutade poster - en pågående timer har ingen längd ännu.
    entries = TimeEntry.objects.filter(
        ended_at__isnull=False, started_at__date__gte=start, started_at__date__lte=end
    ).select_related("issue", "issue__project", "issue__project__customer", "issue__customer")
    if kund:
        customer = _customer(kund)
        entries = entries.filter(Q(issue__project__customer=customer) | Q(issue__customer=customer))
    if projekt:
        entries = entries.filter(issue__project=find_project(projekt))

    groups = {}
    for entry in entries:
        customer = entry.issue.effective_customer
        name = customer.name if customer else "Internt"
        group = groups.setdefault(name, {"s": 0, "b": 0, "arenden": {}})
        group["s"] += entry.seconds
        group["b"] += entry.seconds if entry.is_billable else 0
        row = group["arenden"].setdefault(
            entry.issue_id, {"issue": entry.issue, "s": 0, "b": 0, "n": 0}
        )
        row["s"] += entry.seconds
        row["b"] += entry.seconds if entry.is_billable else 0
        row["n"] += 1

    # Sekunder summeras och görs om till minuter sist, annars driver
    # avrundningen iväg över många korta poster.
    kunder = []
    for name, group in sorted(groups.items()):
        kunder.append(
            {
                "kund": name,
                "minuter": group["s"] // 60,
                "fakturerbara_minuter": group["b"] // 60,
                "arenden": [
                    {
                        "nyckel": r["issue"].key,
                        "rubrik": r["issue"].title,
                        "projekt": r["issue"].project.key if r["issue"].project_id else None,
                        "minuter": r["s"] // 60,
                        "fakturerbara_minuter": r["b"] // 60,
                        "poster": r["n"],
                    }
                    for r in group["arenden"].values()
                ],
            }
        )
    return {
        "fran": start.isoformat(),
        "till": end.isoformat(),
        "kunder": kunder,
        "totalt_minuter": sum(g["s"] for g in groups.values()) // 60,
        "totalt_fakturerbara_minuter": sum(g["b"] for g in groups.values()) // 60,
    }


# --- Direktoperationer ------------------------------------------------------


def _skapa_arende(
    user,
    rubrik,
    projekt=None,
    kund=None,
    beskrivning="",
    prioritet="normal",
    forfaller=None,
    etiketter=None,
    ansvarig=None,
    synlig_for_kund=False,
    kolumn=None,
):
    require_agency(user)
    project = find_project(projekt) if projekt else None
    customer = _customer(kund) if kund else None
    if project is None and customer is None:
        raise OperationError(
            "Ange projekt (nyckel) eller kund (id/namn). Använd lista_projekt och lista_kunder."
        )
    if (
        project is not None
        and customer is not None
        and project.customer_id
        and project.customer_id != customer.pk
    ):
        raise OperationError(
            f"Projektet {project.key} tillhör {project.customer.name}, inte {customer.name}. "
            f"Ange bara projekt."
        )
    column = None
    if kolumn:
        if project is None:
            raise OperationError("kolumn kräver ett projekt.")
        column = _column(project, kolumn)

    title = clean_text(rubrik, "rubrik", 200)
    if not title:
        raise OperationError("rubrik får inte vara tom.")
    data = {
        "title": title,
        "description": clean_text(beskrivning, "beskrivning", 20000, multiline=True),
        "project": project.pk if project else None,
        "customer": customer.pk if customer else None,
        "column": column.pk if column else None,
        "issue_type": IssueType.TASK,
        "priority": _priority(prioritet),
        "assignee": _staff(ansvarig).pk if ansvarig else None,
        "labels": [label.pk for label in _labels(etiketter or [])],
        "estimate_minutes": None,
        "due_on": parse_date(forfaller, "forfaller"),
        "is_billable": True,
        # Aldrig sant av misstag: ett internt ärende ska inte läcka till
        # portalen för att modellen råkade sätta en flagga.
        "visible_to_customer": bool(synlig_for_kund),
    }
    form = _form(IssueForm, Issue(), data)
    issue = form.save(commit=False)
    issue.reporter = user
    issue.save()
    form.save_m2m()
    issue.log(user, "skapade ärendet via assistenten")
    return {
        "status": "skapat",
        "id": issue.pk,
        "nyckel": issue.key,
        "projekt": issue.project.key if issue.project_id else None,
        "kolumn": issue.column.title if issue.column_id else None,
        "synlig_for_kund": issue.visible_to_customer,
        "lank": _issue_url(issue),
    }


def _uppdatera_arende(
    user,
    nyckel_eller_id,
    rubrik=None,
    beskrivning=None,
    prioritet=None,
    forfaller=None,
    ansvarig=None,
    etiketter=None,
    synlig_for_kund=None,
    uppskattning_min=None,
):
    require_agency(user)
    issue = _issue(nyckel_eller_id)

    # Bara skickade fält rör vi. None betyder "oförändrat", inte "töm".
    changed = {}
    if rubrik is not None:
        changed["title"] = clean_text(rubrik, "rubrik", 200)
        if not changed["title"]:
            raise OperationError("rubrik får inte vara tom.")
    if beskrivning is not None:
        changed["description"] = clean_text(beskrivning, "beskrivning", 20000, multiline=True)
    if prioritet is not None:
        changed["priority"] = _priority(prioritet)
    if forfaller is not None:
        changed["due_on"] = parse_date(forfaller, "forfaller")  # "" tar bort datumet
    if ansvarig is not None:
        changed["assignee"] = _staff(ansvarig).pk if str(ansvarig).strip() else None
    if etiketter is not None:
        changed["labels"] = [label.pk for label in _labels(etiketter)]
    if synlig_for_kund is not None:
        changed["visible_to_customer"] = bool(synlig_for_kund)
    if uppskattning_min is not None:
        try:
            minutes = int(uppskattning_min)
        except (TypeError, ValueError):
            raise OperationError("uppskattning_min ska vara ett heltal.") from None
        if minutes < 0:
            raise OperationError("uppskattning_min kan inte vara negativ.")
        changed["estimate_minutes"] = minutes or None
    if not changed:
        raise OperationError("Inget att ändra - ange minst ett fält.")

    before = {
        "priority": issue.priority,
        "assignee": issue.assignee_id,
        "due_on": issue.due_on,
        "visible_to_customer": issue.visible_to_customer,
    }
    data = _issue_data(issue)
    data.update(changed)
    _form(IssueForm, issue, data).save()

    # Loggar bara det som betyder något för en människa, och bara om det
    # faktiskt blev annorlunda.
    issue = _issue(issue.pk)
    for field, describe in _FIELD_LOG.items():
        if field not in changed:
            continue
        now = issue.assignee_id if field == "assignee" else getattr(issue, field)
        if now != before[field]:
            issue.log(user, describe(issue))
    return {
        "status": "uppdaterat",
        "nyckel": issue.key,
        "andrade_falt": sorted(changed),
        "synlig_for_kund": issue.visible_to_customer,
        "lank": _issue_url(issue),
    }


def _flytta_arende(user, nyckel_eller_id, kolumn=None, steg=None):
    require_agency(user)
    issue = _issue(nyckel_eller_id)
    if not kolumn and not steg:
        raise OperationError(
            "Ange kolumn (kolumntitel i projektet) eller steg (new, active, done)."
        )
    column = None
    if kolumn:
        if not issue.project_id:
            raise OperationError(
                f"{issue.key} har inget projekt och därmed inga kolumner - använd steg."
            )
        column = _column(issue.project, kolumn)
    elif steg not in dict(STAGES):
        raise OperationError("steg ska vara new, active eller done.")

    was_column, was_closed = issue.column_id, issue.is_closed
    try:
        move_issue(issue, column=column, stage=steg)
    except ValueError as exc:
        raise OperationError(str(exc)) from exc
    if issue.column_id != was_column or issue.is_closed != was_closed:
        where = issue.column.title if issue.column_id else dict(STAGES).get(steg, "")
        issue.log(user, f"flyttade till {where}")
    return {
        "status": "flyttat",
        "nyckel": issue.key,
        "kolumn": issue.column.title if issue.column_id else None,
        "steg": issue.stage,
        "stangt": issue.is_closed,
        "lank": _issue_url(issue),
    }


def _kommentera_arende(user, nyckel_eller_id, text, intern=True):
    require_agency(user)
    issue = _issue(nyckel_eller_id)
    body = clean_text(text, "text", 20000, multiline=True)
    if not body:
        raise OperationError("Kommentaren är tom.")
    comment = Comment.objects.create(issue=issue, author=user, body=body, is_internal=bool(intern))
    issue.log(user, "skrev en intern anteckning" if comment.is_internal else "svarade i portalen")
    return {
        "status": "kommenterat",
        "kommentar_id": comment.pk,
        "nyckel": issue.key,
        "intern": comment.is_internal,
        "syns_i_portalen": (not comment.is_internal) and issue.visible_to_customer,
        "not": "Inget mejl har skickats till kunden - det gör Giovanni manuellt från tavlan.",
        "lank": _issue_url(issue),
    }


def _logga_tid(user, nyckel_eller_id, minuter, datum=None, anteckning="", fakturerbart=None):
    require_agency(user)
    issue = _issue(nyckel_eller_id)
    try:
        minutes = int(minuter)
    except (TypeError, ValueError):
        raise OperationError("minuter ska vara ett heltal.") from None
    if minutes <= 0:
        raise OperationError("minuter måste vara större än noll.")
    if minutes > 24 * 60:
        raise OperationError("Högst 24 timmar per tidspost - dela upp den.")
    today = timezone.localdate()
    on_date = parse_date(datum, "datum") or today
    if on_date > today:
        raise OperationError("datum får inte ligga i framtiden - tid loggas i efterhand.")

    entry = TimeEntry.log(
        issue,
        user,
        minutes,
        on_date=on_date,
        note=clean_text(anteckning, "anteckning", 300),
        is_billable=None if fakturerbart is None else bool(fakturerbart),
    )
    issue.log(user, f"loggade {minutes} min")
    return {
        "status": "loggat",
        "tidspost_id": entry.pk,
        "nyckel": issue.key,
        "minuter": minutes,
        "datum": on_date.isoformat(),
        "fakturerbart": entry.is_billable,
        "totalt_min_pa_arendet": issue.total_seconds() // 60,
        "lank": _issue_url(issue),
    }


def _lagg_till_checklista(user, nyckel_eller_id, punkter):
    require_agency(user)
    issue = _issue(nyckel_eller_id)
    if not isinstance(punkter, list) or not punkter:
        raise OperationError("punkter ska vara en lista med minst en text.")
    texts = [clean_text(p, "punkt", 200) for p in punkter]
    if any(not t for t in texts):
        raise OperationError("Tomma punkter tillåts inte.")
    last = issue.checklist.order_by("-position").first()
    position = (last.position + 1) if last else 0
    created = [
        ChecklistItem.objects.create(issue=issue, text=text, position=position + offset)
        for offset, text in enumerate(texts)
    ]
    done, total = issue.checklist_progress()
    return {
        "status": "tillagt",
        "nyckel": issue.key,
        "punkter": [{"id": c.pk, "text": c.text} for c in created],
        "checklista": f"{done}/{total}",
        "lank": _issue_url(issue),
    }


def _bocka_checklista(user, punkt_id, klar=True):
    require_agency(user)
    item = (
        ChecklistItem.objects.select_related("issue", "issue__project").filter(pk=punkt_id).first()
    )
    if item is None:
        raise OperationError(f"Okänt delmoment: {punkt_id}. Använd hamta_arende för id:n.")
    item.is_done = bool(klar)
    item.save(update_fields=["is_done"])
    done, total = item.issue.checklist_progress()
    return {
        "status": "bockad" if item.is_done else "avbockad",
        "punkt_id": item.pk,
        "nyckel": item.issue.key,
        "checklista": f"{done}/{total}",
        "lank": _issue_url(item.issue),
    }


def _skapa_projekt(
    user,
    namn,
    kund=None,
    key=None,
    beskrivning="",
    timpris=None,
    budget_timmar=None,
    deadline=None,
):
    require_agency(user)
    customer = _customer(kund) if kund else None
    name = clean_text(namn, "namn", 200)
    if not name:
        raise OperationError("namn får inte vara tomt.")
    data = {
        "name": name,
        "key": clean_text(key, "key", 10).upper(),  # tomt = ProjectForm gör en ur namnet
        "customer": customer.pk if customer else None,
        "description": clean_text(beskrivning, "beskrivning", 5000, multiline=True),
        "status": ProjectStatus.ACTIVE,
        "hourly_rate": timpris,
        "budget_hours": budget_timmar,
        "starts_on": None,
        "due_on": parse_date(deadline, "deadline"),
    }
    form = _form(ProjectForm, Project(), data)
    project = form.save(commit=False)
    project.created_by = user
    project.save()
    return {
        "status": "skapat",
        "key": project.key,
        "namn": project.name,
        "kund": project.customer.name if project.customer_id else None,
        "kolumner": [c.title for c in project.columns.all()],
        "lank": f"{base_url()}/manage/projekt/{project.key}/",
    }


def _skapa_kund(user, namn, epost="", telefon="", org_nummer="", webbplats=""):
    require_agency(user)
    name = clean_text(namn, "namn", 200)
    if not name:
        raise OperationError("namn får inte vara tomt.")
    existing = Customer.objects.filter(name__iexact=name).first()
    if existing is not None:
        raise OperationError(f"Kunden {existing.name} finns redan (id {existing.pk}).")
    data = {
        "name": name,
        "org_number": clean_text(org_nummer, "org_nummer", 20),
        "email": str(epost or "").strip(),
        "phone": clean_text(telefon, "telefon", 40),
        "website": str(webbplats or "").strip(),
        "notes": "",
        "is_active": True,
    }
    customer = _form(CustomerForm, Customer(), data).save()
    return {
        "status": "skapat",
        "id": customer.pk,
        "namn": customer.name,
        "lank": f"{base_url()}/manage/kunder/{customer.pk}/",
    }


# --- Registrering -----------------------------------------------------------

_S = {"type": "string"}
_B = {"type": "boolean"}
_I = {"type": "integer"}
_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


def _schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_REF = {
    "type": "string",
    "description": "Ärendets nyckel (NORD-3) eller id (123). Tål gemener och #.",
}

register(
    Operation(
        name="lista_kunder",
        description=(
            "Lista byråns kunder med deras projekt och antal öppna ärenden. "
            "Kund -> Projekt -> Ärende är hierarkin; börja här."
        ),
        input_schema=_EMPTY,
        risk=Risk.READ,
        read=_lista_kunder,
    )
)
register(
    Operation(
        name="lista_projekt",
        description=(
            "Lista alla projekt med nyckel (t.ex. NORD), kund, status, kolumnerna på "
            "projektets tavla, öppna ärenden och loggad tid i timmar."
        ),
        input_schema=_EMPTY,
        risk=Risk.READ,
        read=_lista_projekt,
    )
)
register(
    Operation(
        name="lista_arenden",
        description=(
            "Lista ärenden, som standard bara öppna. Filtrera på projekt (nyckel), "
            "kund (id eller namn), status (oppna, klara, alla), mina (ansvarig = du) "
            f"och sok (fritext i rubrik/beskrivning). Högst {MAX_ROWS} rader; svaret "
            "säger om listan kapades."
        ),
        input_schema=_schema(
            {
                "projekt": {**_S, "description": "Projektnyckel, t.ex. NORD."},
                "kund": {**_S, "description": "Kundens id eller namn."},
                "status": {"type": "string", "enum": ["oppna", "klara", "alla"]},
                "mina": _B,
                "sok": _S,
            }
        ),
        risk=Risk.READ,
        read=_lista_arenden,
    )
)
register(
    Operation(
        name="hamta_arende",
        description=(
            "Hämta ett ärende i sin helhet: beskrivning, checklista (med id:n för "
            "bocka_checklista), kommentarer, tidsposter, bilagor, de 30 senaste "
            "aktivitetsraderna och totaler. Läs alltid innan du ändrar."
        ),
        input_schema=_schema({"nyckel_eller_id": _REF}, ["nyckel_eller_id"]),
        risk=Risk.READ,
        read=_hamta_arende,
    )
)
register(
    Operation(
        name="tidrapport",
        description=(
            "Loggad tid per kund och ärende: minuter, fakturerbara minuter och antal "
            "poster, plus totaler. fran/till är ISO-datum; utan dem gäller innevarande "
            "månad. Filtrera på kund (id/namn) och projekt (nyckel)."
        ),
        input_schema=_schema(
            {
                "fran": {**_S, "description": "YYYY-MM-DD"},
                "till": {**_S, "description": "YYYY-MM-DD"},
                "kund": _S,
                "projekt": _S,
            }
        ),
        risk=Risk.READ,
        read=_tidrapport,
    )
)
register(
    Operation(
        name="skapa_arende",
        description=(
            "Skapa ett ärende DIREKT på tavlan (inget utkast). Ange projekt (nyckel) "
            "eller kund (id/namn) - inte påhittade; läs lista_projekt/lista_kunder "
            "först. Etiketter måste finnas sedan tidigare. Ärendet är osynligt för "
            "kunden om du inte uttryckligen sätter synlig_for_kund. Svaret ger nyckel "
            "och länk till tavlan."
        ),
        input_schema=_schema(
            {
                "rubrik": _S,
                "projekt": {**_S, "description": "Projektnyckel, t.ex. NORD."},
                "kund": {**_S, "description": "Kundens id eller namn - bara om inget projekt."},
                "beskrivning": _S,
                "prioritet": {"type": "string", "enum": ["lag", "normal", "hog", "akut"]},
                "forfaller": {**_S, "description": "YYYY-MM-DD"},
                "etiketter": {"type": "array", "items": _S},
                "ansvarig": {**_S, "description": "Användarnamn eller förnamn på byråns personal."},
                "synlig_for_kund": _B,
                "kolumn": {**_S, "description": "Kolumntitel i projektet; annars första kolumnen."},
            },
            ["rubrik"],
        ),
        risk=Risk.ACTION,
        run=_skapa_arende,
    )
)
register(
    Operation(
        name="uppdatera_arende",
        description=(
            "Ändra fält på ett ärende DIREKT. Bara fält du skickar ändras. forfaller "
            "eller ansvarig som tom sträng tar bort värdet; etiketter ersätter hela "
            "listan. synlig_for_kund ändras bara om du skickar den."
        ),
        input_schema=_schema(
            {
                "nyckel_eller_id": _REF,
                "rubrik": _S,
                "beskrivning": _S,
                "prioritet": {"type": "string", "enum": ["lag", "normal", "hog", "akut"]},
                "forfaller": {**_S, "description": "YYYY-MM-DD, eller tom sträng för att ta bort."},
                "ansvarig": {**_S, "description": "Användarnamn/förnamn, eller tom sträng."},
                "etiketter": {"type": "array", "items": _S},
                "synlig_for_kund": _B,
                "uppskattning_min": _I,
            },
            ["nyckel_eller_id"],
        ),
        risk=Risk.ACTION,
        run=_uppdatera_arende,
    )
)
register(
    Operation(
        name="flytta_arende",
        description=(
            "Flytta ett ärende till en kolumn i projektet (kolumn = titel, se "
            "lista_projekt) eller till ett steg: new, active eller done. Steget done "
            "stänger ärendet. Kunden mejlas inte."
        ),
        input_schema=_schema(
            {
                "nyckel_eller_id": _REF,
                "kolumn": _S,
                "steg": {"type": "string", "enum": ["new", "active", "done"]},
            },
            ["nyckel_eller_id"],
        ),
        risk=Risk.ACTION,
        run=_flytta_arende,
    )
)
register(
    Operation(
        name="kommentera_arende",
        description=(
            "Lägg en kommentar på ett ärende. Intern som standard (syns aldrig i "
            "portalen). intern=false gör den kundsynlig om ärendet är det - men "
            "skickar ALDRIG mejl; det gör Giovanni själv från tavlan."
        ),
        input_schema=_schema(
            {"nyckel_eller_id": _REF, "text": _S, "intern": _B},
            ["nyckel_eller_id", "text"],
        ),
        risk=Risk.ACTION,
        run=_kommentera_arende,
    )
)
register(
    Operation(
        name="logga_tid",
        description=(
            "Logga tid i efterhand på ett ärende: hela minuter, valfritt datum "
            "(YYYY-MM-DD, standard i dag, aldrig i framtiden), anteckning och "
            "fakturerbart (standard: ärendets inställning). Det finns ingen timer att "
            "starta eller stoppa härifrån."
        ),
        input_schema=_schema(
            {
                "nyckel_eller_id": _REF,
                "minuter": _I,
                "datum": {**_S, "description": "YYYY-MM-DD"},
                "anteckning": _S,
                "fakturerbart": _B,
            },
            ["nyckel_eller_id", "minuter"],
        ),
        risk=Risk.ACTION,
        run=_logga_tid,
    )
)
register(
    Operation(
        name="lagg_till_checklista",
        description="Lägg till delmoment (punkter) sist i ärendets checklista.",
        input_schema=_schema(
            {"nyckel_eller_id": _REF, "punkter": {"type": "array", "items": _S}},
            ["nyckel_eller_id", "punkter"],
        ),
        risk=Risk.ACTION,
        run=_lagg_till_checklista,
    )
)
register(
    Operation(
        name="bocka_checklista",
        description=("Bocka av (eller ångra) ett delmoment. punkt_id får du från hamta_arende."),
        input_schema=_schema({"punkt_id": _I, "klar": _B}, ["punkt_id"]),
        risk=Risk.ACTION,
        run=_bocka_checklista,
    )
)
register(
    Operation(
        name="skapa_projekt",
        description=(
            "Skapa ett projekt DIREKT, med standardkolumnerna Att göra / Pågår / "
            "Klart. key (t.ex. NORD, max 10 tecken) skapas ur namnet om den utelämnas. "
            "kund är id eller namn på en befintlig kund."
        ),
        input_schema=_schema(
            {
                "namn": _S,
                "kund": _S,
                "key": _S,
                "beskrivning": _S,
                "timpris": {**_I, "description": "Kronor per timme exkl. moms."},
                "budget_timmar": _I,
                "deadline": {**_S, "description": "YYYY-MM-DD"},
            },
            ["namn"],
        ),
        risk=Risk.ACTION,
        run=_skapa_projekt,
    )
)
register(
    Operation(
        name="skapa_kund",
        description=(
            "Skapa en kund DIREKT. Fel om namnet redan finns - använd lista_kunder "
            "först. Skapar ingen portalinloggning och skickar inget mejl."
        ),
        input_schema=_schema(
            {"namn": _S, "epost": _S, "telefon": _S, "org_nummer": _S, "webbplats": _S},
            ["namn"],
        ),
        risk=Risk.ACTION,
        run=_skapa_kund,
    )
)
