"""
Förfrågningsformuläret - ett kvalificerande enkelstegsformulär för byrån.

Ämnesvalet byggs ur en vitlista (tjänsterna i databasen + paketen + Annat)
så en select aldrig kan smuggla in fritext, och alla textfält går genom
samma saneringsgräns som resten av sajten (apps.common.security).
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.common.security import sanitize_plain_text

from .models import CustomerType, InquiryBudget, InquiryTimeline

PACKAGE_TOPICS = ["Paket: Start", "Paket: Tillväxt", "Paket: Enterprise"]
OTHER_TOPIC = "Annat / vet inte än"


def topic_choices():
    """Tjänsterna + paketen + Annat. Läses ur databasen vid varje rendering
    så listan aldrig glider ifrån tjänsteutbudet."""
    from apps.services.models import Service

    services = Service.objects.filter(is_active=True).order_by("order", "name")
    choices = [(s.name, s.name) for s in services]
    choices += [(p, p) for p in PACKAGE_TOPICS]
    choices.append((OTHER_TOPIC, OTHER_TOPIC))
    return choices


class InquiryForm(forms.Form):
    topic = forms.ChoiceField(label=_("Vad gäller det?"), choices=topic_choices)
    company_name = forms.CharField(
        max_length=200, required=False, label=_("Företag / organisation")
    )
    name = forms.CharField(max_length=200, label=_("Namn"))
    email = forms.EmailField(label=_("E-post"))
    phone = forms.CharField(max_length=50, required=False, label=_("Telefon (valfritt)"))
    customer_type = forms.ChoiceField(
        label=_("Ni är"),
        choices=CustomerType.choices,
        initial=CustomerType.COMPANY,
        required=False,
    )
    budget = forms.ChoiceField(
        label=_("Ungefärlig budget (valfritt)"),
        choices=[("", "—")] + list(InquiryBudget.choices),
        required=False,
    )
    timeline = forms.ChoiceField(
        label=_("Önskad tidplan (valfritt)"),
        choices=[("", "—")] + list(InquiryTimeline.choices),
        required=False,
    )
    description = forms.CharField(
        label=_("Berätta om ert behov"),
        widget=forms.Textarea,
        help_text=_("Var står ni i dag, och vart vill ni? Ett par meningar räcker."),
    )

    def clean_name(self):
        return sanitize_plain_text(self.cleaned_data["name"], max_length=200)

    def clean_company_name(self):
        return sanitize_plain_text(self.cleaned_data.get("company_name", ""), max_length=200)

    def clean_phone(self):
        return sanitize_plain_text(self.cleaned_data.get("phone", ""), max_length=50)

    def clean_description(self):
        return sanitize_plain_text(self.cleaned_data["description"], max_length=5000)


class NewsletterForm(forms.Form):
    email = forms.EmailField(label=_("E-post"))
