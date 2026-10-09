"""
E-post från utskick (README F och D.6). S1: bara bekräftelsemejlet
(dubbel opt-in) genom transport.send(kind="doi"); byggaren, renderaren och
kontrollerna kommer i S3.

    mime.py        rå MIME (stdlib email.message bara här)
    transport.py   SES v2 i eu-west-1, .eml-filer lokalt, FakeSes i testerna

Mappen heter email men skuggar inte standardbibliotekets email: importerna
är absoluta (from email.message import ... är alltid standardbiblioteket).
"""
