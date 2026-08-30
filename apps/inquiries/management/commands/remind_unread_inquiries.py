"""
Morgonpåminnelse om olästa förfrågningar.

Svarstid är den enskilt största konverteringsfaktorn för tjänsteleads - den
som svarar först vinner ofta affären. Notismejlet vid inskick kan missas;
den här påminnelsen ser till att inget lead blir liggande.

Körs från cron (en gång per morgon). Skickar BARA om det finns olästa -
ett tomt mejl varje dag lär mottagaren att ignorera avsändaren. Ingen
databasstate behövs: en daglig sammanställning är idempotent per dag.

    manage.py remind_unread_inquiries
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.inquiries.emails import _as_list, _email_configured
from apps.inquiries.models import Inquiry


class Command(BaseCommand):
    help = "Mejla en påminnelse om det finns olästa förfrågningar."

    def handle(self, *args, **options):
        unread = list(Inquiry.objects.filter(is_read=False).order_by("created_at"))
        if not unread:
            self.stdout.write("Inga olästa förfrågningar - inget mejl.")
            return
        if not _email_configured():
            self.stderr.write("E-post är inte konfigurerad - hoppar över.")
            return

        base = (getattr(settings, "SITE_BASE_URL", "") or "https://adx.se").rstrip("/")
        now = timezone.now()
        lines = [f"{len(unread)} oläst(a) förfrågning(ar) väntar:", ""]
        for inquiry in unread:
            hours = int((now - inquiry.created_at).total_seconds() // 3600)
            source = f" | källa: {inquiry.traffic_source}" if inquiry.traffic_source else ""
            lines.append(
                f"- {inquiry.reference} {inquiry.name}"
                f" ({hours} h gammal{source})"
            )
            lines.append(f"  {base}{inquiry.get_absolute_url()}")
        lines += ["", "Snabbast svar vinner affären."]

        from django.core.mail import EmailMultiAlternatives

        recipients = _as_list(settings.INQUIRY_NOTIFICATION_EMAIL)
        if not recipients:
            self.stderr.write("Ingen mottagare (INQUIRY_NOTIFICATION_EMAIL) - hoppar över.")
            return
        msg = EmailMultiAlternatives(
            subject=f"Påminnelse: {len(unread)} obesvarad(e) förfrågning(ar)",
            body="\n".join(lines),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=recipients,
        )
        msg.send(fail_silently=False)
        self.stdout.write(self.style.SUCCESS(f"Påminnelse skickad ({len(unread)} olästa)."))
