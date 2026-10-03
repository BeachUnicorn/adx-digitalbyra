"""
Demodata för ADX Flamingo, bara lokalt (settings.DEBUG måste vara på).

Skapar kunden "Lindqvist Rör AB (demo)" med Flamingo aktiverat, en kontakt
utan lösenord, uppgifter (några bekräftade), tre tjänster, fyra kampanjer
(utkast, hos ADX, väntar på kunden, live), åtta förfrågningar, en
konvertering i kö och sms-rader som visar att sms inte är inkopplat.

Idempotent: kundens Flamingo-innehåll byggs om från grunden varje gång.
Ingenting skickas: inga mejl, inga sms, inget till Google.

    uv run python manage.py flamingo_demo
"""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.projects.models import Customer

from ...models import (
    Campaign,
    Fact,
    FlamingoAccount,
    Lead,
    Review,
    Service,
    SmsLog,
)

CUSTOMER_NAME = "Lindqvist Rör AB (demo)"
CONTACT_EMAIL = "demo@lindqvistror.se"
WEBSITE = "https://lindqvistror.se"
PHONE = "08-000 00 00"

FACTS = [
    # key, label, value, source, confirmed
    ("telefon", "Telefon", PHONE, Fact.SOURCE_SITE, True),
    ("adress", "Adress", "Exempelvägen 4, Nacka", Fact.SOURCE_GOOGLE, True),
    ("jour", "Jour", "Dygnet runt, alla dagar", Fact.SOURCE_SITE, True),
    ("omrade", "Område", "Nacka, Värmdö och Tyresö", Fact.SOURCE_CUSTOMER, True),
    ("behorighet", "Behörighet", "Säker Vatten-auktoriserade", Fact.SOURCE_SITE, False),
    ("forsakring", "Försäkring", "Ansvarsförsäkring för alla jobb", Fact.SOURCE_SITE, False),
    # Samma nyckel och rubrik som onboarding.ensure_price_fact ger tjänsten.
    ("pris-badrumsrenovering", "Pris, Badrumsrenovering", "", Fact.SOURCE_CUSTOMER, False),
]

SERVICES = [
    ("Rörjour", Service.SALES_CALL),
    ("Badrumsrenovering", Service.SALES_QUOTE),
    ("Byte av varmvattenberedare", Service.SALES_QUOTE),
]

NEGATIVES = ["jobb", "lön", "utbildning", "gör det själv", "gratis", "praktik"]


class Command(BaseCommand):
    help = "Skapar demodata för ADX Flamingo (bara med DEBUG på)."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "flamingo_demo körs bara lokalt med DEBUG på. Demodata hör inte "
                "hemma i en riktig databas."
            )
        with transaction.atomic():
            customer, account, campaigns = self._build(timezone.now())
        self._print_urls(customer, campaigns)

    # ------------------------------------------------------------------

    def _build(self, now):
        User = get_user_model()
        customer, _ = Customer.objects.get_or_create(
            name=CUSTOMER_NAME,
            defaults={"website": WEBSITE, "phone": PHONE, "email": CONTACT_EMAIL},
        )
        if not customer.is_active:
            customer.is_active = True
            customer.save(update_fields=["is_active", "updated_at"])

        contact, created = User.objects.get_or_create(
            username=CONTACT_EMAIL,
            defaults={"email": CONTACT_EMAIL, "first_name": "Johan", "last_name": "Lindqvist"},
        )
        if created or contact.has_usable_password():
            contact.set_unusable_password()
            contact.save()
        customer.users.add(contact)

        staff = User.objects.filter(is_staff=True, is_active=True).order_by("pk").first()

        account, _ = FlamingoAccount.objects.get_or_create(customer=customer)
        account.is_enabled = True
        account.enabled_at = account.enabled_at or now - timedelta(days=40)
        account.enabled_by = account.enabled_by or staff
        account.website_url = WEBSITE
        account.scan_status = FlamingoAccount.SCAN_DONE
        account.scanned_at = now - timedelta(days=38)
        account.scan_error = ""
        account.google_ads_customer_id = ""
        account.google_status = FlamingoAccount.GOOGLE_BILLING_OK
        account.google_note = "Demo: kopplat för hand, inget riktigt konto."
        account.notify_phone = "070-000 00 00"
        account.notify_sms = True
        account.autoreply_enabled = True
        account.save()

        # Bygg om innehållet från grunden (kampanjerna först: tjänsterna är
        # skyddade så länge en kampanj pekar på dem).
        account.sms_log.all().delete()
        account.leads.all().delete()
        account.campaigns.all().delete()
        account.services.all().delete()
        account.facts.all().delete()

        for order, (key, label, value, source, confirmed) in enumerate(FACTS):
            Fact.objects.create(
                account=account,
                key=key,
                label=label,
                value=value,
                source=source,
                confirmed=confirmed,
                order=order,
            )
        services = {}
        for order, (name, mode) in enumerate(SERVICES):
            services[name] = Service.objects.create(
                account=account, name=name, sales_mode=mode, order=order
            )

        campaigns = self._campaigns(account, services, contact, staff, now)
        self._leads(account, services, campaigns, now)
        return customer, account, campaigns

    def _campaigns(self, account, services, contact, staff, now):
        jour = services["Rörjour"]
        badrum = services["Badrumsrenovering"]
        vvb = services["Byte av varmvattenberedare"]

        live = Campaign.objects.create(
            account=account,
            service=jour,
            name="Rörjour Nacka",
            status=Campaign.STATUS_LIVE,
            area="Nacka + 15 km",
            radius_km=15,
            daily_budget_kr=150,
            headlines=[
                "Rörjour i Nacka",
                "Ring Lindqvist Rör",
                "Jour dygnet runt",
                "Vattenläcka? Ring oss",
                "Stopp i avloppet?",
            ],
            descriptions=[
                "Vattenläcka eller stopp? Jouren är öppen dygnet runt, alla dagar.",
                "Rörjour i Nacka, Värmdö och Tyresö. Ring oss direkt.",
            ],
            keywords=[
                {"text": "rörjour nacka", "match": "phrase"},
                {"text": "akut rörjour", "match": "phrase"},
                {"text": "vattenläcka", "match": "phrase"},
            ],
            negatives=NEGATIVES,
            page={
                "title": "Rörjour i Nacka",
                "lead": "Vattenläcka eller stopp? Ring oss, jouren är öppen dygnet runt.",
                "points": ["Jour dygnet runt, alla dagar", "Nacka, Värmdö och Tyresö"],
                "phone": PHONE,
                "form_title": "Hellre att vi ringer dig?",
                "questions": [],
                "note": "Stäng huvudkranen medan du väntar. Den sitter oftast vid vattenmätaren.",
            },
            approved_at=now - timedelta(days=30),
            approved_by=contact,
            published_at=now - timedelta(days=29),
            created_by=contact,
            created_at=now - timedelta(days=35),
        )
        Review.objects.create(
            campaign=live,
            round=1,
            submitted_at=now - timedelta(days=33),
            submitted_by=contact,
            state=Review.STATE_DONE,
            reviewer=staff,
            reviewed_at=now - timedelta(days=32),
            snapshot=live.content_snapshot(),
            changes=[],
            note="Inget att ändra.",
        )

        needs = Campaign.objects.create(
            account=account,
            service=badrum,
            name="Badrumsrenovering",
            status=Campaign.STATUS_NEEDS_CUSTOMER,
            area="Nacka + 15 km",
            radius_km=15,
            daily_budget_kr=200,
            headlines=["Badrumsrenovering i Nacka", "Nytt badrum i Nacka", "Begär offert i dag"],
            descriptions=[
                "Berätta om ditt badrum så återkommer vi med en offert.",
                "Badrumsrenovering i Nacka, Värmdö och Tyresö.",
            ],
            keywords=[
                {"text": "badrumsrenovering nacka", "match": "phrase"},
                {"text": "renovera badrum", "match": "phrase"},
            ],
            negatives=NEGATIVES + ["badrumsmatta"],
            page={
                "title": "Badrumsrenovering i Nacka",
                "lead": "Berätta om ditt badrum så återkommer vi med en offert.",
                "points": ["Nacka, Värmdö och Tyresö"],
                "phone": PHONE,
                "form_title": "Berätta om ditt badrum",
                "questions": [
                    {"key": "storlek", "label": "Ungefär hur stort? (m2)", "kind": "text"},
                    {"key": "jobbet", "label": "Vad vill du göra?", "kind": "textarea"},
                ],
                "note": "",
            },
            created_by=contact,
            created_at=now - timedelta(days=4),
        )
        Review.objects.create(
            campaign=needs,
            round=1,
            submitted_at=now - timedelta(days=3),
            submitted_by=contact,
            state=Review.STATE_DONE,
            reviewer=staff,
            reviewed_at=now - timedelta(days=2),
            snapshot=needs.content_snapshot(),
            changes=[
                {
                    "field": "headlines",
                    "label": "Rubrik",
                    "before": "Badrum Nacka dygnet runt",
                    "after": "Badrumsrenovering i Nacka",
                    "reason": "Jouren gäller rörjour, inte renoveringar.",
                },
                {
                    "field": "negatives",
                    "label": "Negativt sökord",
                    "before": "",
                    "after": "badrumsmatta",
                    "reason": "De som söker på badrumsmattor vill inte renovera.",
                },
                {
                    "field": "page",
                    "label": "Sidans ingress",
                    "before": "Vi renoverar ditt badrum snabbt och billigt.",
                    "after": "Berätta om ditt badrum så återkommer vi med en offert.",
                    "reason": "Inga påståenden om pris eller tid som inte finns bland uppgifterna.",
                },
            ],
            note="Tre ändringar. Godkänn om de ser rätt ut.",
        )

        in_review = Campaign.objects.create(
            account=account,
            service=jour,
            name="Rörjour Värmdö",
            status=Campaign.STATUS_IN_REVIEW,
            area="Värmdö + 10 km",
            radius_km=10,
            daily_budget_kr=100,
            headlines=["Rörjour på Värmdö", "Ring Lindqvist Rör", "Jour dygnet runt"],
            descriptions=[
                "Vattenläcka eller stopp på Värmdö? Jouren är öppen dygnet runt.",
                "Rörjour på Värmdö, i Nacka och Tyresö. Ring oss direkt.",
            ],
            keywords=[{"text": "rörjour värmdö", "match": "phrase"}],
            negatives=NEGATIVES,
            page={
                "title": "Rörjour på Värmdö",
                "lead": "Vattenläcka eller stopp? Ring oss, jouren är öppen dygnet runt.",
                "points": ["Jour dygnet runt, alla dagar"],
                "phone": PHONE,
                "form_title": "Hellre att vi ringer dig?",
                "questions": [],
                "note": "",
            },
            created_by=contact,
            created_at=now - timedelta(days=1),
        )
        Review.objects.create(
            campaign=in_review,
            round=1,
            submitted_at=now - timedelta(hours=3),
            submitted_by=contact,
            state=Review.STATE_PENDING,
            snapshot=in_review.content_snapshot(),
        )

        draft = Campaign.objects.create(
            account=account,
            service=vvb,
            name="Byte av varmvattenberedare",
            status=Campaign.STATUS_DRAFT,
            area="Nacka + 10 km",
            radius_km=10,
            daily_budget_kr=100,
            headlines=["Ny varmvattenberedare", "Byte av varmvattenberedare"],
            descriptions=["Begär offert på byte av varmvattenberedare i Nacka."],
            keywords=[{"text": "byta varmvattenberedare", "match": "phrase"}],
            negatives=NEGATIVES,
            page={
                "title": "Byte av varmvattenberedare i Nacka",
                "lead": "",
                "points": [],
                "phone": PHONE,
                "form_title": "",
                "questions": [],
                "note": "",
            },
            created_by=contact,
            created_at=now - timedelta(hours=20),
        )
        return {"live": live, "needs_customer": needs, "in_review": in_review, "draft": draft}

    def _leads(self, account, services, campaigns, now):
        live = campaigns["live"]
        jour = services["Rörjour"]
        badrum = services["Badrumsrenovering"]
        vvb = services["Byte av varmvattenberedare"]

        def lead(ago, **fields):
            fields.setdefault("source", Lead.SOURCE_FORM)
            return Lead.objects.create(account=account, created_at=now - ago, **fields)

        waiting = lead(
            timedelta(hours=5),
            campaign=live,
            service=jour,
            name="Sara Holm",
            phone="070-123 45 67",
            message="Det läcker under diskbänken. Vattnet är avstängt.",
            gclid="demo-gclid-sara",
            keyword="rörjour nacka",
            utm={"utm_source": "google", "utm_medium": "cpc"},
        )
        lead(
            timedelta(days=9),
            campaign=live,
            service=jour,
            source=Lead.SOURCE_CALL,
            phone="076-555 44 33",
            message="Samtal, 2 minuter.",
        )
        lead(
            timedelta(days=2),
            campaign=live,
            service=jour,
            name="Maria Nilsson",
            phone="073-222 33 44",
            email="maria.nilsson@example.se",
            message="Stopp i avloppet i källaren.",
            gclid="demo-gclid-maria",
            keyword="stopp i avlopp",
            status=Lead.STATUS_CONTACTED,
        )
        lead(
            timedelta(days=6),
            service=badrum,
            source=Lead.SOURCE_MANUAL,
            name="Johan Berg",
            phone="070-987 65 43",
            message="Vill byta allt inklusive golvbrunn.",
            answers={"Ungefär hur stort? (m2)": "6"},
            status=Lead.STATUS_QUOTE,
        )
        won_jour = lead(
            timedelta(days=12),
            campaign=live,
            service=jour,
            name="Erik Svensson",
            phone="070-444 55 66",
            message="Läckande blandare i badrummet.",
            gclid="demo-gclid-erik",
            keyword="akut rörjour",
        )
        won_jour.set_status(Lead.STATUS_WON, value_kr=4800, now=now - timedelta(days=10))
        won_badrum = lead(
            timedelta(days=25),
            service=badrum,
            source=Lead.SOURCE_MANUAL,
            name="Lena Ek",
            phone="070-321 00 11",
            message="Badrum på 5 m2, helrenovering.",
        )
        won_badrum.set_status(Lead.STATUS_WON, value_kr=168000, now=now - timedelta(days=15))
        lead(
            timedelta(days=4),
            service=vvb,
            name="Per Lund",
            phone="072-111 22 33",
            message="Vill ha pris på en ny beredare.",
            status=Lead.STATUS_LOST,
        )
        lead(
            timedelta(days=3),
            campaign=live,
            service=jour,
            name="Webbyrå Exempel",
            email="info@example.com",
            message="Vill ni synas bättre på Google? Vi erbjuder sökmotoroptimering.",
            status=Lead.STATUS_JUNK,
        )

        body = f"Ny förfrågan: {waiting.display_name}, {waiting.phone}. {jour.name}."
        SmsLog.objects.create(
            account=account,
            lead=waiting,
            kind=SmsLog.KIND_OWNER,
            to=account.notify_phone,
            body=body,
            status=SmsLog.STATUS_NOT_CONFIGURED,
            created_at=waiting.created_at,
        )
        SmsLog.objects.create(
            account=account,
            lead=waiting,
            kind=SmsLog.KIND_AUTOREPLY,
            to=waiting.phone,
            body=account.autoreply_text,
            status=SmsLog.STATUS_NOT_CONFIGURED,
            created_at=waiting.created_at,
        )

    def _print_urls(self, customer, campaigns):
        w = self.stdout.write
        w(self.style.SUCCESS(f"Demodata klar: {customer.name} (kund {customer.pk})."))
        w("")
        w("Som kunden (logga in som byrån, sedan Visa som kunden på kundkortet):")
        w(f"  /manage/kunder/{customer.pk}/#flamingo")
        w("Verktyget:")
        w("  /flamingo/app/")
        w("  /flamingo/app/inkorg/")
        w("  /flamingo/app/kampanjer/")
        for state, campaign in campaigns.items():
            w(f"  /flamingo/app/kampanjer/{campaign.pk}/   ({state})")
        w("Landningssidan (live):")
        w(f"  {campaigns['live'].landing_url}")
        w("Byråns granskning:")
        w("  /manage/flamingo/granska/")
        w(f"  /manage/flamingo/granska/{campaigns['in_review'].pk}/")
        w("  /manage/flamingo/konverteringar.csv")
        w("")
        w(f"Kontakten {CONTACT_EMAIL} har inget lösenord. Inget har mejlats eller sms:ats.")
