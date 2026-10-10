"""
Brevs egna malltaggar (templates/utskick/brev/, README F.4, F.6), bredvid
sidbyggarens flamingo_pb som Brev också laddar.

    {% pb_empty_attrs "kicker" %}
        attributen för ett tomt fält på ett element som mallen ritar själv,
        med samma tagg och inline-stil som när fältet är ifyllt:
        data-pb-field, data-pb-empty och data-pb-placeholder (fältets
        etikett). Bara i redigeringsläget; annars ingenting.

Sidbyggarens {% pb_empty %} ritar ett eget element utan stil. På en sida
ger klassen layouten, men i ett mejl sitter stilen inline, så ett tomt
"Rubrik och bild" visade "Överrubrik", "Rubrik" och "Ingress" utan sina
marginaler och storlekar. Med pb_empty_attrs står platshållaren i samma
element som texten: på sin egen rad, i samma storlek och med samma
avstånd. Mallen ritar elementet när fältet har ett värde eller när
editing är satt:

    {% if v.kicker or editing %}<p style="{{ S.kick }}"
      {% if v.kicker %}{% pb "kicker" %}{% else %}{% pb_empty_attrs "kicker" %}{% endif %}
      >{{ v.kicker }}</p>{% endif %}

(på en rad i mallen: elementet ska vara helt tomt för att etiketten ska synas).

Allt escapas med format_html.
"""

from django import template
from django.utils.html import format_html

register = template.Library()


@register.simple_tag(takes_context=True)
def pb_empty_attrs(context, key):
    if not context.get("editing"):
        return ""
    b = context.get("b") or {}
    spec = (b.get("fields") or {}).get(str(key).split(".")[0])
    label = spec.label if spec is not None else key
    return format_html(
        ' data-pb-field="{}" data-pb-empty data-pb-placeholder="{}"',
        key,
        label,
    )
