"""
Acceptformuläret: det kunden fyller i när den säger ja.

Beställarens namn, företag och organisationsnummer gör accepten till en
beställning som går att fakturera. Fakturaadress och referens frågas här
för att de annars måste jagas innan första fakturan.
"""

from django import forms


def normalize_org_number(raw):
    """
    '5567 12-3456' -> '556712-3456'. Tio siffror (bolag) eller tolv
    (enskild firma med sekelsiffror). Returnerar None om det inte går.
    """
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) == 10:
        return f"{digits[:6]}-{digits[6:]}"
    if len(digits) == 12:
        return f"{digits[:8]}-{digits[8:]}"
    return None


class AcceptForm(forms.Form):
    first_name = forms.CharField(label="Förnamn", max_length=80)
    last_name = forms.CharField(label="Efternamn", max_length=80)
    email = forms.EmailField(label="E-post", max_length=254)
    phone = forms.CharField(label="Telefon", max_length=40)
    company = forms.CharField(label="Företagsnamn", max_length=200)
    org_number = forms.CharField(label="Organisationsnummer", max_length=20)
    billing_address = forms.CharField(
        label="Fakturaadress",
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=500,
        help_text="Gata, postnummer och ort.",
    )
    billing_email = forms.EmailField(
        label="Faktura-e-post",
        required=False,
        max_length=254,
        help_text="Bara om fakturan ska gå till en annan adress än er e-post ovan.",
    )
    reference = forms.CharField(
        label="Er referens eller PO-nummer",
        required=False,
        max_length=100,
        help_text="Om ni vill ha en märkning på fakturan.",
    )
    message = forms.CharField(
        label="Meddelande till oss",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=2000,
    )
    confirm = forms.BooleanField(
        label="Jag har rätt att beställa för företaget och godkänner offerten.",
        error_messages={"required": "Bekräfta att ni godkänner offerten."},
    )

    def clean_org_number(self):
        normalized = normalize_org_number(self.cleaned_data["org_number"])
        if normalized is None:
            raise forms.ValidationError(
                "Skriv organisationsnumret med tio siffror, t.ex. 556712-3456."
            )
        return normalized

    def as_quote_fields(self):
        """Fältvärdena med prefixet Quote-modellen använder."""
        data = self.cleaned_data
        return {
            "accept_first_name": data["first_name"].strip(),
            "accept_last_name": data["last_name"].strip(),
            "accept_email": data["email"].strip().lower(),
            "accept_phone": data["phone"].strip(),
            "accept_company": data["company"].strip(),
            "accept_org_number": data["org_number"],
            "accept_billing_address": data["billing_address"].strip(),
            "accept_billing_email": data["billing_email"].strip().lower(),
            "accept_reference": data["reference"].strip(),
            "accept_message": data["message"].strip(),
        }
