"""
Vem får se vad.

Två sorters inloggade: byrån (is_staff) i /manage/, och kundkontakter
(vanliga användare kopplade till en Customer) i /kund/. Reglerna bor här
och ingen annanstans:

- customer_for(user): kunden en portalanvändare hör till (första aktiva).
- customer_issues(customer): ärenden kunden får se - hens egna, via
  projekt eller direkt, OCH bara de som är markerade synliga.
"""

from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect

from .models import Issue


def is_agency_user(user):
    """
    Byrån = inloggad med staff-flaggan. Inget annat räcker.

    Fram till 2026-10-03 räknades alla inloggade UTAN kundmedlemskap som
    byrån, för att äldre konton saknade flaggan. Det lämnade ett hål: en
    kontakt som togs bort från sin sista kund medan hen var inloggad blev
    "byrån" och kom in i /manage/ tills sessionen gick ut. Alla byråkonton
    har flaggan (kontrollerat i prod samma dag), så regeln är nu strikt.
    """
    return bool(user and user.is_authenticated and user.is_staff)


def customer_for(user):
    if not user.is_authenticated or user.is_staff:
        return None
    return user.customers.filter(is_active=True).first()


def customer_issues(customer):
    return (
        Issue.objects.for_customer(customer)
        .filter(visible_to_customer=True)
        .select_related("project", "customer")
    )


def staff_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not is_agency_user(request.user):
            return redirect("portal:home")
        return view(request, *args, **kwargs)

    return wrapped


#: Sessionsnyckel för "Visa som kund": byrån tittar på portalen med en
#: kunds ögon utan att logga in som kunden. Byråkontot förblir inloggat,
#: så ingen kundinloggning behövs och loggen visar vem som tittade.
VIEW_AS_KEY = "visa_som_kund"


def viewing_customer(request):
    """Kunden byrån just nu tittar som, eller None."""
    user = getattr(request, "user", None)
    if not user or not is_agency_user(user):
        return None
    pk = request.session.get(VIEW_AS_KEY)
    if not pk:
        return None
    from .models import Customer

    return Customer.objects.filter(pk=pk, is_active=True).first()


def customer_required(view):
    """
    Portalvy: kräver en inloggad kontakt med en aktiv kund - eller en
    byråanvändare i kundvyn. Kundvyn är SKRIVSKYDDAD: ett ärende skapat
    "som kunden" hade fått byråns användare som avsändare, och det ska
    aldrig kunna blandas ihop med vad kunden själv skrivit.
    """

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"/kund/logga-in/?next={request.path}")
        customer = customer_for(request.user)
        request.viewing_as = False
        if customer is None and is_agency_user(request.user):
            customer = viewing_customer(request)
            if customer is None:
                return redirect("manage:board")
            request.viewing_as = True
            if request.method == "POST":
                messages.error(
                    request,
                    "Du tittar som kunden och kan inte skriva här. "
                    "Skapa ärenden och svara från tavlan i panelen.",
                )
                return redirect("portal:home")
        if customer is None:
            return HttpResponseForbidden("Kontot är inte kopplat till någon kund.")
        request.customer = customer
        return view(request, *args, **kwargs)

    return wrapped
