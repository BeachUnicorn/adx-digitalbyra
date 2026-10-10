"""
404 från vyerna ritas i förfrågans egen tråd.

Under ASGI ritar Django annars felsidan med
sync_to_async(response_for_exception, thread_sensitive=False), alltså i
händelseloopens delade trådpool. Där behöver 404-sidan (meny, sidfot,
tjänster) en andra Postgres-anslutning medan förfrågans tråd fortfarande
håller sin första. Skannrarna 2026-10-09 frågade efter sluggar som inte
finns: varje sådan 404 kostade två anslutningar i stället för en.

process_exception körs i förfrågans tråd (thread_sensitive=True), och där
stängs anslutningen av request_finished som vanligt. Svaret är detsamma:
samma response_for_exception, samma handler404. 404:or som uppstår innan
någon vy valts (adresser som ingen route matchar) går den vanliga vägen;
den delade trådpoolen städar efter dem (apps/assistant/db.py, TidyExecutor).
"""

from django.core.handlers.exception import response_for_exception
from django.http import Http404
from django.utils.deprecation import MiddlewareMixin


class NotFoundInRequestThreadMiddleware(MiddlewareMixin):
    """Står sist i MIDDLEWARE: närmast vyn."""

    def process_exception(self, request, exception):
        if isinstance(exception, Http404):
            return response_for_exception(request, exception)
        return None
