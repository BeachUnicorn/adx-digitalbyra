from django.urls import path  # noqa: F401

app_name = "services"

# Tjänsternas publika sidor är BlockPages på /<slug>/ (t.ex. /webbutveckling/)
# - se seed_site. Service-modellen lever kvar som datakälla för navigation,
# tjänstelistan, förfrågningsformuläret och AI-redaktören, men har inga egna
# publika vyer. Tomma urlpatterns hellre än rutter mot rivna mallar
# (mönsterkatalogen, antikatalogen: "routade endpoints mot bortrivet lager").
urlpatterns = []
