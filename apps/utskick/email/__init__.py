"""
E-post från utskick (README F och D.6). S1: bara bekräftelsemejlet
(dubbel opt-in) genom transport.send(kind="doi"). S3: Brev, renderaren,
kontrollerna och domänerna (ägarna i S3-HANDOFF.md).

    mime.py        rå MIME (stdlib email.message bara här), List-Unsubscribe
    transport.py   SES v2 i eu-west-1, .eml-filer lokalt, FakeSes i testerna
    registry.py    Brevs 24 element (F.1)
    blocks.py      blockens validering, fältsorterna och sparningen (F.1 till F.3)
    style.py       accentfärgen och inline-stilarna (F.2, F.4)
    render.py      HTML i lägena editor, preview och send (F.4)
    text.py        textversionen ur samma block (F.4)
    images.py      bilderna i mejlens format (F.4, C.2)
    checks.py      mejlets kontroller i redigeraren och Granska (F.5)
    domains.py     kundernas avsändardomäner (B.3)

Mappen heter email men skuggar inte standardbibliotekets email: importerna
är absoluta (from email.message import ... är alltid standardbiblioteket).
"""
