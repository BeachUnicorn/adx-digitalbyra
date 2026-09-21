"""
Guiderna: markdownfiler i apps/aidocs/content/, renderade som Django-mallar.

En guide får kontext med de riktiga värdena (nyckeln, källkoden till
statusendpointet läst ur repot) så att den aldrig kan bli inaktuell
gentemot koden. Ny guide = ny fil + en rad i GUIDES.
"""

from pathlib import Path

from django.conf import settings
from django.template import Context, Engine

GUIDE_DIR = Path(__file__).parent / "content"

#: slug -> (titel, en rad om vad guiden ger)
GUIDES = {
    "overvakning": (
        "Övervakning: statusendpointet /status/adx/",
        "Lägg in ADX standardiserade statusrapport i ett Django-projekt så att adx.se "
        "kan visa databas, server, backup, deploy och besök för kunden.",
    ),
}

_ENGINE = Engine(autoescape=False)


def _source(relative):
    return (Path(settings.BASE_DIR) / relative).read_text(encoding="utf-8")


def context():
    base = (getattr(settings, "SITE_BASE_URL", "") or "https://adx.se").rstrip("/")
    return {
        "base_url": base,
        "status_key": getattr(settings, "ADX_STATUS_KEY", "")
        or "<NYCKEL SAKNAS PÅ ADX.SE - FRÅGA GIOVANNI>",
        "status_endpoint_source": _source("apps/monitor/status_endpoint.py"),
    }


def files(slug):
    """Filer guiden ber AI:n skapa, som [{path, content}] - för ?format=json."""
    if slug == "overvakning":
        return [
            {
                "path": "core/status_endpoint.py",
                "note": "Valfri plats; vilken app som helst som redan finns i projektet.",
                "content": _source("apps/monitor/status_endpoint.py"),
            }
        ]
    return []


def render(slug):
    path = GUIDE_DIR / f"{slug}.md"
    return _ENGINE.from_string(path.read_text(encoding="utf-8")).render(Context(context()))
