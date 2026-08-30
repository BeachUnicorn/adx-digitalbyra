"""Sparade granskningar från Hemsidekollen.

Historiken finns så att Giovanni kan jämföra körningar under testfasen -
och den blir grunden för publika, delbara rapporter den dag verktyget
släpps utåt (då med referens-slug i stället för id).
"""

from django.conf import settings
from django.db import models


class SiteReport(models.Model):
    url = models.URLField(max_length=500)
    results = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.url} ({self.created_at:%Y-%m-%d %H:%M})"
