"""
Sajtens adresser som om ADX Flamingo inte fanns.

Flamingogrinden (apps/flamingo/middleware.py) routar en obehörig förfrågan
till verktyget (/flamingo/app/...) hit i stället för att svara själv. Då går den genom exakt
samma kedja som vilken okänd adress som helst - CSRF, APPEND_SLASH,
X-Frame-Options, sajtens 404 - och svaret går inte att skilja från en
adress som inte finns.
"""

from config.urls import handler404, handler500  # noqa: F401
from config.urls import urlpatterns as _all

urlpatterns = [p for p in _all if str(p.pattern) != "flamingo/"]
