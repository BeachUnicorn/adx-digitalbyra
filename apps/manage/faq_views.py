"""
/manage/faq/ - CRUD for FAQ sections and their items.

Flow:
  faq_overview    /manage/faq/                  - all sections
  faq_detail      /manage/faq/<slug>/           - one section's items (add/edit/delete/move)
  faq_section_new /manage/faq/new/              - create a section
  faq_section_edit /manage/faq/<slug>/edit/     - edit section meta
  faq_section_delete /manage/faq/<slug>/delete/ - delete section
  faq_item_new    /manage/faq/<slug>/items/new/ - add item
  faq_item_edit   /manage/faq/items/<pk>/       - edit item
  faq_item_delete /manage/faq/items/<pk>/delete/ - delete item
  faq_item_move   /manage/faq/items/<pk>/move/  - move item up/down
"""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.common.security import sanitize_plain_text, sanitize_rich_html_basic
from apps.faq.models import FAQItem, FAQSection
from apps.website.models import SiteSettings


def _ctx(**extra):
    ctx = {"site_settings": SiteSettings.load(), "active": "faq"}
    ctx.update(extra)
    return ctx


# --- Section forms ---


class FAQSectionForm(forms.ModelForm):
    class Meta:
        model = FAQSection
        fields = ["title", "slug", "description", "meta_description", "is_active", "order"]
        widgets = {
            "title": forms.TextInput(),
            "slug": forms.TextInput(attrs={"placeholder": "genereras automatiskt"}),
            "description": forms.Textarea(attrs={"rows": 3}),
            "meta_description": forms.TextInput(),
        }
        labels = {
            "title": "Titel",
            "slug": "Webbadress (slug)",
            "description": "Beskrivning",
            "meta_description": "Metabeskrivning (SEO)",
            "is_active": "Aktiv",
            "order": "Sortering",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["slug"].required = False

    def clean_title(self):
        return sanitize_plain_text(self.cleaned_data.get("title", ""), max_length=200)

    def clean_description(self):
        return sanitize_plain_text(self.cleaned_data.get("description", ""), max_length=500)

    def clean_meta_description(self):
        # Metafält är ren text - taggar blir bokstavliga tecken i <meta>.
        return sanitize_plain_text(self.cleaned_data.get("meta_description", ""), max_length=300)

    def clean_slug(self):
        slug = (self.cleaned_data.get("slug") or "").strip()
        title = self.cleaned_data.get("title") or ""
        base = slugify(slug) if slug else slugify(title)
        if not base:
            return ""
        candidate = base
        qs = FAQSection.objects.exclude(pk=self.instance.pk)
        i = 2
        while qs.filter(slug=candidate).exists():
            candidate = f"{base}-{i}"
            i += 1
        return candidate


class FAQItemForm(forms.ModelForm):
    class Meta:
        model = FAQItem
        fields = ["question", "answer", "is_active"]
        widgets = {
            "question": forms.TextInput(),
            "answer": forms.Textarea(attrs={"data-tiptap": "basic", "rows": 5}),
        }
        labels = {
            "question": "Fråga",
            "answer": "Svar",
            "is_active": "Aktiv",
        }

    def clean_question(self):
        return sanitize_plain_text(self.cleaned_data.get("question", ""), max_length=500)

    def clean_answer(self):
        return sanitize_rich_html_basic(self.cleaned_data.get("answer", ""))


# --- Views ---


@login_required
def faq_overview(request):
    sections = FAQSection.objects.all()
    return render(request, "manage/faq/overview.html", _ctx(sections=sections))


@login_required
def faq_section_new(request):
    if request.method == "POST":
        form = FAQSectionForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, _("FAQ-sektionen skapades."))
            return redirect("manage:faq_detail", slug=form.instance.slug)
    else:
        form = FAQSectionForm()
    return render(request, "manage/faq/section_form.html", _ctx(form=form, instance=None))


@login_required
def faq_section_edit(request, slug):
    section = get_object_or_404(FAQSection, slug=slug)
    if request.method == "POST":
        form = FAQSectionForm(request.POST, instance=section)
        if form.is_valid():
            form.save()
            messages.success(request, _("FAQ-sektionen sparades."))
            return redirect("manage:faq_detail", slug=section.slug)
    else:
        form = FAQSectionForm(instance=section)
    return render(request, "manage/faq/section_form.html", _ctx(form=form, instance=section))


@login_required
@require_POST
def faq_section_delete(request, slug):
    section = get_object_or_404(FAQSection, slug=slug)
    section.delete()
    messages.success(request, _("FAQ-sektionen togs bort."))
    return redirect("manage:faq_overview")


@login_required
def faq_detail(request, slug):
    section = get_object_or_404(FAQSection, slug=slug)
    items = section.items.all()
    return render(request, "manage/faq/detail.html", _ctx(section=section, items=items))


@login_required
def faq_item_new(request, slug):
    section = get_object_or_404(FAQSection, slug=slug)
    if request.method == "POST":
        form = FAQItemForm(request.POST)
        if form.is_valid():
            item = form.save(commit=False)
            item.section = section
            item.order = (section.items.aggregate(m=Max("order"))["m"] or 0) + 1
            item.save()
            messages.success(request, _("Frågan lades till."))
            return redirect("manage:faq_detail", slug=section.slug)
    else:
        form = FAQItemForm()
    return render(
        request, "manage/faq/item_form.html", _ctx(form=form, section=section, instance=None)
    )


@login_required
def faq_item_edit(request, pk):
    item = get_object_or_404(FAQItem.objects.select_related("section"), pk=pk)
    if request.method == "POST":
        form = FAQItemForm(request.POST, instance=item)
        if form.is_valid():
            form.save()
            messages.success(request, _("Frågan sparades."))
            return redirect("manage:faq_detail", slug=item.section.slug)
    else:
        form = FAQItemForm(instance=item)
    return render(
        request, "manage/faq/item_form.html", _ctx(form=form, section=item.section, instance=item)
    )


@login_required
@require_POST
def faq_item_delete(request, pk):
    item = get_object_or_404(FAQItem.objects.select_related("section"), pk=pk)
    slug = item.section.slug
    item.delete()
    messages.success(request, _("Frågan togs bort."))
    return redirect("manage:faq_detail", slug=slug)


@login_required
@require_POST
def faq_item_move(request, pk):
    item = get_object_or_404(FAQItem.objects.select_related("section"), pk=pk)
    direction = request.POST.get("direction")
    siblings = list(item.section.items.order_by("order", "id"))
    index = next((i for i, it in enumerate(siblings) if it.pk == item.pk), None)

    if index is not None:
        swap_with = None
        if direction == "up" and index > 0:
            swap_with = siblings[index - 1]
        elif direction == "down" and index < len(siblings) - 1:
            swap_with = siblings[index + 1]

        if swap_with is not None:
            item.order, swap_with.order = swap_with.order, item.order
            FAQItem.objects.bulk_update([item, swap_with], ["order"])

    return redirect("manage:faq_detail", slug=item.section.slug)
