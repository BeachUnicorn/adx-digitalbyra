"""
OAuth 2.1-provider för MCP-SDK:t.

SDK:t levererar själva endpointerna (/authorize, /token, /register, revoke och
discovery-metadata). Vår del är lagringen och de två besluten protokollet inte
kan ta åt oss:

- `authorize()` returnerar en URL. Vi pekar den på Djangos inloggning +
  samtyckesvy, så kunden godkänner anslutningen som sig själv. Koden skapas
  först när hen klickat "Tillåt".
- Access-token knyts till en Django-användare, och `verify_token` gör den
  kopplingen tillbaka - resten av systemet ser bara "vem är det som skriver".

Bearer-nyckeln (AssistantToken) finns kvar parallellt för Claude Code och
skript, som kan skicka egna headers och därför inte behöver OAuth.
"""

import logging
from urllib.parse import urlencode

from asgiref.sync import sync_to_async
from django.conf import settings
from django.urls import reverse
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .oauth_models import ACCESS_TOKEN_TTL, DEFAULT_SCOPE, OAuthClient
from .oauth_models import AuthorizationCode as CodeRow
from .oauth_models import OAuthToken as TokenRow

logger = logging.getLogger(__name__)


def base_url():
    return (getattr(settings, "SITE_BASE_URL", "") or "").rstrip("/")


def _to_client_info(row):
    return OAuthClientInformationFull(
        client_id=row.client_id,
        client_secret=row.client_secret,
        client_name=row.client_name or None,
        redirect_uris=row.redirect_uris or [],
        grant_types=row.grant_types or ["authorization_code", "refresh_token"],
        response_types=row.response_types or ["code"],
        token_endpoint_auth_method=row.token_endpoint_auth_method,
        scope=row.scope or DEFAULT_SCOPE,
    )


class DjangoOAuthProvider:
    """OAuthAuthorizationServerProvider mot Django-modellerna."""

    # --- Klientregistrering -------------------------------------------------

    async def get_client(self, client_id):
        row = await sync_to_async(
            OAuthClient.objects.filter(client_id=client_id).first, thread_sensitive=True
        )()
        return _to_client_info(row) if row else None

    async def register_client(self, client_info):
        """
        Dynamisk klientregistrering (RFC 7591).

        Claude registrerar sig själv vid första anslutningen, så kunden slipper
        klistra in client id och secret för hand.

        SDK:t har redan genererat id och hemlighet på `client_info`. Vi
        behåller SDK:ts id - annars hittar klienten inte tillbaka till sin
        registrering - och lagrar hemligheten krypterad.
        """
        secret = client_info.client_secret

        def _create():
            row, _created = OAuthClient.objects.update_or_create(
                client_id=client_info.client_id,
                defaults={
                    "client_name": client_info.client_name or "",
                    "redirect_uris": [str(u) for u in (client_info.redirect_uris or [])],
                    "grant_types": list(client_info.grant_types or []),
                    "response_types": list(client_info.response_types or []),
                    "token_endpoint_auth_method": client_info.token_endpoint_auth_method
                    or "client_secret_post",
                    "scope": client_info.scope or DEFAULT_SCOPE,
                    "client_uri": str(client_info.client_uri or ""),
                },
            )
            row.set_secret(secret)
            row.save(update_fields=["client_secret_encrypted"])

        await sync_to_async(_create, thread_sensitive=True)()

    # --- Auktorisering ------------------------------------------------------

    async def authorize(self, client, params):
        """
        Skicka kunden till samtyckesvyn.

        Ingen kod skapas här - den skapas när kunden faktiskt godkänt, i
        `oauth_views.consent`. Att lämna ut en kod innan dess vore att
        auktorisera utan samtycke.
        """
        query = urlencode(
            {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "explicit_redirect": "1" if params.redirect_uri_provided_explicitly else "0",
                "code_challenge": params.code_challenge or "",
                "state": params.state or "",
                "scopes": " ".join(params.scopes or [DEFAULT_SCOPE]),
                "resource": params.resource or "",
            }
        )
        return f"{base_url()}{reverse('manage:oauth_consent')}?{query}"

    async def load_authorization_code(self, client, authorization_code):
        from .oauth_models import _hash

        def _load():
            row = (
                CodeRow.objects.select_related("client")
                .filter(code_hash=_hash(authorization_code), client__client_id=client.client_id)
                .first()
            )
            if row is None or not row.is_usable:
                return None
            return AuthorizationCode(
                code=authorization_code,
                scopes=row.scopes or [DEFAULT_SCOPE],
                expires_at=row.expires_at.timestamp(),
                client_id=client.client_id,
                code_challenge=row.code_challenge,
                redirect_uri=row.redirect_uri,
                redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
                resource=row.resource or None,
            )

        return await sync_to_async(_load, thread_sensitive=True)()

    async def exchange_authorization_code(self, client, authorization_code):
        from django.utils import timezone

        from .oauth_models import _hash

        def _exchange():
            row = (
                CodeRow.objects.select_related("client", "user")
                .filter(
                    code_hash=_hash(authorization_code.code),
                    client__client_id=client.client_id,
                )
                .first()
            )
            if row is None or not row.is_usable:
                raise ValueError("Auktoriseringskoden är förbrukad eller har gått ut.")

            # Engångsbruk: markera först, så en upprepad inlösen inte ger en
            # andra uppsättning tokens.
            row.used_at = timezone.now()
            row.save(update_fields=["used_at"])

            access, access_raw = TokenRow.issue(
                TokenRow.Kind.ACCESS, row.client, row.user, row.scopes, row.resource
            )
            _refresh, refresh_raw = TokenRow.issue(
                TokenRow.Kind.REFRESH, row.client, row.user, row.scopes, row.resource
            )
            return OAuthToken(
                access_token=access_raw,
                token_type="Bearer",
                expires_in=int(ACCESS_TOKEN_TTL.total_seconds()),
                scope=" ".join(access.scopes),
                refresh_token=refresh_raw,
            )

        return await sync_to_async(_exchange, thread_sensitive=True)()

    # --- Refresh ------------------------------------------------------------

    async def load_refresh_token(self, client, refresh_token):
        def _load():
            row = TokenRow.lookup(TokenRow.Kind.REFRESH, refresh_token)
            if row is None or row.client.client_id != client.client_id:
                return None
            return RefreshToken(
                token=refresh_token,
                client_id=client.client_id,
                scopes=row.scopes or [DEFAULT_SCOPE],
                expires_at=int(row.expires_at.timestamp()),
            )

        return await sync_to_async(_load, thread_sensitive=True)()

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        """
        Byt refresh-token mot ett nytt par.

        Refresh-token roterar: den gamla återkallas direkt. Dyker den upp igen
        är den stulen, och då är den redan värdelös.
        """

        def _exchange():
            row = TokenRow.lookup(TokenRow.Kind.REFRESH, refresh_token.token)
            if row is None or row.client.client_id != client.client_id:
                raise ValueError("Ogiltig refresh-token.")

            granted = list(scopes or row.scopes or [DEFAULT_SCOPE])
            row.revoke()

            access, access_raw = TokenRow.issue(
                TokenRow.Kind.ACCESS, row.client, row.user, granted, row.resource
            )
            _new_refresh, refresh_raw = TokenRow.issue(
                TokenRow.Kind.REFRESH, row.client, row.user, granted, row.resource
            )
            return OAuthToken(
                access_token=access_raw,
                token_type="Bearer",
                expires_in=int(ACCESS_TOKEN_TTL.total_seconds()),
                scope=" ".join(access.scopes),
                refresh_token=refresh_raw,
            )

        return await sync_to_async(_exchange, thread_sensitive=True)()

    # --- Access-token -------------------------------------------------------

    async def load_access_token(self, token):
        def _load():
            row = TokenRow.lookup(TokenRow.Kind.ACCESS, token)
            if row is None:
                return None
            from django.utils import timezone

            TokenRow.objects.filter(pk=row.pk).update(last_used_at=timezone.now())
            return AccessToken(
                token=token,
                client_id=row.client.client_id,
                scopes=row.scopes or [DEFAULT_SCOPE],
                expires_at=int(row.expires_at.timestamp()),
                resource=row.resource or None,
                subject=str(row.user_id),
            )

        return await sync_to_async(_load, thread_sensitive=True)()

    async def verify_token(self, token):
        return await self.load_access_token(token)

    async def revoke_token(self, token):
        def _revoke():
            for kind in (TokenRow.Kind.ACCESS, TokenRow.Kind.REFRESH):
                row = TokenRow.lookup(kind, token.token)
                if row is not None:
                    row.revoke()
                    return

        await sync_to_async(_revoke, thread_sensitive=True)()

    async def exchange_identity_assertion(self, client, params):
        raise NotImplementedError("Identity assertion används inte här.")
