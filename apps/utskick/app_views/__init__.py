"""
Verktygets vyer för Kontakter (S1) och Utskick (S2 och senare).

En modul per del, och en ägare per modul:

    contacts.py   listan, kortet, lägg till, redigera, samtycke, export, ta bort, rensa
    imports.py    importens steg och felfilen
    lists.py      listor och taggar
    fields.py     extrafälten
    signup.py     inställningarna för anmälningssidan
    settings.py   Inställningar för kontakter
    dpa.py        biträdesavtalet
    utskick.py    Utskick (S2): listan, guiden, Granska, rapporten,
                  mottagarna, testsändningen och Inställningar för utskick
    inbox_reply.py  svaren i Inkorgen (S2): svara och avregistrera från sms

Varje vy skrivs så här:

    from apps.utskick.access import owned, utskick_view
    from . import render_contacts

    @utskick_view
    def contact_detail(request, account, pk):
        kontakt = owned(Contact, account, pk)
        return render_contacts(request, "flamingo/app/kontakter/detail.html", "contacts",
                               {"kontakt": kontakt})

utskick_view ger 404 när utskick inte är på för kontot och 400 när ett id
ur förfrågan inte är kontots (access.owned_ids). render_contacts lägger på
verktygets kontext (sidomenyn med Kontakter aktiv) och flikraden. I mallar
och kontexter heter en kontakt alltid "kontakt" / "kontakter", aldrig
"contact" (README "Naming rule").
"""

from apps.flamingo.app_views import render_app

from .. import nav


def render_contacts(request, template, tab, context=None, status=200):
    """render_app för en sida under Kontakter: sidomenyns punkt är
    "contacts" och flikraden har tab ("contacts", "lists", "import",
    "fields", "signup", "settings") markerad. Mallarna utgår från
    flamingo/app/kontakter/_layout.html."""
    merged = {
        "kt_nav": nav.contacts_tabs(tab),
        "utskick_settings": getattr(request, "utskick_settings", None),
    }
    merged.update(context or {})
    return render_app(request, template, "contacts", merged, status=status)


def render_utskick(request, template, tab, context=None, status=200):
    """Som render_contacts, för en sida under Utskick: sidomenyns punkt är
    "utskick" och flikraden har tab ("utskick", "settings") markerad.
    Mallarna utgår från flamingo/app/utskick/_layout.html."""
    merged = {
        "ut_nav": nav.utskick_tabs(tab),
        "utskick_settings": getattr(request, "utskick_settings", None),
    }
    merged.update(context or {})
    return render_app(request, template, "utskick", merged, status=status)
