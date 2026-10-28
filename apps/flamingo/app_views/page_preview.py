"""
Förhandsvisningen av en sida i verktyget (Giovanni 2026-10-10, "Good build
the landing page urls"; steg 2 2026-10-28, README):

    page_preview    /flamingo/app/sidor/<pk>/forhandsvisa/: sidans utkast
                    ritat som besökarna skulle se det

Varje sida har en egen adress, /lp/<konto>/<sida>/ (addresses.py,
public_views.page_landing), öppen när sidan är publicerad. Kampanjerna som
visar sidan har dessutom sin /lp/<slug>/. En sida som inte är publicerad har
bara förhandsvisningen: den kräver inloggning i verktyget (app_view, samma
kontroll av kunden som de andra sidvyerna, också för byrån i kundvyn och
demokontot), har noindex både i sidan och i X-Robots-Tag, och formuläret
skickar ingenting: ett ifyllt formulär visar tacksidan, märkt som
förhandsvisning, utan att någon förfrågan skapas, något sms skickas eller
något räknas.

Listan (pages/list.html), sidbyggaren (pages/editor.html) och sidans flikar
(pages/_detail_head.html) visar "Öppna sidan" (sidans egen adress) eller
"Förhandsvisa" med taggen page_preview_url (templatetags/flamingo_pages.py).
"""

from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from .. import pagebuilder
from ..models import LandingPage
from . import app_view

#: ?tack=1 visar förhandsvisningens tacksida (efter ett ifyllt formulär).
THANKS_PARAM = "tack"


def preview_url(page):
    return reverse("flamingo:app_page_preview", args=[page.pk])


def _respond(response):
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Cache-Control"] = "private, no-store"
    return response


@app_view
@require_http_methods(["GET", "HEAD", "POST"])
def page_preview(request, account, pk):
    """Utkastet som besökarna skulle se det, för den som är inloggad i
    verktyget. Formuläret prövas som på riktigt (felen syns), men ett
    formulär som går igenom leder bara till tacksidan här."""
    from ..public_views import HONEYPOT, LeadForm, _lp_consent

    page = get_object_or_404(LandingPage, pk=pk, account=account)
    here = preview_url(page)
    extra = {
        # preview: formulärets och tacksidans rad "Förhandsvisning: ..."
        "preview": True,
        # page_preview: remsan överst i layouten (lp/ren/layout.html).
        "page_preview": True,
        "action": here,
        "back_url": here,
    }
    if request.method != "POST" and request.GET.get(THANKS_PARAM) == "1":
        context = pagebuilder.page_view_context(
            page, account, None, which="draft", request=request, extra=extra
        )
        return _respond(render(request, pagebuilder.render.THANKS_TEMPLATE, context))

    spec = pagebuilder.form_spec(page.draft_blocks) or pagebuilder.FormSpec()
    consent = _lp_consent(account, spec)
    if request.method == "POST":
        form = LeadForm(request.POST, spec=spec, consent=consent)
        if request.POST.get(HONEYPOT, "") or form.is_valid():
            # Ingen förfrågan, inget sms, inget samtycke: bara tacksidan.
            return redirect(f"{here}?{THANKS_PARAM}=1")
    else:
        form = LeadForm(spec=spec, consent=consent)
    extra["lp_consent"] = consent
    html = pagebuilder.render_page_html(
        page, account, None, which="draft", request=request, form=form, extra=extra
    )
    return _respond(HttpResponse(html))
