"""
Omdömen från Google i verktyget (/flamingo/app/omdomen/): kunden pekar ut
sin Google-profil, bekräftar "Det här är vi" och väljer vilka omdömen som
syns på sidorna, och i vilken ordning (reviews.py).

    reviews_view    GET: profilen, omdömena och vägarna in
                    POST action=find      sök (namn och ort) eller en länk
                                          från Google Maps; svaret ritas
                                          direkt, inget sparas
                    POST action=place_id  ett Place ID: bekräftas sedan
                    POST action=confirm   "Det här är vi": Place Details
                    POST action=refresh   hämta omdömena igen
                    POST action=select    valet och ordningen (move=<id>:up/down)
                    POST action=own       "Profilen är vår": kunden (eller byrån)
                                          intygar att en profil som inte liknar
                                          företaget är dess (reviews.confirm_owner)
                    POST action=disconnect  "Koppla bort profilen"

En profil som inte liknar företaget (reviews.store_details) visar inga
omdömen och inget betyg på sidorna förrän den är intygad; sidan säger det,
och byrån har larmats. Länkarna till Google prövas igen här innan de ritas
(reviews.google_link).

Ett demokonto anropar aldrig Google, och utan GOOGLE_PLACES_API_KEY görs
inget anrop: kunden kan ändå spara sitt Place ID. Sökningar och hämtningar
har en gräns per dag (reviews.SEARCH_DAILY_MAX, DETAILS_DAILY_MAX).
Sidomenyn visar Företaget (sidan hör dit).
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse

from .. import limits, reviews
from ..pagebuilder.render import _stars
from . import app_view, render_app


def _back():
    return redirect(reverse("flamingo:app_reviews"))


def _rows(account):
    """Omdömena i kundens ordning: de valda först, sedan resten nyast först."""
    selected = [str(i) for i in account.google_reviews_selected or []]
    by_id = {str(r.get("id")): r for r in account.google_reviews or [] if isinstance(r, dict)}
    chosen = [i for i in selected if i in by_id]
    order = chosen + [i for i in by_id if i not in chosen]
    rows = []
    for review_id in order:
        review = dict(by_id[review_id])
        # Länkarna prövas igen när de ritas (de sparas prövade).
        review["author_uri"] = reviews.google_link(review.get("author_uri"))
        review["uri"] = reviews.google_link(review.get("uri"))
        position = chosen.index(review_id) if review_id in chosen else -1
        rows.append(
            {
                "id": review_id,
                "review": review,
                "shown": position >= 0,
                "number": position + 1,
                "stars": _stars(review.get("rating")),
                # Ordningen gäller de valda: pilarna flyttar bland dem.
                "can_up": position > 0,
                "can_down": 0 <= position < len(chosen) - 1,
            }
        )
    return rows


def _context(request, account, **extra):
    configured = reviews.is_configured()
    context = {
        "configured": configured,
        "is_demo": account.is_demo,
        "can_call": not reviews.refusal(account),
        "connected": bool(account.google_place_id),
        "fetched": account.google_reviews_fetched_at is not None,
        "unverified": account.google_place_unverified,
        "maps_uri": reviews.google_link(account.google_maps_uri),
        "rows": _rows(account),
        "rating": f"{account.google_rating:.1f}".replace(".", ",")
        if account.google_rating is not None
        else "",
        "rating_stars": _stars(account.google_rating or 0),
        "searches_left": max(
            0,
            reviews.SEARCH_DAILY_MAX - limits.daily_used(account, reviews.USAGE_SEARCH),
        ),
        "details_left": max(
            0,
            reviews.DETAILS_DAILY_MAX - limits.daily_used(account, reviews.USAGE_DETAILS),
        ),
        "hits": None,
        "pending": "",
        "query": "",
    }
    context.update(extra)
    return context


def _render(request, account, **extra):
    return render_app(
        request,
        "flamingo/app/reviews/reviews.html",
        "business",
        _context(request, account, **extra),
    )


@app_view
def reviews_view(request, account):
    if request.method != "POST":
        return _render(request, account)
    action = request.POST.get("action", "")
    if action == "find":
        return _find(request, account)
    if action == "place_id":
        return _place_id(request, account)
    if action == "confirm":
        return _confirm(request, account)
    if action == "refresh":
        return _refresh(request, account)
    if action == "select":
        return _select(request, account)
    if action == "own":
        return _own(request, account)
    if action == "disconnect":
        reviews.disconnect(account)
        messages.success(request, "Profilen är bortkopplad. Omdömena syns inte längre på sidorna.")
        return _back()
    messages.error(request, "Okänd åtgärd.")
    return _back()


def _find(request, account):
    text = " ".join(request.POST.get("q", "").split())[:500]
    if not text:
        messages.error(request, "Skriv företagets namn, eller klistra in länken från Google Maps.")
        return _render(request, account)
    link = reviews.parse_maps_link(text)
    if link is not None and link.error:
        messages.error(request, link.error)
        return _render(request, account, query=text)
    if link is not None and link.place_id:
        return _render(request, account, pending=link.place_id, query=text)
    query = link.query if link is not None else text
    if account.is_demo:
        messages.info(request, reviews.DEMO_REFUSED)
        return _render(request, account, query=text)
    if not reviews.is_configured():
        messages.info(
            request,
            "Sökningen kräver kopplingen till Google, som inte är påslagen än. "
            "Ange ditt Place ID i stället.",
        )
        return _render(request, account, query=text)
    try:
        hits = reviews.search(account, query)
    except reviews.ReviewsError as exc:
        messages.error(request, exc.message)
        return _render(request, account, query=text)
    return _render(request, account, hits=hits, query=text)


def _place_id(request, account):
    place_id = reviews.clean_place_id(request.POST.get("place_id"))
    if not place_id:
        messages.error(request, reviews.BAD_PLACE_ID)
        return _render(request, account, show_id=True)
    if reviews.refusal(account):
        return _save_id_only(request, account, place_id)
    return _render(request, account, pending=place_id)


def _save_id_only(request, account, place_id):
    reviews.save_place_id(account, place_id)
    if account.is_demo:
        messages.info(request, "Place ID är sparat. " + reviews.DEMO_REFUSED)
    else:
        messages.success(
            request,
            "Place ID är sparat. Omdömena hämtas när kopplingen till Google är påslagen.",
        )
    return _back()


def _confirm(request, account):
    place_id = reviews.clean_place_id(request.POST.get("place_id"))
    if not place_id:
        messages.error(request, reviews.BAD_PLACE_ID)
        return _back()
    if reviews.refusal(account):
        return _save_id_only(request, account, place_id)
    try:
        reviews.connect(account, place_id)
    except reviews.ReviewsError as exc:
        messages.error(request, exc.message)
        return _back()
    name = account.google_place_name or "Profilen"
    if account.google_place_unverified:
        messages.warning(
            request,
            f"{name} är kopplad, men den liknar inte företaget: namnet, hemsidan och "
            "telefonnumret stämmer inte med dina uppgifter. Omdömena och betyget syns inte "
            "på sidorna förrän du intygat att profilen är er. ADX har fått veta det.",
        )
    elif account.google_reviews:
        messages.success(
            request, f"{name} är kopplad. Välj vilka omdömen som ska synas på sidorna."
        )
    else:
        messages.success(request, f"{name} är kopplad. Google har inga omdömen att visa än.")
    return _back()


def _refresh(request, account):
    if not account.google_place_id:
        return _back()
    refused = reviews.refusal(account)
    if refused:
        messages.info(request, refused)
        return _back()
    try:
        reviews.connect(account, account.google_place_id)
    except reviews.ReviewsError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Omdömena är hämtade från Google igen.")
    return _back()


def _own(request, account):
    try:
        reviews.confirm_owner(account, request.user)
    except reviews.ReviewsError as exc:
        messages.error(request, exc.message)
        return _back()
    messages.success(
        request, "Tack. Profilen är intygad, och omdömena och betyget kan synas på sidorna."
    )
    return _back()


def _select(request, account):
    known = [row["id"] for row in _rows(account)]
    order = [i for i in request.POST.getlist("order") if i in known]
    order += [i for i in known if i not in order]
    shown = set(request.POST.getlist("show"))
    chosen = [i for i in order if i in shown]
    move = request.POST.get("move", "")
    if ":" in move:
        review_id, direction = move.rsplit(":", 1)
        if review_id in chosen:
            index = chosen.index(review_id)
            target = index - 1 if direction == "up" else index + 1
            if 0 <= target < len(chosen):
                chosen[index], chosen[target] = chosen[target], chosen[index]
    chosen = reviews.select(account, chosen)
    if not move:
        if chosen:
            word = "omdöme" if len(chosen) == 1 else "omdömen"
            messages.success(request, f"{len(chosen)} {word} syns på sidorna.")
        else:
            messages.info(request, "Inga omdömen är valda, så blocket syns inte på sidorna.")
    return _back()
