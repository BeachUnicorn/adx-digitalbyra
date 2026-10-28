"""
Verktyget (/flamingo/app/...): gemensamma byggstenar för alla vyer.

Varje vy i verktyget skrivs så här:

    from . import app_view, render_app

    @app_view
    def lead_list(request, account):
        leads = account.leads.all()          # alltid via kontot
        return render_app(request, "flamingo/app/inbox/list.html", "inbox", {"leads": leads})

    @app_view
    def google(request, account):
        return render_app(request, "flamingo/app/onboarding/google.html", "ads/google")

    @app_view
    def lead_detail(request, account, pk):
        lead = get_object_or_404(Lead, pk=pk, account=account)

- app_view kräver Flamingo-behörighet (samma som flamingo_required) och
  slår upp kundens FlamingoAccount. Vyn får kontot som andra argument och
  ska filtrera ALLT på det: ett id ur adressen hämtas aldrig utan
  account=account (eller account__customer=customer).
- Byrån utan kundvy har ingen kund: den får listan över Flamingo-kunder med
  "Visa som kunden" i stället för vyn (staff_index.html). Byrån i kundvyn
  på en kund som aldrig haft Flamingo får no_account.html.
- Byrån i kundvyn ser och gör det kunden ser och gör; det sparas i byråns
  namn. Bara utkastförhandsvisningen i /manage/ är skrivskyddad: grinden
  skickar då tillbaka en POST och mallarna döljer knappar med {{ read_only }}.
- render_app lägger på menyns kontext (app_context). Ingen
  kontextprocessor: bara verktygets sidor betalar för frågorna.
- Det tredje argumentet är sidnyckeln: delen i menyn och fliken under
  rubriken ("inbox", "ads/google", "messaging/contacts"), se
  apps/flamingo/app_nav.py. Delen blir app_active, fliken app_tab.
"""

from functools import wraps

from django.db.models import Count, Q
from django.shortcuts import render

from apps.website.models import SiteSettings

from .. import app_nav
from ..access import CONTACT
from ..models import Campaign, FlamingoAccount, Lead
from ..rules import onboarding_for
from ..views import flamingo_pages, flamingo_required

#: Menyns sex punkter i ordning: (app_active, url-namn, rubrik). Ett konto
#: utan utskick ser fem (Sms och e-post saknas), se app_nav.main_nav.
APP_NAV = tuple((item.key, item.url_name, item.label) for item in app_nav.MAIN_NAV)


def app_view(view):
    """Verktygsvy: Flamingo-behörighet och kundens konto.

    Vyn anropas som view(request, account, *args, **kwargs) och kontot finns
    också på request.flamingo_account."""

    @flamingo_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        access = request.flamingo
        if access.customer is None:
            return staff_index(request)
        account = (
            FlamingoAccount.objects.filter(customer=access.customer)
            .select_related("customer")
            .first()
        )
        if account is None:
            # Byrån i kundvyn på en kund som aldrig haft Flamingo. Kontakter
            # når aldrig hit (grinden kräver ett aktiverat konto).
            context = _shell_context(request)
            context["app_active"] = ""
            return render(request, "flamingo/app/no_account.html", context)
        request.flamingo_account = account
        return view(request, account, *args, **kwargs)

    return wrapper


def _shell_context(request):
    access = request.flamingo
    return {
        "flamingo": access,
        "flamingo_nav": flamingo_pages(access),
        "site_settings": SiteSettings.load(),
        "customer": access.customer,
        "read_only": access.read_only,
        "is_contact": access.mode == CONTACT,
    }


def app_context(request, active, tab_args=None):
    """Kontexten varje sida i verktyget behöver: menyn och flikraden, kunden
    och kontot, antal nya förfrågningar och var i kom-igång-stegen kunden är.

    active är sidnyckeln (app_nav.PAGE_KEYS): "overview", "inbox",
    "ads/google" ... Delen blir app_active, fliken app_tab. tab_args är
    id:t för flikar som hör till en sak (en landningssida, app_nav.Tab.per_object)."""
    section, tab = app_nav.split_key(active)
    account = getattr(request, "flamingo_account", None)
    context = _shell_context(request)
    tabs = app_nav.section_tabs(section, tab, account, args=tab_args)
    context.update(
        {
            "account": account,
            "app_active": section,
            "app_tab": tab,
            "app_nav": app_nav.main_nav(account),
            "app_tabs": tabs,
            "app_section_label": app_nav.section_label(section),
            # Sidor som guidens steg och rapporten sätter den till False.
            "show_tabs": bool(tabs),
            "new_lead_count": (
                account.leads.filter(status=Lead.STATUS_NEW).count() if account else 0
            ),
            "onboarding": onboarding_for(account) if account else None,
            # Kom-igång-stegen ovanför innehållet. Vyerna i kom-igång-flödet
            # (och översikten tills allt är klart) sätter den till True.
            "show_steps": False,
        }
    )
    if section == "messaging" and account is not None:
        context["new_buttons"] = _new_buttons(account, tabs, request.path)
    return context


def _new_buttons(account, tabs, path):
    """Nytt sms och Nytt mejl överst i Sms och e-post (mockupen), på varje
    fliks första sida. Nytt mejl syns när e-posten finns för kontot
    (demot har allt); Nytt sms alltid, som Nytt utskick förut: guidens steg
    Kanal säger vad som saknas."""
    if not any(t["url"] == path for t in tabs):
        return None
    from apps.utskick.sending import state

    return {"sms": True, "email": account.is_demo or state.email_live()}


def render_app(request, template, active, context=None, status=200, tab_args=None):
    """render() med app_context(request, active) under vyns egen kontext.
    active är sidnyckeln (app_nav.PAGE_KEYS); tab_args id:t för flikar som
    hör till en sak (en landningssida)."""
    merged = app_context(request, active, tab_args=tab_args)
    merged.update(context or {})
    return render(request, template, merged, status=status)


def staff_index(request):
    """Byrån utan kundvy: Flamingo-kunderna, med vägen in som kunden."""
    accounts = (
        FlamingoAccount.objects.filter(is_enabled=True, customer__is_active=True)
        .select_related("customer")
        .annotate(
            new_leads=Count("leads", filter=Q(leads__status=Lead.STATUS_NEW), distinct=True),
            in_review=Count(
                "campaigns",
                filter=Q(campaigns__status=Campaign.STATUS_IN_REVIEW),
                distinct=True,
            ),
        )
        .order_by("customer__name")
    )
    context = _shell_context(request)
    context.update({"accounts": accounts, "app_active": ""})
    return render(request, "flamingo/app/staff_index.html", context)
