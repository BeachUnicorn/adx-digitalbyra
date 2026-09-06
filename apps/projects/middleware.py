"""
Portalgrinden: en kundkontakt som är inloggad får aldrig in i /manage/.

/manage/ är byggt på login_required rakt av (byrån var länge enda
användaren). När kunder får konton räcker inte det - utan den här
grinden skulle en kund kunna öppna /manage/ och se allt. Kundmedlemskapet
avgör (access.is_agency_user): kontakt -> portalen, annars byrån.
"""

from django.shortcuts import redirect

from .access import is_agency_user


class PortalGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and not is_agency_user(user)
            and request.path.startswith(("/manage/", "/admin/"))
            and not request.path.startswith("/manage/logout/")
        ):
            return redirect("portal:home")
        return self.get_response(request)
