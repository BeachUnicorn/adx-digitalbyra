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

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect

from .models import Issue


def is_agency_user(user):
    """
    Byrån = inloggad OCH inte kundkontakt. is_staff räcker alltid, men
    /manage/ har historiskt släppt in alla inloggade (login_required), och
    Giovannis befintliga konton ska inte låsas ute för att de saknar
    staff-flaggan. Gränsen som skyddar kunderna är kundmedlemskapet.
    """
    if not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    return not user.customers.exists()


def customer_for(user):
    if not user.is_authenticated or user.is_staff:
        return None
    return user.customers.filter(is_active=True).first()


def customer_issues(customer):
    return (
        Issue.objects.for_customer(customer)
        .filter(visible_to_customer=True)
        .select_related("project", "column", "customer")
    )


def staff_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not is_agency_user(request.user):
            return redirect("portal:home")
        return view(request, *args, **kwargs)

    return wrapped


def customer_required(view):
    """Portalvy: kräver en inloggad kontakt med en aktiv kund."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"/kund/logga-in/?next={request.path}")
        customer = customer_for(request.user)
        if customer is None:
            if is_agency_user(request.user):
                return redirect("manage:board")
            return HttpResponseForbidden("Kontot är inte kopplat till någon kund.")
        request.customer = customer
        return view(request, *args, **kwargs)

    return wrapped
