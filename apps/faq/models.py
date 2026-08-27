"""
FAQ app models.

Structure: FAQSection → FAQItem (ordered).
A section is a named group of Q&A pairs that can be linked from:
  - A FAQ block on a block page (via block.data["faq_section_id"])
  - A Service (optional FK)
  - Standalone pages at /faq/ and /faq/<slug>/
"""

from django.db import models
from django.urls import reverse
from django.utils.html import strip_tags
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _


class FAQSection(models.Model):
    """A named group of FAQ items (e.g. 'Vattenburna system')."""

    title = models.CharField(_("Titel"), max_length=200)
    slug = models.SlugField(_("Slug"), max_length=200, unique=True, blank=True)
    description = models.TextField(
        _("Beskrivning"),
        blank=True,
        help_text=_("Valfri intro som visas på FAQ-sidan."),
    )
    meta_description = models.CharField(
        _("Metabeskrivning"),
        max_length=160,
        blank=True,
        help_text=_("SEO-beskrivning (max 160 tecken)."),
    )
    is_active = models.BooleanField(_("Aktiv"), default=True)
    order = models.PositiveIntegerField(_("Sortering"), default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "title"]
        verbose_name = _("FAQ-sektion")
        verbose_name_plural = _("FAQ-sektioner")

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)
        if not self.meta_description and self.description:
            self.meta_description = strip_tags(self.description)[:160]
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("faq:section_detail", kwargs={"slug": self.slug})

    def get_meta_description(self):
        if self.meta_description:
            return self.meta_description
        if self.description:
            return strip_tags(self.description)[:160]
        return f"Vanliga frågor om {self.title}"


class FAQItem(models.Model):
    """A single question + answer pair, belonging to one section."""

    section = models.ForeignKey(
        FAQSection,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("Sektion"),
    )
    question = models.CharField(_("Fråga"), max_length=500)
    answer = models.TextField(
        _("Svar"),
        help_text=_("Grundläggande HTML (fet, kursiv, listor, länkar) tillåtet."),
    )
    is_active = models.BooleanField(_("Aktiv"), default=True)
    order = models.PositiveIntegerField(_("Sortering"), default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = _("FAQ-fråga")
        verbose_name_plural = _("FAQ-frågor")

    def __str__(self):
        return self.question[:100]
