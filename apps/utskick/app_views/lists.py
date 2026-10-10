"""
Listor och taggar (README I.1 och I.7): översikten och en lista.

    list_index    listorna med antal, taggarna med antal; ny lista, ny tagg,
                  byt namn på och ta bort en tagg
    list_detail   en lista: namn och beskrivning, kontakterna i den (50 per
                  sida), ta bort en kontakt ur listan, byt namn, ta bort listan

Listor fyller kunden själv. Segmenten (S4, regler som räknas om varje gång
de används) listas här med sin senaste räkning och byggs i
app_views/segments.py. Att ta bort en lista eller en tagg
tar aldrig bort kontakterna. POST går genom en ordbok med handlers
(onboarding._BUSINESS_ACTIONS), och varje id ur formuläret genom
access.owned_ids (400 för ett främmande).
"""

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from .. import contacts as register
from ..access import owned, owned_ids, utskick_view
from ..models import Contact, ContactList, Segment, Tag
from . import render_contacts
from .contacts import (
    PER_PAGE,
    Refused,
    clean_name,
    count_text,
    date_text,
    decorate,
    tag_target,
    with_rows,
)

LIST_EXISTS = "Det finns redan en lista med det namnet."
TAG_EXISTS = "Det finns redan en tagg med det namnet."


def _lists_url(anchor=""):
    return reverse("flamingo:app_lists") + (f"#{anchor}" if anchor else "")


def _unique_save(row, fields, exists_text):
    try:
        with transaction.atomic():
            if fields:
                row.save(update_fields=fields)
            else:
                row.save()
    except IntegrityError:
        raise Refused(exists_text) from None


def _list_new(request, account):
    name = clean_name(request.POST.get("namn"), 80)
    if not name:
        raise Refused("Skriv ett namn på listan.")
    row = ContactList(
        account=account,
        name=name,
        description=clean_name(request.POST.get("beskrivning"), 200),
        created_by=request.user if request.user.is_authenticated else None,
    )
    _unique_save(row, None, LIST_EXISTS)
    messages.success(request, f"Listan {row.name} är skapad. Lägg kontakter i den från Kontakter.")
    return redirect("flamingo:app_list", pk=row.pk)


def _tag_new(request, account):
    name = clean_name(request.POST.get("namn"), 40)
    if not name:
        raise Refused("Skriv ett namn på taggen.")
    row = Tag(account=account, name=name)
    _unique_save(row, None, TAG_EXISTS)
    messages.success(request, f"Taggen {row.name} är skapad.")
    return redirect(_lists_url("taggar"))


def _tag_rename(request, account):
    tag = tag_target(request, account, allow_new=False)
    name = clean_name(request.POST.get("namn"), 40)
    if not name:
        raise Refused("Skriv ett namn på taggen.")
    tag.name = name
    _unique_save(tag, ["name"], TAG_EXISTS)
    messages.success(request, f"Taggen heter nu {tag.name}.")
    return redirect(_lists_url("taggar"))


def _tag_delete(request, account):
    tag = tag_target(request, account, allow_new=False)
    name = tag.name
    tag.delete()
    messages.success(request, f"Taggen {name} är borttagen. Kontakterna finns kvar.")
    return redirect(_lists_url("taggar"))


_INDEX_ACTIONS = {
    "list_new": _list_new,
    "tag_new": _tag_new,
    "tag_rename": _tag_rename,
    "tag_delete": _tag_delete,
}


@utskick_view
def list_index(request, account):
    if request.method == "POST":
        handler = _INDEX_ACTIONS.get(request.POST.get("action", ""))
        if handler is None:
            return redirect("flamingo:app_lists")
        try:
            return handler(request, account)
        except Refused as exc:
            messages.error(request, exc.message)
            anchor = "taggar" if request.POST.get("action", "").startswith("tag") else "ny-lista"
            return redirect(_lists_url(anchor))
    lists = (
        ContactList.objects.filter(account=account)
        .annotate(size=Count("memberships"))
        .order_by("name")
    )
    tags = Tag.objects.filter(account=account).annotate(size=Count("contacts")).order_by("name")
    # ?ny=1 (knappen Ny lista i sidhuvudet) öppnar formuläret direkt.
    context = {"lists": lists, "tags": tags, "ny_lista": request.GET.get("ny") == "1"}
    # --- S4 (segment-byggaren): segmenten med senaste räkningen ---
    context["segments"] = list(Segment.objects.filter(account=account).order_by("name", "pk"))
    context["segments_full"] = len(context["segments"]) >= Segment.MAX_PER_ACCOUNT
    # --- slut S4
    return render_contacts(request, "flamingo/app/kontakter/lists.html", "lists", context)


def _list_rename(request, account, target):
    name = clean_name(request.POST.get("namn"), 80)
    if not name:
        raise Refused("Skriv ett namn på listan.")
    target.name = name
    target.description = clean_name(request.POST.get("beskrivning"), 200)
    _unique_save(target, ["name", "description"], LIST_EXISTS)
    messages.success(request, "Listan är sparad.")
    return redirect("flamingo:app_list", pk=target.pk)


def _list_delete(request, account, target):
    name = target.name
    target.delete()
    messages.success(request, f"Listan {name} är borttagen. Kontakterna finns kvar i Kontakter.")
    return redirect("flamingo:app_lists")


def _list_remove(request, account, target):
    ids = owned_ids(Contact, account, request.POST.getlist("ids"), limit=PER_PAGE)
    if not ids:
        raise Refused("Välj en kontakt.")
    removed = register.remove_from_list(target, ids)
    messages.success(request, f"{count_text(removed, 'kontakt', 'kontakter')} togs bort ur listan.")
    return redirect(reverse("flamingo:app_list", args=[target.pk]) + "#kontakter")


_LIST_ACTIONS = {
    "rename": _list_rename,
    "delete": _list_delete,
    "remove": _list_remove,
}


@utskick_view
def list_detail(request, account, pk):
    target = owned(ContactList, account, pk)
    if request.method == "POST":
        handler = _LIST_ACTIONS.get(request.POST.get("action", ""))
        if handler is None:
            return redirect("flamingo:app_list", pk=target.pk)
        try:
            return handler(request, account, target)
        except Refused as exc:
            messages.error(request, exc.message)
            return redirect("flamingo:app_list", pk=target.pk)
    members = with_rows(
        Contact.objects.filter(account=account, memberships__list=target).order_by(
            "-memberships__added_at", "-pk"
        )
    )
    page = Paginator(members, PER_PAGE).get_page(request.GET.get("sida"))
    rows = decorate(list(page.object_list), account, timezone.now())
    context = {
        "lista": target,
        "kontakter": rows,
        "page": page,
        "created_text": date_text(target.created_at),
        "contacts_url": f"{reverse('flamingo:app_contacts')}?lista={target.pk}",
    }
    return render_contacts(request, "flamingo/app/kontakter/list_detail.html", "lists", context)
