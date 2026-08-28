"""
/manage/import/ — Bulk JSON import for superusers.

Supports importing service categories, services (with steps), and FAQ sections
(with items) from a single JSON payload. Additive only — never deletes or
overwrites existing records.
"""
# ruff: noqa: E501

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_http_methods

from apps.faq.models import FAQItem, FAQSection
from apps.services.models import Service, ServiceCategory, ServiceStep
from apps.website.models import SiteSettings


def _is_superuser(user):
    return user.is_superuser


def _ctx(**extra):
    ctx = {"site_settings": SiteSettings.load(), "active": "superuser"}
    ctx.update(extra)
    return ctx


@login_required
@user_passes_test(_is_superuser)
@require_http_methods(["GET", "POST"])
def import_view(request):
    """Show the import form, validate JSON, show preview or execute import."""
    preview = None
    raw_json = ""
    errors = []

    if request.method == "POST":
        action = request.POST.get("action", "preview")
        raw_json = request.POST.get("json_data", "").strip()

        if not raw_json:
            errors.append("Ingen JSON angiven.")
        else:
            try:
                data = json.loads(raw_json)
            except json.JSONDecodeError as e:
                errors.append(f"Ogiltig JSON: {e}")
                data = None

            if data and not isinstance(data, dict):
                errors.append(
                    "JSON måste vara ett objekt (dict) med nycklar som 'categories', 'services', 'faq_sections'."
                )
                data = None

            if data:
                preview = _build_preview(data)

                if action == "confirm" and not preview["errors"]:
                    result = _execute_import(data, preview)
                    messages.success(request, result)
                    return redirect("manage:import_view")

    return render(
        request,
        "manage/import/import.html",
        _ctx(
            raw_json=raw_json,
            preview=preview,
            errors=errors,
            json_format=_JSON_FORMAT_HELP,
        ),
    )


def _build_preview(data):
    """Analyse what would be created/skipped without writing to DB."""
    preview = {
        "categories": [],
        "services": [],
        "faq_sections": [],
        "errors": [],
        "totals": {"create": 0, "skip": 0},
    }

    # Categories
    for cat in data.get("categories", []):
        name = (cat.get("name") or "").strip()
        if not name:
            preview["errors"].append("Kategori saknar 'name'.")
            continue
        exists = ServiceCategory.objects.filter(name=name).exists()
        preview["categories"].append({"name": name, "exists": exists})
        if exists:
            preview["totals"]["skip"] += 1
        else:
            preview["totals"]["create"] += 1

    # Services
    for svc in data.get("services", []):
        name = (svc.get("name") or "").strip()
        if not name:
            preview["errors"].append("Tjänst saknar 'name'.")
            continue
        exists = Service.objects.filter(name=name).exists()
        steps_count = len(svc.get("steps", []))
        cat_name = (svc.get("category") or "").strip()
        preview["services"].append(
            {
                "name": name,
                "category": cat_name,
                "steps": steps_count,
                "exists": exists,
            }
        )
        if exists:
            preview["totals"]["skip"] += 1
        else:
            preview["totals"]["create"] += 1

    # FAQ sections
    for sec in data.get("faq_sections", []):
        title = (sec.get("title") or "").strip()
        if not title:
            preview["errors"].append("FAQ-sektion saknar 'title'.")
            continue
        exists = FAQSection.objects.filter(title=title).exists()
        items_count = len(sec.get("items", []))
        preview["faq_sections"].append(
            {
                "title": title,
                "items": items_count,
                "exists": exists,
            }
        )
        if exists:
            preview["totals"]["skip"] += 1
        else:
            preview["totals"]["create"] += 1

    return preview


def _execute_import(data, preview):
    """Actually create records in DB. Returns a summary string."""
    created_cats = 0
    created_svcs = 0
    created_steps = 0
    created_faq_secs = 0
    created_faq_items = 0

    # Categories first (services may reference them by name)
    for cat in data.get("categories", []):
        name = (cat.get("name") or "").strip()
        if not name or ServiceCategory.objects.filter(name=name).exists():
            continue
        ServiceCategory.objects.create(
            name=name,
            slug=_unique_slug(name, ServiceCategory, "slug"),
            description=(cat.get("description") or "").strip(),
            is_active=True,
        )
        created_cats += 1

    # Services
    for svc in data.get("services", []):
        name = (svc.get("name") or "").strip()
        if not name or Service.objects.filter(name=name).exists():
            continue

        cat_name = (svc.get("category") or "").strip()
        category = None
        if cat_name:
            category = ServiceCategory.objects.filter(name=cat_name).first()

        service = Service.objects.create(
            name=name,
            slug=_unique_slug(name, Service, "slug"),
            category=category,
            description=(svc.get("description") or "").strip()[:200],
            body=svc.get("body") or "",
            is_active=True,
        )
        created_svcs += 1

        for order, step in enumerate(svc.get("steps", [])):
            title = (step.get("title") or "").strip()
            if not title:
                continue
            ServiceStep.objects.create(
                service=service,
                title=title[:200],
                description=(step.get("description") or "").strip()[:300],
                order=order,
            )
            created_steps += 1

    # FAQ sections
    for sec in data.get("faq_sections", []):
        title = (sec.get("title") or "").strip()
        if not title or FAQSection.objects.filter(title=title).exists():
            continue

        section = FAQSection.objects.create(
            title=title,
            slug=_unique_slug(title, FAQSection, "slug"),
            description=(sec.get("description") or "").strip(),
            is_active=True,
        )
        created_faq_secs += 1

        for order, item in enumerate(sec.get("items", [])):
            q = (item.get("question") or "").strip()
            a = (item.get("answer") or "").strip()
            if not q:
                continue
            FAQItem.objects.create(
                section=section,
                question=q,
                answer=a,
                order=order,
                is_active=True,
            )
            created_faq_items += 1

    parts = []
    if created_cats:
        parts.append(f"{created_cats} kategorier")
    if created_svcs:
        parts.append(f"{created_svcs} tjänster ({created_steps} steg)")
    if created_faq_secs:
        parts.append(f"{created_faq_secs} FAQ-sektioner ({created_faq_items} frågor)")
    return f"Import klar! Skapade: {', '.join(parts)}." if parts else "Inget nytt att importera."


def _unique_slug(name, model, field):
    """Generate a unique slug for a model."""
    base = slugify(name)
    slug = base
    i = 2
    while model.objects.filter(**{field: slug}).exists():
        slug = f"{base}-{i}"
        i += 1
    return slug


_JSON_FORMAT_HELP = """{
  "categories": [
    { "name": "Kategorinamn", "description": "Valfri beskrivning" }
  ],
  "services": [
    {
      "name": "Tjänstenamn",
      "category": "Kategorinamn",
      "description": "Kort beskrivning (max 200 tecken)",
      "body": "<p>Brödtext (HTML)</p>",
      "steps": [
        { "title": "Steg 1", "description": "Beskrivning av steget" }
      ]
    }
  ],
  "faq_sections": [
    {
      "title": "Sektionens titel",
      "description": "Intro-text",
      "items": [
        { "question": "Frågan?", "answer": "Svaret (kan vara HTML)" }
      ]
    }
  ]
}"""
