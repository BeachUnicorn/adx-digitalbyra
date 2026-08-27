"""
Versionslagret: vad som versioneras och hur revisioner skapas.

- register_models() körs från AppConfig.ready() och registrerar innehålls-
  modellerna hos django-reversion. `follow` gör att t.ex. ett område och dess
  tjänstekopplingar/FAQ-rader versioneras som en enhet.
- ManageRevisionMiddleware fångar alla manuella skrivningar i /manage/ utan
  att röra en enda vy: varje POST wrappas i en revision med användare och
  källa MANUAL. Ingen revision sparas om inget registrerat objekt ändrades.
- MediaFile versioneras inte (binärdata; radering är redan mjuk).

Befintliga rader saknar historik tills första sparningen - kör
`manage.py createinitialrevisions` efter migrering (och i produktion).
"""

import reversion

#: (modellväg, follow-relationer). Ordningen spelar ingen roll.
VERSIONED_MODELS = [
    ("apps.website.models.BlockPage", ["blocks"]),
    ("apps.website.models.Block", []),
    ("apps.website.models.SiteSettings", []),
    ("apps.services.models.ServiceCategory", []),
    ("apps.services.models.Service", ["steps"]),
    ("apps.services.models.ServiceStep", []),
    ("apps.services.models.Audience", []),
    ("apps.areas.models.Area", ["area_services", "faq_items"]),
    ("apps.areas.models.AreaService", []),
    ("apps.areas.models.AreaFAQ", []),
    ("apps.faq.models.FAQSection", ["items"]),
    ("apps.faq.models.FAQItem", []),
]


def _resolve(path):
    module_path, name = path.rsplit(".", 1)
    module = __import__(module_path, fromlist=[name])
    return getattr(module, name)


def register_models():
    for path, follow in VERSIONED_MODELS:
        try:
            model = _resolve(path)
        except AttributeError:
            # Modellen finns inte (t.ex. ServiceStep om steps-relationen har
            # annat namn) - hoppa hellre än att krascha uppstarten; testerna
            # fångar luckan.
            continue
        if not reversion.is_registered(model):
            reversion.register(model, follow=follow or ())


#: Vyer som sätter sin egen revisionsmetadata. Reversion nästlar
#: revisionsblock, så om middlewaren också öppnade ett block här skulle
#: add_meta köras två gånger på samma revision - och RevisionMeta har
#: OneToOne mot revisionen. De här vägarna äger sina egna revisioner.
SELF_MANAGED_PREFIXES = ("/manage/ai/", "/manage/historik/")


class ManageRevisionMiddleware:
    """
    Wrappar skrivande /manage/-requests i en reversion-revision.

    Medvetet path-begränsad: publika sidor och API:er ska inte betala
    transaktionskostnaden. AI:ns ändringar går inte den här vägen - de
    skapas i draft.approve() med källa AI.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.method in ("GET", "HEAD", "OPTIONS")
            or not request.path.startswith("/manage/")
            or request.path.startswith(SELF_MANAGED_PREFIXES)
        ):
            return self.get_response(request)

        from .models import RevisionMeta

        with reversion.create_revision(atomic=True):
            response = self.get_response(request)
            user = getattr(request, "user", None)
            if user is not None and user.is_authenticated:
                reversion.set_user(user)
            reversion.add_meta(RevisionMeta, source=RevisionMeta.Source.MANUAL)
            return response
