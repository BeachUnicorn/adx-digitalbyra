"""
ASGI-ingång.

Routar /mcp/ till MCP-servern (AI-redaktören) och allt annat till Django.
Kör med t.ex.:

    uvicorn config.asgi:application --port 8765

WSGI (gunicorn/runserver) fungerar fortfarande för sajten, men då svarar
/mcp/ inte - MCP-endpointen kräver ASGI.
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

from apps.assistant.asgi_app import build_application  # noqa: E402

application = build_application()
