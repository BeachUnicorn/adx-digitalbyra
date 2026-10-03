"""
Principerna bakom sidbyggarens förslag: AI-förslagen ("Bygg sidan åt mig",
"Skriv om") och Konverteringskollen taggar varje val med en av dem.

    PRINCIPLES        alla principer i visningsordning
    get(key)          Principle eller None
    label(key)        den svenska etiketten, eller ""
    as_dict(key)      {key, label, text, source} för JSON
    AI_KEYS           nycklarna AI får tagga ett förslag med
    BLOCK_PRINCIPLE   {blocktyp: princip} för förklaringarna

Påståendena är allmänna och ärliga: ingen statistik och inga procentsatser,
bara vad principen säger och var den kommer ifrån. Knapphet används aldrig
som press: bara när en bekräftad uppgift säger det, och då som en uppgift
bland andra (AI får inte tagga något med den).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Principle:
    key: str
    #: Kort svensk etikett, som taggen i gränssnittet.
    label: str
    #: En rad på svenska om vad principen säger.
    text: str
    #: Var den kommer ifrån, allmänt beskrivet.
    source: str
    #: False: AI får inte välja den själv (knapphet).
    ai: bool = True

    def as_dict(self):
        return {"key": self.key, "label": self.label, "text": self.text, "source": self.source}


CIALDINI = "Robert Cialdini, Influence (1984)"

PRINCIPLES = (
    Principle(
        "samma_budskap",
        "Samma budskap som annonsen",
        "Rubriken säger samma sak som sökningen och annonsen, så besökaren ser direkt att "
        "sidan är rätt.",
        "Message match inom sökannonsering, och Googles riktlinjer för landningssidans relevans.",
    ),
    Principle(
        "klarhet",
        "Klarhet först",
        "Vad, var och hur du når företaget ska gå att förstå på några sekunder "
        "(femsekunderstestet).",
        "Femsekunderstestet i användbarhetstester, och Nielsen Norman Groups studier av hur "
        "snabbt besökare bestämmer sig.",
    ),
    Principle(
        "pris_tidigt",
        "Priset tidigt",
        "Ett från-pris tidigt, i annonsen och på sidan, får den som inte vill betala det att "
        "låta bli att klicka. Det sparar pengar på klick som aldrig blir affär.",
        "Förkvalificering, vedertagen praxis i sökannonsering.",
    ),
    Principle(
        "en_handling",
        "En tydlig handling",
        "En sak att göra, ring eller skicka formuläret, i stället för flera knappar som "
        "tävlar om uppmärksamheten.",
        "Hicks lag (fler val tar längre tid att välja mellan) och vedertagen UX-praxis.",
    ),
    Principle(
        "farre_falt",
        "Färre fält",
        "Varje fält i ett formulär är ett skäl att avbryta. Fråga bara det som behövs för "
        "att höra av sig.",
        "Forskning om formulär från bland andra Baymard Institute och Nielsen Norman Group.",
    ),
    Principle(
        "socialt_bevis",
        "Andras omdömen",
        "Andras erfarenheter väger tyngre än det företaget säger om sig självt.",
        CIALDINI,
    ),
    Principle(
        "auktoritet",
        "Bevis på behörighet",
        "Behörigheter, försäkringar och medlemskap visar att någon annan har granskat företaget.",
        CIALDINI,
    ),
    Principle(
        "sympati",
        "Någon att känna igen",
        "Vi säger hellre ja till någon vi känner igen och tycker om.",
        CIALDINI,
    ),
    Principle(
        "ansikte_namn",
        "Ansikte och namn",
        "En riktig person med namn, och gärna en bild, gör företaget lättare att lita på.",
        f"Sympati, {CIALDINI}.",
    ),
    Principle(
        "omsesidighet",
        "Ge något först",
        "Den som först får något användbart, ett råd eller ett tydligt svar, ger gärna "
        "något tillbaka.",
        CIALDINI,
    ),
    Principle(
        "engagemang",
        "Ett litet första steg",
        "Ett litet första steg, som en enkel fråga, gör nästa steg lättare att ta.",
        CIALDINI,
    ),
    Principle(
        "gemenskap",
        "Nära besökaren",
        "Vi litar mer på dem som hör till samma sammanhang som vi, till exempel samma ort.",
        "Robert Cialdini, Pre-Suasion (2016).",
    ),
    Principle(
        "riskomvandning",
        "Mindre risk för köparen",
        "En garanti flyttar risken från köparen till företaget. Bara när garantin är bekräftad.",
        "Förlustaversion (Kahneman och Tversky): en förlust väger tyngre än en lika stor vinst.",
    ),
    Principle(
        "konkret",
        "Konkret och specifikt",
        "Konkreta uppgifter, vad, var och vad det kostar, är lättare att tro på än allmänna ord.",
        "Nielsen Norman Group om saklig, skannbar text på webben.",
    ),
    Principle(
        "nasta_steg",
        "Visa vad som händer sedan",
        "När besökaren vet vad som händer efter klicket minskar osäkerheten, och fler vågar "
        "ta steget.",
        "Jakob Nielsens heuristik om systemets status, och vedertagen UX-praxis.",
    ),
    Principle(
        "invandningar",
        "Bemöter invändningar",
        "Svar på de vanliga frågorna tar bort skäl att lämna sidan innan de hinner bli det.",
        "Vedertagen säljpraxis, och Nielsen Norman Group om frågor och svar.",
    ),
    Principle(
        "riktiga_bilder",
        "Riktiga bilder",
        "Bilder från egna jobb visar vad företaget gör. Bilder från bildbanker gör det inte.",
        "Nielsen Norman Group: besökare tittar på bilder med innehåll och hoppar över dekoration.",
    ),
    Principle(
        "snabb_sida",
        "Snabb sida",
        "En sida som laddar snabbt i mobilen tappar färre besökare.",
        "Googles riktlinjer för sidupplevelse (Core Web Vitals).",
    ),
    Principle(
        "knapphet",
        "Knapphet, bara som fakta",
        "Används bara när en bekräftad uppgift säger det, och då som en uppgift. Aldrig som "
        "press eller falsk brådska.",
        CIALDINI,
        ai=False,
    ),
)

BY_KEY = {p.key: p for p in PRINCIPLES}

#: Nycklarna AI får tagga ett förslag med (knapphet är inte med).
AI_KEYS = tuple(p.key for p in PRINCIPLES if p.ai)

#: Principen bakom varje blocktyp, för förklaringarna i förslaget.
BLOCK_PRINCIPLE = {
    "hero": "samma_budskap",
    "price": "pris_tidigt",
    "reviews_google": "socialt_bevis",
    "reviews_reco": "socialt_bevis",
    "certificates": "auktoritet",
    "guarantee": "riskomvandning",
    "person": "ansikte_namn",
    "steps": "nasta_steg",
    "before_after": "riktiga_bilder",
    "area": "gemenskap",
    "faq": "invandningar",
    "form": "farre_falt",
    "callbar": "en_handling",
}


def get(key):
    return BY_KEY.get(key)


def label(key):
    principle = BY_KEY.get(key)
    return principle.label if principle else ""


def as_dict(key):
    principle = BY_KEY.get(key)
    return principle.as_dict() if principle else None


def catalogue():
    """Alla principer som JSON (för gränssnittet)."""
    return [p.as_dict() for p in PRINCIPLES]
