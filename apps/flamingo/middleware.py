"""
Flamingogrinden: en enda punkt framför allt under /flamingo/.

Utan behörighet svarar grinden inte själv - den routar förfrågan genom
config.urls_public, där /flamingo/ inte finns. Allt efter det (CSRF,
APPEND_SLASH, X-Frame-Options, sajtens 404) blir då identiskt med en okänd
adress; ett eget Http404 här gick att skilja från den (granskning
2026-10-03). Med behörighet sätts request.flamingo, svaret märks noindex
och ocachat, och byrån i kundvyn kan bara läsa.
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.utils.cache import add_never_cache_headers

from .access import is_flamingo_path, resolve

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


class FlamingoGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # path_info, inte path: det är den resolvern matchar (path innehåller
        # SCRIPT_NAME om appen någon gång körs under ett prefix).
        if not is_flamingo_path(request.path_info):
            return self.get_response(request)
        access = resolve(request)
        if access is None:
            request.urlconf = "config.urls_public"
            return self.get_response(request)
        request.flamingo = access
        if access.read_only and request.method not in SAFE_METHODS:
            messages.info(request, "Kundvyn är skrivskyddad. Ändra från panelen i stället.")
            return redirect(request.path_info)
        response = self.get_response(request)
        response["X-Robots-Tag"] = "noindex, nofollow"
        add_never_cache_headers(response)
        return response
