from django import template

from apps.flamingo.access import portal_shows_flamingo

register = template.Library()


@register.simple_tag
def show_flamingo(request):
    """Portalen visar vägen in till ADX Flamingo bara för den som har det."""
    return portal_shows_flamingo(request)
