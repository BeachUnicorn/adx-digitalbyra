"""
Påhittade sms för en kund, för att se SMS-sidorna lokalt (aldrig i drift).

    manage.py sms_seed_demo --customer <id> [--count 120] [--clear]

Numren är ur PTS serie för fiktiva mobilnummer (070-174 06 05 till 99) och
ett påhittat norskt nummer; inget skickas och 46elks anropas inte. Raderna
får provider_id (eller reference) som börjar med "demo-", så att de går att
känna igen och tas bort med --clear.
Vägrar utan DEBUG.
"""

import random
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from apps.projects.models import Customer
from apps.sms import encoding, pricing
from apps.sms.models import SmsAccount, SmsMessage

TEXTS = [
    "Hej! Din tid hos oss är bekräftad till torsdag kl 14.00. Välkommen!",
    "Påminnelse: du har en bokad tid i morgon kl 09.30. Svara inte på detta sms.",
    "Din order 10234 har skickats och kommer med PostNord.",
    "Din kod är 482913. Den gäller i tio minuter.",
    "Tack för ditt besök! Vi hoppas att allt blev bra.",
    "Hej! Vi har fått din förfrågan och ringer upp dig.",
    "Ditt paket väntar på utlämningsstället. Ta med legitimation.",
    "Nu har vi öppnat igen efter semestern. Varmt välkommen in!",
    "Glöm inte: fakturan förfaller på fredag. Tack!",
    "Hej! Din bil är klar att hämta. Vi har öppet till 18.",
    "Grattis på födelsedagen! Visa detta sms i kassan för 10 % rabatt.",
    "Vi har fått din betalning. Kvitto finns under Mina sidor. \U0001f60a",
]


class Command(BaseCommand):
    help = "Påhittade sms för en kund (bara lokalt, med DEBUG)."

    def add_arguments(self, parser):
        parser.add_argument("--customer", type=int, required=True)
        parser.add_argument("--count", type=int, default=120)
        parser.add_argument("--clear", action="store_true", help="Ta bort tidigare demo-sms.")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Bara lokalt: kommandot vägrar utan DEBUG.")
        customer = Customer.objects.filter(pk=options["customer"]).first()
        if customer is None:
            raise CommandError("Ingen sådan kund.")
        account = SmsAccount.objects.filter(customer=customer).first()
        if account is None or not account.is_enabled:
            raise CommandError("Aktivera SMS på kundkortet först.")
        if options["clear"]:
            deleted = (
                SmsMessage.objects.filter(account=account)
                .filter(Q(provider_id__startswith="demo-") | Q(reference__startswith="demo-"))
                .delete()[0]
            )
            self.stdout.write(f"{deleted} demo-sms borttagna.")
        rng = random.Random(customer.pk)
        now = timezone.now()
        made = 0
        for i in range(options["count"]):
            age = timedelta(days=rng.randint(0, 44), minutes=rng.randint(0, 1439))
            created = now - age
            country = "NO" if rng.random() < 0.08 else "SE"
            to = "+4791234567" if country == "NO" else f"+467017406{rng.randint(5, 99):02d}"
            body = rng.choice(TEXTS)
            if rng.random() < 0.1:
                body = body + " " + rng.choice(TEXTS)
            analysis = encoding.analyse(body)
            per_part = 7000 if country == "NO" else 5200
            roll = rng.random()
            status = (
                SmsMessage.Status.DELIVERED
                if roll < 0.9
                else SmsMessage.Status.FAILED
                if roll < 0.95
                else SmsMessage.Status.SENT
            )
            if age < timedelta(minutes=30) and status == SmsMessage.Status.DELIVERED:
                status = SmsMessage.Status.SENT
            cost = per_part * analysis.parts
            markup = pricing.markup_for(account, analysis.parts)
            SmsMessage.objects.create(
                account=account,
                to=to,
                country=country,
                sender=account.sender_name,
                body=body,
                parts=analysis.parts,
                encoding=analysis.encoding,
                status=status,
                provider_id=f"demo-{customer.pk}-{i}-{rng.randint(0, 10**8)}",
                estimated_cost=cost,
                provider_cost=cost,
                markup=markup,
                customer_price=cost + markup,
                created_at=created,
                sent_at=created + timedelta(seconds=1),
                delivered_at=(
                    created + timedelta(seconds=rng.randint(2, 40))
                    if status == SmsMessage.Status.DELIVERED
                    else None
                ),
                error_code="delivery_failed" if status == SmsMessage.Status.FAILED else "",
                error=(
                    "Operatören kunde inte leverera sms:et."
                    if status == SmsMessage.Status.FAILED
                    else ""
                ),
            )
            made += 1
        # Några stopp, så att filtret och felkoderna syns.
        SmsMessage.objects.create(
            account=account,
            to="+12025550123",
            country="US",
            sender=account.sender_name,
            body="Test till USA",
            reference="demo-stopp",
            parts=1,
            status=SmsMessage.Status.REJECTED,
            error_code="country_not_allowed",
            error="Kontot får inte skicka till USA (US).",
            provider_id="",
            created_at=now - timedelta(days=2),
        )
        self.stdout.write(self.style.SUCCESS(f"{made} demo-sms för {customer.name}."))
