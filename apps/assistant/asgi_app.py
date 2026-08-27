"""
ASGI-montering av MCP-servern.

/mcp/ hanteras av MCP-SDK:ts Streamable HTTP-app, allt annat av Django. En
tunn router framför båda, så produktionen kör en process.

Autentiseringen sitter här och inte inne i verktygen: en request utan giltig
bearer-token når aldrig MCP-lagret.
"""

import os

from asgiref.sync import sync_to_async

MCP_PATH = "/mcp"


def _unauthorized_response():
    """401 med WWW-Authenticate, formatet MCP-klienter förväntar sig."""

    async def app(scope, receive, send):
        body = b'{"error":"invalid_token","error_description":"Giltig bearer-token kravs."}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="adx-mcp"'),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return app


def _bearer_token(scope):
    """
    Plocka ut token ur Authorization-headern.

    Två toleranser, båda för samma verkliga problem: en nyckel som klistrats
    in via en webbläsare får ofta med sig ett hårt mellanslag (U+00A0) i
    stället för ett vanligt.

    - Avkoda som UTF-8. Ett hårt mellanslag går på tråden som två bytes
      (\\xc2\\xa0); latin-1 gör 'Â' av den första, som då klistrar fast vid
      "Bearer" och aldrig matchar.
    - Dela på godtyckligt blanktecken i stället för att kräva exakt "Bearer ".

    Utan detta blir enda symptomet en 401 utan förklaring - dyrt att felsöka
    för något som är uppenbart fel-tolererbart.
    """
    for name, value in scope.get("headers", []):
        if name != b"authorization":
            continue
        try:
            raw = value.decode("utf-8")
        except UnicodeDecodeError:
            raw = value.decode("latin-1")
        parts = raw.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    return None


#: Endpoints SDK:t äger själv (OAuth-flödet och discovery). De ska nås utan
#: token - det är ju där en klient skaffar sin token.
_OAUTH_PREFIXES = (
    "/.well-known/",
    "/authorize",
    "/token",
    "/register",
    "/revoke",
)


def _is_oauth_path(path):
    return any(path.startswith(p) for p in _OAUTH_PREFIXES)


class AuthenticatedMCPApp:
    """
    Släpper igenom två sorters åtkomst till verktygen:

    - OAuth access-token, som Claude-appens connector skaffar via /authorize.
    - Personlig bearer-nyckel (AssistantToken), för Claude Code och skript som
      kan skicka egna headers och inte behöver hela OAuth-dansen.

    Båda landar i samma sak: en Django-användare i `scope["assistant_user"]`.
    Resten av systemet bryr sig inte om vilken väg som användes.

    Databasslagningen är synkron, så den går via sync_to_async - ASGI-appen
    körs i en händelseloop.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return

        # OAuth-endpointerna autentiserar sig själva.
        if _is_oauth_path(scope.get("path", "")):
            await self.inner(scope, receive, send)
            return

        raw = _bearer_token(scope)
        user = await self._resolve_user(raw)
        if user is None:
            await _unauthorized_response()(scope, receive, send)
            return

        scope["assistant_user"] = user
        await self.inner(scope, receive, send)

    @staticmethod
    async def _resolve_user(raw):
        from .models import AssistantToken
        from .oauth_models import OAuthToken

        if not raw:
            return None

        def _lookup():
            # Personlig nyckel har eget prefix - billigast att testa först.
            if raw.startswith(AssistantToken.PREFIX):
                token = AssistantToken.authenticate(raw)
                return token.user if token else None
            row = OAuthToken.lookup(OAuthToken.Kind.ACCESS, raw)
            if row is None:
                return None
            from django.utils import timezone

            OAuthToken.objects.filter(pk=row.pk).update(last_used_at=timezone.now())
            return row.user

        return await sync_to_async(_lookup, thread_sensitive=True)()


def build_application():
    """Router: /mcp/* -> MCP-appen, allt annat -> Django."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

    from django.conf import settings
    from django.core.asgi import get_asgi_application

    django_app = get_asgi_application()

    if settings.DEBUG:
        # `runserver` serverar statiska filer åt oss; uvicorn gör det inte.
        # Utan det här renderas /manage/ helt ostylat vid lokal utveckling,
        # och lokal utveckling är hela poängen med att kunna köra ASGI direkt.
        from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler

        django_app = ASGIStaticFilesHandler(django_app)

    # Importeras efter get_asgi_application() så att apparna är laddade.
    from django.conf import settings
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from mcp.server.transport_security import TransportSecuritySettings

    from .mcp_server import build_server
    from .oauth_models import DEFAULT_SCOPE
    from .oauth_provider import DjangoOAuthProvider, base_url

    issuer = base_url() or "http://localhost:8765"
    provider = DjangoOAuthProvider()

    # SDK:ts DNS-rebinding-skydd tillåter bara localhost av sig självt.
    # I produktion avvisades Host: www.adx.se med 421 - OAuth gick
    # igenom men själva MCP-anropet stoppades ("Couldn't connect to the
    # server", 2026-08-22). Värdarna hämtas ur Djangos ALLOWED_HOSTS så
    # listan har EN källa; SDK:t vill ha dem med och utan port.
    allowed_hosts = ["localhost", "127.0.0.1"]
    for host in getattr(settings, "ALLOWED_HOSTS", []):
        if host and host != "*" and not host.startswith("."):
            allowed_hosts.append(host)
    allowed_hosts += [f"{h}:80" for h in allowed_hosts if ":" not in h]
    allowed_hosts += [f"{h}:443" for h in allowed_hosts if ":" not in h]
    allowed_hosts += ["localhost:8765", "127.0.0.1:8765"]

    security = TransportSecuritySettings(
        allowed_hosts=allowed_hosts,
        allowed_origins=[f"https://{h}" for h in allowed_hosts if ":" not in h]
        + [f"http://{h}" for h in ("localhost", "127.0.0.1")]
        + ["http://localhost:8765", "http://127.0.0.1:8765"],
    )

    mcp_app = AuthenticatedMCPApp(
        build_server().streamable_http_app(
            streamable_http_path=MCP_PATH,
            transport_security=security,
            auth=AuthSettings(
                issuer_url=issuer,
                resource_server_url=f"{issuer}{MCP_PATH}",
                required_scopes=[DEFAULT_SCOPE],
                # Claude registrerar sig själv vid första anslutningen, så
                # kunden slipper klistra in client id och secret för hand.
                client_registration_options=ClientRegistrationOptions(
                    enabled=True,
                    valid_scopes=[DEFAULT_SCOPE],
                    default_scopes=[DEFAULT_SCOPE],
                ),
                revocation_options=RevocationOptions(enabled=True),
            ),
            auth_server_provider=provider,
        )
    )

    async def application(scope, receive, send):
        path = scope.get("path", "")
        # OAuth-endpointerna ligger i MCP-appens rot, inte under /mcp/.
        if scope["type"] == "http" and _is_oauth_path(path):
            await mcp_app(scope, receive, send)
            return
        if scope["type"] == "http" and (path == MCP_PATH or path.startswith(MCP_PATH + "/")):
            # SDK:ts route är exakt "/mcp". En klient som skickar "/mcp/" får
            # annars en 307 från Starlette, och en omdirigerad POST tappar
            # innehållstypen hos vissa klienter. Normalisera i stället, så
            # båda skrivsätten fungerar - kunden klistrar in adressen för hand.
            if path.rstrip("/") == MCP_PATH:
                scope = {**scope, "path": MCP_PATH, "raw_path": MCP_PATH.encode()}
            await mcp_app(scope, receive, send)
            return
        if scope["type"] == "lifespan":
            # Både Django och MCP-appen vill ha lifespan; MCP-appens
            # session manager startar sina bakgrundsuppgifter där.
            await mcp_app(scope, receive, send)
            return
        await django_app(scope, receive, send)

    return application
