"""
Underförslag: nästa steg för ett objekt som fått en ändring godkänd.

Deterministiska - de speglar vad som faktiskt saknas på raden, inte vad
modellen råkar tycka. Prompten bär en @-token så AI:n vet exakt vilket
objekt det gäller.

(Tidigare fanns här även exempelförslag för tomma chatten. De togs bort
2026-08-21: de lästes som statiska exempel oavsett att de byggdes på
verklig data, och tog plats från det som faktiskt räknas - rutan man
skriver i.)
"""


def followups_for(target):
    """
    Nästa steg för ett objekt som just fått en ändring godkänd.

    Underförslagen är deterministiska - vi tittar på vad som faktiskt
    saknas på raden, inte på vad modellen råkar tycka. Prompten bär en
    @-token så AI:n vet EXAKT vilket objekt det gäller.
    """
    from apps.areas.models import Area
    from apps.services.models import Service

    out = []
    if isinstance(target, Service):
        token = f"@tjanst:{target.slug}"
        if not target.steps.exists():
            out.append(
                {
                    "title": "Skriv arbetsgången",
                    "prompt": f"Skriv arbetsgången för {token} - stegen för hur jobbet går till.",
                }
            )
        if target.faq_section_id is None:
            out.append(
                {
                    "title": "Skapa FAQ för tjänsten",
                    "prompt": (
                        f"Skapa en FAQ-sektion för {token} med de vanligaste "
                        f"kundfrågorna, och koppla den till tjänsten."
                    ),
                }
            )
        if not target.body:
            out.append(
                {
                    "title": "Skriv brödtexten",
                    "prompt": f"Skriv brödtexten för {token} enligt skrivguiden.",
                }
            )
    elif isinstance(target, Area):
        token = f"@omrade:{target.slug}"
        if not target.body:
            out.append(
                {
                    "title": f"Skriv områdestext för {target.name}",
                    "prompt": (
                        f"Skriv en lokal områdestext för {token}. Unik för orten, "
                        f"inte en mall med utbytt ortsnamn."
                    ),
                }
            )
    return out[:3]
