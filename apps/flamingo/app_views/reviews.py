"""
Omdömen i verktyget (/flamingo/app/omdomen/): kunden pekar ut sin
Google-profil, bekräftar "Det här är vi" och väljer vilka omdömen som syns
på sidorna, och i vilken ordning (reviews.py). Under Google står kundens
profil på Reco (reco.py, ankaret #reco): sidorna visar Recos egen ruta, eller
(Utvalda) de omdömen från profilen som kunden valt här. När Utvalda är
avstängt (reco.selected_enabled) göms valet, med en rad om varför.

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

                    Reco (reco.py):
                    POST action=reco_find        länken eller id:t läses (inget
                                                 anrop); "Är det här ni?" ritas
                    POST action=reco_confirm     "Det här är vi": profilsidan
                                                 hämtas, prövas och sparas
                    POST action=reco_refresh     "Hämta igen": profilen och
                                                 omdömena (inom gränsen per dag)
                    POST action=reco_select      Utvalda: valet och ordningen
                                                 (move=<id>:up/down)
                    POST action=reco_own         "Profilen är vår"
                    POST action=reco_disconnect  "Koppla bort profilen"

En profil som inte liknar företaget (reviews.store_details) visar inga
omdömen och inget betyg på sidorna förrän den är intygad; sidan säger det,
och byrån har larmats. Länkarna till Google prövas igen här innan de ritas
(reviews.google_link).

Ett demokonto anropar aldrig Google eller Reco, och utan
GOOGLE_PLACES_API_KEY görs inget anrop till Google: kunden kan ändå spara
sitt Place ID. Sökningar och hämtningar har en gräns per dag
(reviews.SEARCH_DAILY_MAX, DETAILS_DAILY_MAX, reco.LOOKUP_DAILY_MAX).
Recos svar och Googles fel visas aldrig, bara en svensk text.
Sidomenyn visar Företaget (sidan hör dit).
"""

from datetime import date

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse

from .. import limits, reco, reviews
from ..pagebuilder.render import _stars
from . import app_view, render_app


def _back(anchor=""):
    url = reverse("flamingo:app_reviews")
    return redirect(f"{url}#{anchor}" if anchor else url)


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
        "reco": _reco_context(account),
    }
    context.update(extra)
    return context


def _reco_context(account, pending=None, query=""):
    """Recos del av sidan (templates/flamingo/app/reviews/_reco.html)."""
    rating = account.reco_rating
    who = account.reco_confirmed_by
    return {
        "connected": bool(account.reco_venue_id),
        "trusted": account.reco_trusted,
        "unverified": account.reco_unverified,
        "is_demo": account.is_demo,
        "can_call": not reco.refusal(account),
        "venue_id": account.reco_venue_id,
        "name": account.reco_name,
        # Prövas igen här innan den ritas (den sparas prövad).
        "profile_url": reco.profile_link(account.reco_url),
        "rating": f"{rating:.1f}".replace(".", ",") if rating is not None else "",
        "rating_stars": _stars(rating or 0),
        "count": account.reco_review_count,
        "fetched_at": account.reco_fetched_at,
        "confirmed_at": account.reco_confirmed_at,
        "confirmed_by": (who.get_full_name() or who.get_username()) if who else "",
        "lookups_left": max(
            0, reco.LOOKUP_DAILY_MAX - limits.daily_used(account, reco.USAGE_LOOKUP)
        ),
        "pending": pending,
        "query": query,
        **_reco_selection(account),
    }


#: Så många omdömen som inte är valda syns direkt; resten under "Visa alla".
RECO_ROWS_SHOWN = 10


def _reco_selection(account):
    """Utvalda: på eller av, och omdömena i kundens ordning (de valda först,
    sedan resten nyast först)."""
    enabled = reco.selected_enabled()
    reviews = reco.stored_reviews(account) if enabled and account.reco_trusted else []
    by_id = {r["id"]: r for r in reviews}
    chosen = [str(i) for i in account.reco_reviews_selected or [] if str(i) in by_id]
    order = chosen + [r["id"] for r in reviews if r["id"] not in chosen]
    rows = []
    for review_id in order:
        review = dict(by_id[review_id])
        try:
            review["day"] = date.fromisoformat(review["date"])
        except ValueError:
            review["day"] = None
        position = chosen.index(review_id) if review_id in chosen else -1
        rows.append(
            {
                "id": review_id,
                "review": review,
                "shown": position >= 0,
                "number": position + 1,
                "stars": _stars(review["rating"]),
                "can_up": position > 0,
                "can_down": 0 <= position < len(chosen) - 1,
            }
        )
    cut = len(chosen) + RECO_ROWS_SHOWN
    return {
        "selected_enabled": enabled,
        "off_by_setting": reco.off_by_setting(),
        "rows": rows[:cut],
        "rows_more": rows[cut:],
        "has_reviews": bool(rows),
        "selected_count": len(chosen),
        "max_selected": reco.MAX_SELECTED,
    }


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
    if action.startswith("reco_"):
        return _reco_action(request, account, action)
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


# ---------------------------------------------------------------------------
# Reco
# ---------------------------------------------------------------------------


def _reco_render(request, account, pending=None, query=""):
    return _render(request, account, reco=_reco_context(account, pending, query))


def _reco_action(request, account, action):
    if action == "reco_find":
        return _reco_find(request, account)
    if action == "reco_confirm":
        return _reco_confirm(request, account)
    if action == "reco_refresh":
        return _reco_refresh(request, account)
    if action == "reco_select":
        return _reco_select(request, account)
    if action == "reco_own":
        try:
            reco.confirm_owner(account, request.user)
        except reco.RecoError as exc:
            messages.error(request, exc.message)
        else:
            text = "Tack. Profilen är intygad, och Recos ruta kan synas på sidorna."
            if reco.selected_enabled() and not account.reco_reviews:
                text += " Hämta omdömena med Hämta igen, så kan du välja vilka som visas."
            messages.success(request, text)
        return _back("reco")
    if action == "reco_disconnect":
        reco.disconnect(account)
        messages.success(
            request, "Profilen på Reco är bortkopplad. Recos ruta syns inte längre på sidorna."
        )
        return _back("reco")
    messages.error(request, "Okänd åtgärd.")
    return _back("reco")


def _reco_find(request, account):
    """Länken eller id:t läses utan anrop; kunden bekräftar sedan."""
    text = " ".join(request.POST.get("reco", "").split())[:500]
    link = reco.parse_link(text)
    if link.error:
        messages.error(request, link.error)
        return _reco_render(request, account, query=text)
    return _reco_render(request, account, pending=link, query=text)


def _reco_confirm(request, account):
    link = reco.parse_link(request.POST.get("reco", ""))
    if link.error:
        messages.error(request, link.error)
        return _back("reco")
    refused = reco.refusal(account)
    if refused:
        messages.info(request, refused)
        return _back("reco")
    try:
        match = reco.connect(account, link)
    except reco.RecoError as exc:
        messages.error(request, exc.message)
        return _back("reco")
    _reco_connected_message(request, account, match)
    return _back("reco")


def _reco_connected_message(request, account, match):
    name = account.reco_name or "Profilen"
    if account.reco_unverified:
        messages.warning(
            request,
            f"{name} är kopplad, men den liknar inte företaget: varken hemsidan eller "
            "telefonnumret på Reco stämmer med dina uppgifter. Recos ruta syns inte på "
            "sidorna förrän du intygat att profilen är er. ADX har fått veta det.",
        )
    elif match:
        messages.success(
            request,
            f"{name} är kopplad. Profilen har samma {match} som ni, så Recos ruta kan synas "
            "på sidorna: lägg till blocket Omdömen från Reco i sidbyggaren.",
        )
    else:
        messages.success(request, f"{name} är kopplad och intygad som er.")


def _reco_refresh(request, account):
    if not account.reco_venue_id:
        return _back("reco")
    refused = reco.refusal(account)
    if refused:
        messages.info(request, refused)
        return _back("reco")
    try:
        reco.connect(account, reco.stored_link(account))
    except reco.RecoError as exc:
        messages.error(request, exc.message)
    else:
        if reco.selected_enabled() and account.reco_trusted:
            messages.success(request, "Profilen och omdömena är hämtade från Reco igen.")
        else:
            messages.success(request, "Profilen är hämtad från Reco igen.")
    return _back("reco")


def _reco_select(request, account):
    """Utvalda: kundens val och ordning, som Googles (_select)."""
    if not reco.selected_enabled():
        messages.info(request, reco.SELECTED_OFF)
        return _back("reco")
    if not account.reco_trusted:
        messages.error(request, "Omdömena kan väljas när profilen på Reco är intygad som er.")
        return _back("reco")
    known = [r["id"] for r in reco.stored_reviews(account)]
    selected = [str(i) for i in account.reco_reviews_selected or [] if str(i) in known]
    order = [i for i in request.POST.getlist("order") if i in known]
    order += [i for i in selected if i not in order]
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
    too_many = len(chosen) > reco.MAX_SELECTED
    chosen = reco.select(account, chosen)
    if too_many:
        messages.warning(
            request,
            f"Högst {reco.MAX_SELECTED} omdömen kan visas. De {reco.MAX_SELECTED} första "
            "i ordningen är valda.",
        )
    elif not move:
        if chosen:
            word = "omdöme" if len(chosen) == 1 else "omdömen"
            messages.success(request, f"{len(chosen)} {word} från Reco syns på sidorna.")
        else:
            messages.info(
                request, "Inga omdömen från Reco är valda, så Utvalda syns inte på sidorna."
            )
    return _back("reco")
