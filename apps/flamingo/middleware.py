"""
Flamingogrinden: en enda punkt framför allt under /flamingo/.

Bara verktyget (/flamingo/app/...) kräver behörighet (Giovanni 2026-10-04).
Utan behörighet på Flamingos sidor sätts request.flamingo till PUBLIC och
vyn visar bara publicerade sidor, utan noindex. Utan behörighet i verktyget
svarar grinden inte själv - den routar förfrågan genom config.urls_public,
där /flamingo/ inte finns. Allt efter det (CSRF, APPEND_SLASH,
X-Frame-Options, sajtens 404) blir då identiskt med en okänd adress; ett
eget Http404 här gick att skilja från den (granskning 2026-10-03). Med
behörighet sätts request.flamingo och svaret blir ocachat; verktyget märks
dessutom noindex. Byrån i kundvyn gör exakt det kunden gör, och det den sparar
gäller på riktigt (Giovanni 2026-10-03); bara utkastförhandsvisningen i
/manage/ (access.PREVIEW, read_only) är skrivskyddad, och dit skickas en
POST tillbaka.
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.utils.cache import add_never_cache_headers

from .access import PUBLIC, FlamingoAccess, is_app_path, is_flamingo_path, resolve

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


class FlamingoGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Utskickens länkvärdar (k.adx.se, klick.adx.se) har sin egen
        # adresstabell; grinden får aldrig byta den mot config.urls_public.
        if getattr(request, "is_link_host", False):
            return self.get_response(request)
        # path_info, inte path: det är den resolvern matchar (path innehåller
        # SCRIPT_NAME om appen någon gång körs under ett prefix).
        if not is_flamingo_path(request.path_info):
            return self.get_response(request)
        access = resolve(request)
        app = is_app_path(request.path_info)
        if access is None and not app:
            # Flamingos publicerade sidor är öppna för alla, indexeras och
            # står i sitemapen (Giovanni 2026-10-04). Bara verktyget kräver
            # behörighet; utkast visas aldrig för PUBLIC (views.flamingo_page).
            request.flamingo = FlamingoAccess(PUBLIC)
            return self.get_response(request)
        if access is None:
            request.urlconf = "config.urls_public"
            return self.get_response(request)
        request.flamingo = access
        if access.read_only and request.method not in SAFE_METHODS:
            messages.info(request, "Förhandsvisningen är skrivskyddad.")
            return redirect(request.path_info)
        response = self.get_response(request)
        if app:
            response["X-Robots-Tag"] = "noindex, nofollow"
        add_never_cache_headers(response)
        return response
