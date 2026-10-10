"""
Hjälp för utskickens tester (test_s1_*.py och senare steg).

    class MinaTester(UtskickFixture, TestCase): ...

UtskickFixture.setUpTestData skapar demoföretaget Exempelrör
(exempelror.example, också webbplatsen i ADX kundregister, så att länkar dit
inte behöver byråns granskning) med Flamingo och utskick på, en aktuell version av
biträdesavtalet som kontot godkänt, en inloggning för kunden (anna) och en
för byrån (staff), och ett andra konto (Annanfirma) med utskick på för
tester av att inget läcker mellan konton. Nummer är PTS fiktiva serie
+4670174xxxx; adresser slutar på .example.

    self.client_for(user)         inloggad klient (byrån i kundvyn för Exempelrör)
    make_contact(account, **data) en kontakt via contacts.create (källa manual)

setUp glömmer processens prövade nycklar (keys.forget_verified), så att
varje test prövar fingeravtrycken mot sin egen databas, och tömmer cachen
(larmens spärr per process, alerts.py).

    with tick_clock() as clock:   ticken och faserna på testklockan (TickClock)
    class X(OnTickClock, ...)     samma för hela testet, self.clock
"""

import threading
from contextlib import contextmanager
from time import sleep as real_sleep
from unittest import mock

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


class TickClock:
    """Testklockan för ticken och faserna. Budgetarna (budget i tick.run,
    fasernas sekunder, deadline i slingorna) mäts i time.monotonic(); med den
    riktiga klockan räknas allt annat som tar tid i processen in, och i hela
    sviten tog budgeten ibland slut innan sms-fasen kom fram (den får bara de
    första åtta sekunderna av en tick med budget=20). Den troliga orsaken var
    Sentrys övervakartråd (sentry.monitor), som test_sentry och apps.sms
    lämnade kvar (nu stänger de klienten): den sover time.sleep(10) i en
    slinga, och när testerna bytte ut time.sleep mot en MagicMock snurrade den
    utan paus och åt testtrådens tid. En lastad maskin gör samma sak.

    Här går klockan bara fram när koden väntar (time.sleep, som inte väntar på
    riktigt) eller när testet säger till (advance), och annars en mikrosekund
    per avläsning, så att tiden alltid går framåt som den riktiga (en budget
    på noll sekunder tar slut direkt). Samma test ger då samma utfall oavsett
    last; produktionens budgetar är orörda.

    Bara tråden som lade på klockan sover på den. Andra trådar i processen
    (Sentrys övervakare till exempel) sover på riktigt och flyttar inte
    testets klocka; de läser den bara, som kapplöpningarna i test_s3_kick.

    En slinga som väntar på att tiden ska gå utan att sova skulle snurra
    länge här: efter STILL_LIMIT avläsningar i rad utan väntan blir det ett
    AssertionError i stället.
    """

    START = 1000.0
    STEP = 0.000001
    STILL_LIMIT = 100_000

    def __init__(self):
        self.now = self.START
        self.slept = []
        self._reads = 0
        self._owner = threading.get_ident()

    def monotonic(self):
        self._reads += 1
        if self._reads > self.STILL_LIMIT:
            raise AssertionError("Testklockan: en slinga väntar på tiden utan att sova.")
        self.now += self.STEP
        return self.now

    def sleep(self, seconds):
        if threading.get_ident() != self._owner:
            real_sleep(seconds)
            return
        self.slept.append(seconds)
        self.advance(seconds)

    def advance(self, seconds):
        self.now += max(0.0, float(seconds))
        self._reads = 0


@contextmanager
def tick_clock():
    """time.monotonic och time.sleep på en TickClock så länge blocket varar.
    En lapp på time.sleep innanför blocket vinner över klockans, för alla
    trådar (och klockan går då inte fram när koden väntar); läs hellre
    clock.slept."""
    clock = TickClock()
    with (
        mock.patch("time.monotonic", clock.monotonic),
        mock.patch("time.sleep", clock.sleep),
    ):
        yield clock


class OnTickClock:
    """Blandas in först i en testklass eller fixtur: hela testet går på
    testklockan (tick_clock), som läggs på efter fixturens egna lappar.
    self.clock är klockan."""

    def setUp(self):
        super().setUp()
        self.clock = self.enterContext(tick_clock())


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
        cls.customer = Customer.objects.create(
            name="Exempelrör AB", website="https://exempelror.example"
        )
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
