from .models import TimeEntry


def running_timer(request):
    """Byråns pågående timer, för huvudet på tavlan och i panelen."""
    user = getattr(request, "user", None)
    from .access import is_agency_user

    if not user or not is_agency_user(user):
        return {}
    entry = (
        TimeEntry.objects.running()
        .filter(user=user)
        .select_related("issue", "issue__project")
        .first()
    )
    return {"running_timer": entry}
