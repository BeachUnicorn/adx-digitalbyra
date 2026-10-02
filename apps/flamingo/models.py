"""
ADX Flamingo: annonser, sidor och förfrågningar för kunder, mätt till affär.

Tjänsten är stängd: bara kunder som byrån aktiverat ser den (och byrån).
Aktiveringen är en egen rad per kund, inte ett fält på Customer: kundkortets
formulär sparar varje kryssruta det inte ritar som avbockad, och det har
redan stängt av saker en gång (apps/projects/forms.py, 2026-09-20).
"""

from django.conf import settings
from django.db import models

from apps.projects.models import Customer


class FlamingoAccount(models.Model):
    customer = models.OneToOneField(
        Customer, on_delete=models.CASCADE, related_name="flamingo", verbose_name="Kund"
    )
    is_enabled = models.BooleanField("ADX Flamingo aktiverat", default=False)
    enabled_at = models.DateTimeField(null=True, blank=True)
    enabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Flamingo-konto"
        verbose_name_plural = "Flamingo-konton"

    def __str__(self):
        state = "på" if self.is_enabled else "av"
        return f"{self.customer.name}: Flamingo {state}"


def account_for(customer):
    """Kundens Flamingo-rad, skapad vid första behovet."""
    account, _ = FlamingoAccount.objects.get_or_create(customer=customer)
    return account


def has_flamingo(customer):
    """Har kunden ADX Flamingo aktiverat? En aktiv kund krävs också."""
    if customer is None or not customer.is_active:
        return False
    return FlamingoAccount.objects.filter(customer=customer, is_enabled=True).exists()
