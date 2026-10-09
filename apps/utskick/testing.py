"""
Hjälp för utskickens tester (test_s1_*.py och senare steg).

    class MinaTester(UtskickFixture, TestCase): ...

UtskickFixture.setUpTestData skapar demoföretaget Exempelrör
(exempelror.example) med Flamingo och utskick på, en aktuell version av
biträdesavtalet som kontot godkänt, en inloggning för kunden (anna) och en
för byrån (staff), och ett andra konto (Annanfirma) med utskick på för
tester av att inget läcker mellan konton. Nummer är PTS fiktiva serie
+4670174xxxx; adresser slutar på .example.

    self.client_for(user)         inloggad klient (byrån i kundvyn för Exempelrör)
    make_contact(account, **data) en kontakt via contacts.create (källa manual)

setUp glömmer processens prövade nycklar (keys.forget_verified), så att
varje test prövar fingeravtrycken mot sin egen databas, och tömmer cachen
(larmens spärr per process, alerts.py).
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client

from apps.flamingo.models import FlamingoAccount
from apps.projects.access import VIEW_AS_KEY
from apps.projects.models import Customer

from . import contacts, keys
from .models import DpaAcceptance, DpaVersion, UtskickSettings, default_consent_text

User = get_user_model()

PHONE_ANNA = "+46701740601"
PHONE_BO = "+46701740602"
PHONE_CILLA = "+46701740603"


def enable_utskick(account, slug, name):
    return UtskickSettings.objects.create(
        account=account,
        is_enabled=True,
        public_slug=slug,
        display_name=name,
        consent_text_sms=default_consent_text("sms", name),
        consent_text_email=default_consent_text("email", name),
    )


def make_contact(account, source="manual", **data):
    return contacts.create(account, data, source=source)


class UtskickFixture:
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        cls.customer = Customer.objects.create(name="Exempelrör AB")
        cls.anna = User.objects.create_user(
            "anna@exempelror.example",
            email="anna@exempelror.example",
            password="x",
            first_name="Anna",
            last_name="Lindqvist",
        )
        cls.customer.users.add(cls.anna)
        cls.account = FlamingoAccount.objects.create(customer=cls.customer, is_enabled=True)
        cls.settings = enable_utskick(cls.account, "exempelror", "Exempelrör")

        cls.other_customer = Customer.objects.create(name="Annanfirma AB")
        cls.other_account = FlamingoAccount.objects.create(
            customer=cls.other_customer, is_enabled=True
        )
        cls.other_settings = enable_utskick(cls.other_account, "annanfirma", "Annanfirma")

        cls.dpa = DpaVersion.objects.create(
            version="2026-10", text="Biträdesavtal", sha256="0" * 64, is_current=True
        )
        for account in (cls.account, cls.other_account):
            DpaAcceptance.objects.create(account=account, version=cls.dpa, accepted_by=cls.anna)

    def setUp(self):
        super().setUp()
        keys.forget_verified()
        cache.clear()

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        if user.is_staff:
            session = client.session
            session[VIEW_AS_KEY] = self.customer.pk
            session.save()
        return client
