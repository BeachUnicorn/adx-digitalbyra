"""
Samtyckesvyn i OAuth-flödet.

MCP-SDK:ts /authorize skickar hit. Kunden loggar in som vanligt (login_required
sköter det) och ser vilken app som vill ansluta. Först vid "Tillåt" skapas
auktoriseringskoden och vi skickar tillbaka klienten.

Att koden skapas här och inte i providern är hela poängen: ingen kod lämnas ut
utan att en människa klickat.
"""

import secrets

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.utils import timezone
from mcp.server.auth.provider import construct_redirect_uri

from apps.website.models import SiteSettings

from .oauth_models import AUTH_CODE_TTL, DEFAULT_SCOPE, AuthorizationCode, OAuthClient, _hash


def _params(request):
    """Läs och validera parametrarna /authorize skickade med."""
    get = request.GET.get
    client_id = get("client_id", "")
    redirect_uri = get("redirect_uri", "")
    code_challenge = get("code_challenge", "")

    if not (client_id and redirect_uri):
        return None, "Anropet saknar client_id eller redirect_uri."

    client = OAuthClient.objects.filter(client_id=client_id).first()
    if client is None:
        return None, "Okänd klient. Ta bort kopplingen i din AI-app och lägg till den igen."

    # PKCE är obligatoriskt i OAuth 2.1. Utan code_challenge kan en avlyssnad
    # kod lösas in av någon annan.
    if not code_challenge:
        return None, "Anslutningen saknar PKCE (code_challenge) och kan inte godkännas."

    if redirect_uri not in [str(u) for u in (client.redirect_uris or [])]:
        return None, "Adressen appen vill skickas tillbaka till matchar inte registreringen."

    return {
        "client": client,
        "redirect_uri": redirect_uri,
        "explicit_redirect": get("explicit_redirect", "1") == "1",
        "code_challenge": code_challenge,
        "state": get("state", ""),
        "scopes": (get("scopes", "") or DEFAULT_SCOPE).split(),
        "resource": get("resource", ""),
    }, None


@login_required
def consent(request):
    data, error = _params(request)
    if error:
        return HttpResponseBadRequest(error)

    if request.method == "POST":
        if request.POST.get("decision") != "allow":
            return redirect(
                construct_redirect_uri(
                    data["redirect_uri"],
                    error="access_denied",
                    error_description="Kunden nekade anslutningen.",
                    state=data["state"] or None,
                )
            )

        raw_code = secrets.token_urlsafe(32)
        AuthorizationCode.objects.create(
            code_hash=_hash(raw_code),
            client=data["client"],
            user=request.user,
            redirect_uri=data["redirect_uri"],
            redirect_uri_provided_explicitly=data["explicit_redirect"],
            code_challenge=data["code_challenge"],
            scopes=data["scopes"],
            resource=data["resource"],
            expires_at=timezone.now() + AUTH_CODE_TTL,
        )
        return redirect(
            construct_redirect_uri(data["redirect_uri"], code=raw_code, state=data["state"] or None)
        )

    return render(
        request,
        "manage/assistant/oauth_consent.html",
        {
            "site_settings": SiteSettings.load(),
            "active": "assistant",
            "client": data["client"],
            "redirect_uri": data["redirect_uri"],
        },
    )
