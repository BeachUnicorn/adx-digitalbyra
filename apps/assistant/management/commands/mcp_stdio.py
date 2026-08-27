"""
Kör MCP-servern över stdio, för klienter som startar servern som en process.

Claude Desktops egen konfigurationsfil (`claude_desktop_config.json`) stödjer
bara stdio - dess Connectors-flik är en annan sak, som ansluter från
Anthropics servrar och därför kräver en publik HTTPS-adress. Det här
kommandot är vägen in för lokalt arbete: ingen tunnel, ingen nyckel, ingen
server som behöver köra.

Behörigheten avgörs när kommandot startas, inte per anrop. Processen körs av
kunden på hens egen dator och har redan full databasåtkomst - en token skulle
inte skydda något som inte redan är öppet.

Samma operationsregister och samma säkerhetsgräns som över HTTP: skrivningar
blir utkast som godkänns i /manage/.
"""

import asyncio

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.assistant.mcp_server import build_server, set_stdio_user


class Command(BaseCommand):
    help = "Kör MCP-servern över stdio (för Claude Desktop och liknande klienter)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user",
            help=(
                "Användarnamn att agera som. Utelämnas den och det bara finns "
                "en användare väljs den automatiskt."
            ),
        )

    def handle(self, *args, **options):
        user = self._resolve_user(options.get("user"))
        set_stdio_user(user)

        # All utskrift går till stderr: stdout är protokollkanalen, och ett
        # enda extra tecken där gör att klienten inte kan tolka svaret.
        self.stderr.write(f"MCP-server över stdio, agerar som {user.get_username()}.")

        asyncio.run(self._serve())

    def _resolve_user(self, username):
        User = get_user_model()
        if username:
            user = User.objects.filter(username=username, is_active=True).first()
            if user is None:
                raise CommandError(f"Hittar ingen aktiv användare: {username}")
            return user

        users = list(User.objects.filter(is_active=True, is_staff=True)[:2])
        if not users:
            raise CommandError("Det finns ingen aktiv personalanvändare att agera som.")
        if len(users) > 1:
            names = ", ".join(
                User.objects.filter(is_active=True, is_staff=True).values_list(
                    "username", flat=True
                )
            )
            raise CommandError(f"Ange --user. Möjliga: {names}")
        return users[0]

    async def _serve(self):
        from mcp.server.stdio import stdio_server

        server = build_server()
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
