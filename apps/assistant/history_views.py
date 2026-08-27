"""
Historikvyer: versionslista + diff + återställning per objekt.

Generiska över de registrerade modellerna (revisions.VERSIONED_MODELS) - en
vy för alla objekttyper, med allowlist så URL:en inte kan peka på godtyckliga
modeller.
"""

import reversion
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from reversion.models import Version

from .diffing import field_diffs
from .models import RevisionMeta
from .revisions import VERSIONED_MODELS

#: "app_label.model_name" (gemener) -> modellklass, byggd lat.
_ALLOWED = None


def _allowed_models():
    global _ALLOWED
    if _ALLOWED is None:
        from .revisions import _resolve

        _ALLOWED = {}
        for path, _follow in VERSIONED_MODELS:
            try:
                model = _resolve(path)
            except AttributeError:
                continue
            _ALLOWED[f"{model._meta.app_label}.{model._meta.model_name}"] = model
    return _ALLOWED


def _model_or_404(app_label, model_name):
    model = _allowed_models().get(f"{app_label}.{model_name}".lower())
    if model is None:
        raise Http404("Modellen versioneras inte.")
    return model


def _field_labels(model):
    return {
        f.name: str(getattr(f, "verbose_name", f.name)).capitalize() for f in model._meta.fields
    }


def _revision_rows(obj, model):
    """Versioner nyast först, med diff mot närmast föregående version."""
    versions = list(
        Version.objects.get_for_object(obj).select_related("revision", "revision__user")
    )
    labels = _field_labels(model)
    rows = []
    for i, version in enumerate(versions):
        previous = versions[i + 1] if i + 1 < len(versions) else None
        meta = getattr(version.revision, "meta", None)
        rows.append(
            {
                "version": version,
                "revision": version.revision,
                "meta": meta,
                "is_current": i == 0,
                "diffs": field_diffs(
                    previous.field_dict if previous else None, version.field_dict, labels
                ),
            }
        )
    return rows


@login_required
def object_history(request, app_label, model_name, pk):
    model = _model_or_404(app_label, model_name)
    obj = get_object_or_404(model, pk=pk)
    return render(
        request,
        "manage/history/object_history.html",
        {
            "object": obj,
            "model_label": str(model._meta.verbose_name).capitalize(),
            "rows": _revision_rows(obj, model),
            "back_url": request.GET.get("next", "/manage/"),
        },
    )


@require_POST
@login_required
def revert_version(request, version_id):
    """
    Skriv tillbaka en tidigare version.

    Återställningen sker inuti middlewarens revision, så ångret blir i sig en
    ny version - historiken raderas aldrig. `next` valideras till /manage/.
    """
    version = get_object_or_404(Version.objects.select_related("revision"), pk=version_id)
    model = _allowed_models().get(f"{version.content_type.app_label}.{version.content_type.model}")
    if model is None:
        raise Http404("Modellen versioneras inte.")

    # Egen revision: middlewaren håller sig undan här (SELF_MANAGED_PREFIXES)
    # så att metadatan nedan blir den enda på revisionen.
    with reversion.create_revision():
        reversion.set_user(request.user)
        reversion.set_comment(
            f"Återställde till versionen från {version.revision.date_created:%Y-%m-%d %H:%M}"
        )
        reversion.add_meta(RevisionMeta, source=RevisionMeta.Source.MANUAL)
        # Ett objekt, inte hela revisionen: en importrevision kan innehålla
        # hundratals objekt, och den som klickar återställ på EN sida ska inte
        # skriva om alla. _object_version är reversions deserialiserade
        # historiska instans; save() skriver tillbaka den.
        version._object_version.object.save()
    messages.success(request, "Versionen är återställd. Återställningen syns i historiken.")

    next_url = request.POST.get("next", "")
    if not next_url.startswith("/manage/"):
        next_url = "/manage/"
    return redirect(next_url)
