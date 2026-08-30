"""
Hemsidekollen i /manage/ - INTE publik ännu.

Giovannis beslut 2026-08-30: "ja bygg, men publicera inte publikt ännu,
jag vill testa den grundligt själv först. lägg den i /manage/". Därför
login_required + staff-krav och ingen rad i sitemapen. När verktyget
släpps publikt får det egen landningssida, botskydd och rate limit enligt
beacon-mönstret - gaten här ersätter det under testfasen.
"""

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render

from .analyzer import AnalysError, analyze
from .models import SiteReport


@login_required
def hemsidekollen(request):
    if not request.user.is_staff:
        raise Http404
    report = None
    error = ""
    if request.method == "POST":
        url = request.POST.get("url", "")
        try:
            results = analyze(url)
            report = SiteReport.objects.create(
                url=results["url"], results=results, created_by=request.user
            )
        except AnalysError as exc:
            error = str(exc)

    history = SiteReport.objects.select_related("created_by")[:20]
    return render(
        request,
        "tools/hemsidekollen.html",
        {"report": report, "error": error, "history": history, "active": "tools"},
    )


@login_required
def hemsidekollen_report(request, pk):
    if not request.user.is_staff:
        raise Http404
    try:
        report = SiteReport.objects.get(pk=pk)
    except SiteReport.DoesNotExist:
        raise Http404 from None
    history = SiteReport.objects.select_related("created_by")[:20]
    return render(
        request,
        "tools/hemsidekollen.html",
        {"report": report, "error": "", "history": history, "active": "tools"},
    )
