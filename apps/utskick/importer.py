"""
Importen av kontakter (README I.7 och J S1 "Import details"): en fil eller
inklistrat, sedan Kolumner, Samtycke och Granska, sedan in i registret.

    start_upload(account, upload, actor)   nytt jobb från en CSV- eller Excel-fil
    start_paste(account, text, actor)      nytt jobb från inklistrade rader (högst 2 000)
    convert(job)                           filen till en normaliserad UTF-8-CSV, en gång
    column_cards(job, defs)                korten i steg 2 (rubrik, exempel, val, förval)
    save_mapping(job, posted, defs)        {} när kolumnerna sparades, annars fel
    save_consent(job, data)                {} när samtycket sparades, annars fel
    analyse(job, deadline=None)            räknar nya, uppdateras, avregistrerade och fel
    begin_import(job, actor, target_list, target_tag)   granska -> importeras
    run_import(job, deadline=None)         raderna in i registret, i omgångar
    run_in_request(job, step)              analysen eller importen i förfrågan, med tidsgräns
    import_chunk(now, seconds)             tickens fas 8: konvertera, analysera, importera
    recover(now)                           tickens fas 1: jobb som en förfrågan lämnat
    work_exists(now)                       har ticken något att göra här?
    delete_account_jobs(account)           alla kontots jobb och filer bort
    errors_csv(job)                        felrapporten, läser om filen
    cancel(job), cleanup(now)              avbryt; städa filer och gamla jobb (utskick_daily)

Gången (ImportJob.status):

    uploaded -> converting -> mapping -> consent -> analysing -> review
             -> importing -> done        (failed och cancelled kan hända var som helst)

Filer och inklistringar med högst IN_REQUEST_ROWS rader konverteras,
analyseras och importeras i förfrågan (in_request). Större filer, och
Excel-filer över INLINE_XLSX_BYTES som behöver konverteras först, går till
ticken (import_chunk) och sidan frågar efter läget (?status=json).

Filerna (README H.3 och E.7): originalet och CSV:n ligger under
PRIVATE_MEDIA_ROOT/utskick-import/ (ImportJob.file:s lagring), aldrig
MEDIA_ROOT. Originalet tas bort så fort CSV:n finns. Kolumner med
personnummer töms redan i CSV:n och i sample. CSV:n tas bort ett dygn efter
klar, misslyckad eller avbruten, och ett jobb som lämnats halvvägs avbryts
efter sju dagar (cleanup). Felrapporten sparar rad, kolumn och orsak, aldrig
värden; csv-filen med felen läser om filen så länge den finns.

Excel öppnas först med zipfile och nekas som zip-bomb (total storlek,
packningsgrad, sharedStrings), och läses sedan av xlsx2csv.py i en egen
process med minnestak och tidsgräns.

Reglerna för raderna (README B.1 och H.6):
- en rad matchas på mobilnummer eller e-post (contacts.match); en rad som
  pekar på två kontakter, eller vars andra adress skiljer sig från en
  ifylld, är en krock och skriver aldrig över något;
- en rad vars alla adresser står på spärrlistan, eller som är markerad som
  avregistrerad i filen, importeras inte och stannar avregistrerad
  (avregistrerade i filen läggs på spärrlistan med orsak import);
- samtycket sätts bara på de kanaler kunden kryssat i, med kundens "Var och
  när?" som bevis, och bara från missing (consent.set_status);
- rader utan mobilnummer och e-post importeras inte (de går inte att
  matcha, så samma fil två gånger skulle ge dubbletter);
- ett värde i ett extrafält som inte duger (fel datum, ett ord i en
  sifferkolumn, för lång text) hoppas över, inte kontakten: raden importeras
  utan värdet och felrapporten säger var (counts["values"]);
- kontaktgränsen gäller under kontaktgränsens lås (limits.lock_contacts),
  och biträdesavtalet prövas före varje omgång (access.can_collect).

Ingenting här skickar något, och loggarna bär bara pk och antal.
"""

import csv
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
import zipfile
from collections import Counter as Tally
from dataclasses import dataclass, field
from datetime import timedelta
from itertools import islice
from pathlib import Path

from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from django.utils.text import slugify

from . import alerts, contacts, keys, limits, normalize, xlsx2csv
from . import consent as consents
from . import suppression as suppressions
from .access import SYSTEM, Actor, can_collect, collect_block_reason, user_label
from .freemail import is_freemail
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    Consent,
    Contact,
    Event,
    FieldDef,
    ImportJob,
    ListMembership,
    Suppression,
)

logger = logging.getLogger(__name__)

S = ImportJob.Status

# ---------------------------------------------------------------------------
# Gränser
# ---------------------------------------------------------------------------

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = xlsx2csv.MAX_ROWS
MAX_COLUMNS = xlsx2csv.MAX_COLUMNS
PASTE_MAX_ROWS = 2_000
#: Så här många rader körs i förfrågan; fler går till ticken.
IN_REQUEST_ROWS = 2_000
#: Längsta tid ett steg får ta i förfrågan. Hinner det inte bli klart (en
#: belastad server) tar ticken resten och sidan väntar som för en stor fil.
IN_REQUEST_SECONDS = 20
#: Excel-filer upp till den här storleken konverteras i förfrågan.
INLINE_XLSX_BYTES = 1024 * 1024
#: Zip-bomben (README J S1).
XLSX_TOTAL_MAX = 100 * 1024 * 1024
XLSX_RATIO_MAX = 100
XLSX_SHARED_STRINGS_MAX = 30 * 1024 * 1024
CHILD_TIMEOUT = 60
#: Analysen sparar sitt läge efter så här många rader.
CHUNK_ROWS = 2_000
#: Importen skriver så här många rader per transaktion (under låset).
BATCH_ROWS = 250
#: Tickens tid för importer per varv (D.2 fas 8).
TICK_SECONDS = 15
ERRORS_MAX = 1_000
SAMPLE_ROWS = 5
#: Så många värden per kolumn används för att gissa vad kolumnen är.
PROFILE_VALUES = 200
#: Så många rader efter rubrikraden avgör filens bredd (_normalize).
WIDTH_ROWS = 50
CELL_MAX = xlsx2csv.CELL_MAX
HEADER_MAX = 60
#: Byrån larmas när fler rader än så importeras som "ja" eller befintliga kunder.
ALERT_ROWS = 1_000
KEEP_FILES = timedelta(hours=24)
ABANDON_AFTER = timedelta(days=7)
KEEP_JOBS = timedelta(days=90)
#: Ett jobb i förfrågan som inte blivit klart på så här lång tid tas över av ticken.
ORPHAN_AFTER = timedelta(minutes=10)
FOLDER = "utskick-import"

USER_STEPS = (S.UPLOADED, S.MAPPING, S.CONSENT, S.REVIEW)
BACKGROUND = (S.CONVERTING, S.ANALYSING, S.IMPORTING)
FINAL = (S.DONE, S.FAILED, S.CANCELLED)

# ---------------------------------------------------------------------------
# Texter
# ---------------------------------------------------------------------------

UNREADABLE_TEXT = "Filen går inte att läsa. Spara den som CSV och försök igen."
OLD_EXCEL_TEXT = "Spara filen som Excel (.xlsx) eller CSV och försök igen."
KIND_TEXT = "Välj en CSV-fil eller en Excel-fil (.xlsx)."
NO_FILE_TEXT = "Välj en fil att ladda upp."
TOO_BIG_TEXT = "Filen är större än 10 MB. Dela upp den eller spara den som CSV."
TOO_MANY_ROWS_TEXT = "Filen har fler än 50 000 rader. Dela upp den i flera filer."
TOO_MANY_COLUMNS_TEXT = (
    "Filen har fler än 100 kolumner. Ta bort kolumnerna du inte behöver och försök igen."
)
PASTE_EMPTY_TEXT = "Klistra in raderna från ditt kalkylark, med rubrikerna överst."
PASTE_TOO_MANY_TEXT = "Klistra in högst 2 000 rader. Ladda upp en fil för fler."
EMPTY_TEXT = "Filen är tom. Första raden ska vara rubriker och resten kontakter."
NO_ROWS_TEXT = "Filen har bara rubriker. Lägg kontakterna på raderna under."
FIELD_LIMIT_TEXT = "Du har 30 extrafält. Fler kolumner hoppas över."
FIELD_LIMIT_ERROR = "Högst 30 extrafält. Välj Hoppa över för några kolumner."
NEED_ADDRESS_TEXT = "Välj vilken kolumn som är mobilnummer eller e-post."
PICK_TEXT = "Välj vad kolumnen blir."
NO_ADDRESS_TEXT = "Raden saknar mobilnummer och e-post."
ROW_FAILED_TEXT = "Raden kunde inte sparas."
VALUE_SKIPPED_TEXT = "Kontakten importeras utan värdet."
FAILED_TEXT = "Importen kunde inte slutföras. Försök igen eller kontakta ADX."
STUCK_TEXT = "Filen hann inte läsas in. Ladda upp den igen."
KEYS_TEXT = "Importen stoppades: spärrlistans nyckel stämmer inte. ADX har fått ett larm."
CONFLICT_TEXTS = {
    "two_contacts": "Numret och e-posten finns på två olika kontakter.",
    "phone_differs": "E-posten finns på en kontakt med ett annat mobilnummer.",
    "email_differs": "Numret finns på en kontakt med en annan e-post.",
}

# ---------------------------------------------------------------------------
# Kolumnerna
# ---------------------------------------------------------------------------

SKIP = "skip"
#: Vad en kolumn kan bli (steg 2), utöver extrafälten.
TARGETS = (
    (SKIP, "Hoppa över"),
    ("full_name", "Fullständigt namn"),
    ("first_name", "Förnamn"),
    ("last_name", "Efternamn"),
    ("phone", "Mobilnummer (+46)"),
    ("email", "E-post"),
    ("org_number", "Organisationsnummer (typ Företag)"),
    ("company_name", "Företagsnamn"),
    ("unsubscribed", "Avregistrerad"),
)
TARGET_LABELS = dict(TARGETS)
#: Mål som bara en kolumn kan ha.
SINGLE_TARGETS = frozenset(key for key, _ in TARGETS if key != SKIP)

#: Rubrikord (gemener, skiljetecken som mellanslag), i den ordning de prövas:
#: personnummer först (tvingas till Hoppa över), företagsnamn före namn.
_HEADER_WORDS = (
    ("pnr", ("personnummer", "personnr", "pnr", "person nr", "personal number", "ssn")),
    (
        "email",
        ("e post", "epost", "email", "e mail", "mail", "mejl", "e postadress", "epostadress"),
    ),
    (
        "org_number",
        (
            "orgnr",
            "org nr",
            "organisationsnummer",
            "organisationsnr",
            "orgnummer",
            "org nummer",
            "org number",
            "organization number",
        ),
    ),
    (
        "company_name",
        ("företag", "foretag", "företagsnamn", "foretagsnamn", "company", "firma", "bolag"),
    ),
    ("first_name", ("förnamn", "fornamn", "first name", "firstname", "given name")),
    ("last_name", ("efternamn", "last name", "lastname", "surname", "family name")),
    (
        "full_name",
        ("namn", "name", "fullständigt namn", "full name", "kontaktperson", "kund", "kundnamn"),
    ),
    (
        "phone",
        (
            "mobil",
            "mobilnummer",
            "mobilnr",
            "mobiltelefon",
            "mobile",
            "cell",
            "telefon",
            "telefonnummer",
            "telnr",
            "tel",
            "tfn",
            "phone",
        ),
    ),
    (
        "unsubscribed",
        ("avregistrerad", "avregistrerade", "unsubscribed", "stopp", "spärrad", "sparrad"),
    ),
)
#: Mål som också känns igen som ett ord i en längre rubrik ("Mobil privat").
#: Namn, företag och avregistrerad bara på hela rubriken: "Kund sedan" är
#: ett datum, inget namn.
_TOKEN_TARGETS = frozenset({"pnr", "email", "org_number", "first_name", "last_name", "phone"})
#: Värden i en Avregistrerad-kolumn som betyder ja.
_TRUTHY = frozenset(
    {"ja", "j", "x", "1", "true", "sant", "yes", "y", "avregistrerad", "stopp", "stop"}
)

CHOICE_CONSENT = "consent"
CHOICE_EXISTING = "existing"
CHOICE_UNKNOWN = "unknown"
CHOICES = (CHOICE_CONSENT, CHOICE_EXISTING, CHOICE_UNKNOWN)
CHOICE_LABELS = {
    CHOICE_CONSENT: "De har sagt ja",
    CHOICE_EXISTING: "De är befintliga kunder",
    CHOICE_UNKNOWN: "Vet inte",
}
_CHOICE_STATUS = {CHOICE_CONSENT: Consent.Status.YES, CHOICE_EXISTING: Consent.Status.EXISTING}


class ImportRefused(ValueError):
    """Filen eller texten går inte att importera; meddelandet är till kunden."""


class _Stop(Exception):
    """Importen ska inte fortsätta (avbruten, eller kontot får inte samla in)."""


# ---------------------------------------------------------------------------
# Filerna
# ---------------------------------------------------------------------------


def _storage():
    """Lagringen för ImportJob.file (PRIVATE_MEDIA_ROOT). CSV:n och
    analysens lägesfil ligger i samma lagring, så att allt städas lika."""
    return ImportJob._meta.get_field("file").storage


def _abs(name):
    return Path(_storage().path(name))


def _new_name(suffix):
    return f"{FOLDER}/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{suffix}"


def _state_name(job):
    return f"{job.csv_path}.state.json" if job.csv_path else ""


def _remove(name):
    if not name:
        return
    try:
        _abs(name).unlink(missing_ok=True)
    except OSError:
        logger.warning("Importens fil kunde inte tas bort (%s)", Path(name).name)


def _delete_files(job, now=None):
    """Ta bort originalet, CSV:n och lägesfilen, och töm sample. Sparar inte."""
    if job.file:
        _remove(job.file.name)
        job.file = ""
    _remove(_state_name(job))
    _remove(job.csv_path)
    job.csv_path = ""
    job.sample = []
    job.file_deleted_at = now or timezone.now()


def _fail(job, text, delete_files=False):
    job.status = S.FAILED
    job.finished_at = timezone.now()
    job.sample = []
    job.counts = {**(job.counts or {}), "failure": text}
    _remove(_state_name(job))
    if delete_files:
        _delete_files(job)
    job.save()
    return job


# ---------------------------------------------------------------------------
# Steg 1: ladda upp eller klistra in
# ---------------------------------------------------------------------------


def _require_collect(account):
    if not can_collect(account):
        raise ImportRefused(collect_block_reason(account) or contacts.COLLECT_TEXT)


def _new_job(account, actor, **fields):
    actor = actor or SYSTEM
    return ImportJob(
        account=account,
        created_by=actor.user,
        created_as_staff=bool(actor.staff),
        status=S.UPLOADED,
        **fields,
    )


def start_upload(account, upload, actor):
    """Ett nytt jobb från en uppladdad fil. Små filer konverteras direkt
    (jobbet står sedan på mapping); en stor Excel-fil går till ticken
    (converting). ImportRefused med en förklaring när det inte går."""
    _require_collect(account)
    if upload is None:
        raise ImportRefused(NO_FILE_TEXT)
    name = os.path.basename(str(upload.name or "")).strip()[:200] or "fil"
    suffix = Path(name).suffix.lower()
    if suffix in (".csv", ".txt", ".tsv"):
        kind = ImportJob.Kind.CSV
    elif suffix == ".xlsx":
        kind = ImportJob.Kind.XLSX
    elif suffix in (".xls", ".xlsm", ".xlsb", ".ods", ".numbers"):
        raise ImportRefused(OLD_EXCEL_TEXT)
    else:
        raise ImportRefused(KIND_TEXT)
    size = int(upload.size or 0)
    if size > MAX_BYTES:
        raise ImportRefused(TOO_BIG_TEXT)
    if size == 0:
        raise ImportRefused(EMPTY_TEXT)
    job = _new_job(account, actor, original_name=name, kind=kind, size=size)
    job.file.save(f"{uuid.uuid4().hex}{suffix}", upload, save=False)
    job.save()
    if kind == ImportJob.Kind.XLSX and size > INLINE_XLSX_BYTES:
        job.status = S.CONVERTING
        job.in_request = False
        job.save(update_fields=["status", "in_request"])
        return job
    convert(job)
    if job.status == S.FAILED:
        raise ImportRefused(job.counts.get("failure") or UNREADABLE_TEXT)
    return job


def start_paste(account, text, actor):
    """Ett nytt jobb från rader som klistrats in från ett kalkylark (tabbar,
    semikolon eller komman), med rubrikerna överst. Högst PASTE_MAX_ROWS."""
    _require_collect(account)
    text = str(text or "")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ImportRefused(PASTE_EMPTY_TEXT)
    if len(lines) - 1 > PASTE_MAX_ROWS:
        raise ImportRefused(PASTE_TOO_MANY_TEXT)
    raw = text.encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ImportRefused(PASTE_TOO_MANY_TEXT)
    job = _new_job(
        account, actor, original_name="Inklistrat", kind=ImportJob.Kind.PASTE, size=len(raw)
    )
    job.file.save(f"{uuid.uuid4().hex}.txt", ContentFile(raw), save=False)
    job.save()
    convert(job)
    if job.status == S.FAILED:
        raise ImportRefused(job.counts.get("failure") or UNREADABLE_TEXT)
    return job


# ---------------------------------------------------------------------------
# Konverteringen: en gång, till en normaliserad CSV
# ---------------------------------------------------------------------------


def convert(job):
    """Läs filen och skriv den normaliserade CSV:n (radnumret först, inga
    radbrytningar i celler, UTF-8). Jobbet går till mapping med gissade
    kolumner, eller till failed med en förklaring. Originalet tas bort."""
    raw_name = ""
    try:
        if job.kind == ImportJob.Kind.XLSX:
            raw_name = _xlsx_to_csv(job)
            rows = _child_rows(_abs(raw_name))
        else:
            rows = _text_rows(job)
        _normalize(job, rows)
    except ImportRefused as exc:
        _fail(job, str(exc), delete_files=True)
        return job
    except Exception:
        logger.exception("Import %s: filen kunde inte konverteras", job.pk)
        _fail(job, UNREADABLE_TEXT, delete_files=True)
        return job
    finally:
        _remove(raw_name)
    if job.file:
        _remove(job.file.name)
        job.file = ""
    job.status = S.MAPPING
    job.mapping = guess_mapping(job, contacts.field_defs(job.account))
    job.save()
    return job


def _xlsx_to_csv(job):
    """Pröva zip-filen och låt xlsx2csv.py läsa den i en egen process.
    Namnet på den råa CSV:n (tas bort efter normaliseringen)."""
    path = _abs(job.file.name)
    check_xlsx(path)
    out_name = _new_name(".raw.csv")
    out = _abs(out_name)
    out.parent.mkdir(parents=True, exist_ok=True)
    script = Path(xlsx2csv.__file__).resolve()
    try:
        result = subprocess.run(  # noqa: S603 - vår egen fil, inga skalord
            [sys.executable, "-I", str(script), str(path), str(out)],
            capture_output=True,
            timeout=CHILD_TIMEOUT,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            check=False,
        )
    except subprocess.TimeoutExpired:
        _remove(out_name)
        raise ImportRefused(UNREADABLE_TEXT) from None
    if result.returncode == xlsx2csv.EXIT_TOO_MANY_ROWS:
        _remove(out_name)
        raise ImportRefused(TOO_MANY_ROWS_TEXT)
    if result.returncode != xlsx2csv.EXIT_OK:
        _remove(out_name)
        logger.info("Import %s: xlsx2csv slutade med %s", job.pk, result.returncode)
        raise ImportRefused(UNREADABLE_TEXT)
    return out_name


def check_xlsx(path):
    """ImportRefused om filen inte är en läsbar xlsx eller ser ut som en
    zip-bomb: över 100 MB uppackat, en del packad mer än 100 gånger, eller
    xl/sharedStrings.xml över 30 MB (README J S1)."""
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
    except (zipfile.BadZipFile, OSError, ValueError):
        raise ImportRefused(UNREADABLE_TEXT) from None
    names = {m.filename for m in members}
    if "xl/workbook.xml" not in names:
        raise ImportRefused(UNREADABLE_TEXT)
    total = 0
    for member in members:
        total += member.file_size
        if member.file_size and (
            member.compress_size == 0 or member.file_size / member.compress_size > XLSX_RATIO_MAX
        ):
            raise ImportRefused(UNREADABLE_TEXT)
        if member.filename == "xl/sharedStrings.xml" and member.file_size > XLSX_SHARED_STRINGS_MAX:
            raise ImportRefused(UNREADABLE_TEXT)
    if total > XLSX_TOTAL_MAX:
        raise ImportRefused(UNREADABLE_TEXT)


def _child_rows(path):
    csv.field_size_limit(1024 * 1024)
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if not row:
                continue
            try:
                number = int(row[0])
            except ValueError:
                continue
            yield number, row[1:]


def decode(raw):
    """Filens text och kodningen den hade: UTF-8 (med eller utan BOM),
    UTF-16 med BOM (Excels Unicode-text), annars cp1252 (Excel på svenska)."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return raw.decode("latin-1"), "latin-1"


def sniff_delimiter(text):
    """Semikolon (Excel på svenska), komma eller tabb (inklistrat): den som
    ger flest rader med samma antal kolumner, och fler än en kolumn."""
    lines = [line for line in text.splitlines() if line.strip()][:50]
    best, best_score = ",", (0, 0)
    for delimiter in (";", ",", "\t"):
        try:
            widths = [len(row) for row in csv.reader(lines, delimiter=delimiter)]
        except csv.Error:
            continue
        if not widths:
            continue
        width, same = Tally(widths).most_common(1)[0]
        if width <= 1:
            continue
        score = (same, width)
        if score > best_score:
            best, best_score = delimiter, score
    return best


def _text_rows(job):
    path = _abs(job.file.name)
    raw = path.read_bytes()[: MAX_BYTES + 1]
    text, encoding = decode(raw)
    text = text.replace("\x00", "")
    delimiter = sniff_delimiter(text)
    job.encoding = encoding
    job.delimiter = delimiter
    csv.field_size_limit(1024 * 1024)

    def rows():
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        try:
            yield from enumerate(reader, start=1)
        except csv.Error:
            raise ImportRefused(UNREADABLE_TEXT) from None

    return rows()


def _clean_cell(value):
    text = str(value or "").replace("\x00", "")
    if any(ch in text for ch in "\r\n\t"):
        text = " ".join(text.split())
    return text.strip()[:CELL_MAX]


def _header_key(name):
    text = str(name or "").lower()
    text = re.sub(r"[\s._\-/:()]+", " ", text).strip()
    return text


def header_target(name):
    """Vad en rubrik betyder: ett mål ur TARGETS, "pnr" (personnummer) eller ""."""
    key = _header_key(name)
    if not key:
        return ""
    for target, words in _HEADER_WORDS:
        if key in words:
            return target
    tokens = f" {key} "
    for target, words in _HEADER_WORDS:
        if target not in _TOKEN_TARGETS:
            continue
        for word in words:
            if len(word) >= 4 and f" {word} " in tokens:
                return target
    return ""


_PHONEISH = re.compile(r"^[+\d][\d\s\-()/.]{5,24}$")
_ORGISH = re.compile(r"^(\d{2})?\d{6}-?\d{4}$")
_NUMBERISH = re.compile(r"^-?(0|[1-9]\d{0,8})([.,]\d+)?$")


def _looks_phone(value):
    return bool(_PHONEISH.match(value)) and bool(normalize.phone(value).e164)


def _looks_email(value):
    if "@" not in value:
        return False
    try:
        return bool(normalize.email(value))
    except normalize.InvalidValue:
        return False


def _looks_org(value):
    if not _ORGISH.match(value.replace(" ", "")):
        return False
    try:
        return bool(normalize.org_number(value).value)
    except normalize.InvalidValue:
        return False


def _looks_date(value):
    try:
        normalize.field_value("date", value)
    except normalize.InvalidValue:
        return False
    return True


class _ColumnStats:
    def __init__(self, name):
        self.name = name
        self.filled = 0
        self.values = []
        self.pnr_header = header_target(name) == "pnr"

    def add(self, value):
        if value:
            self.filled += 1
            if len(self.values) < PROFILE_VALUES:
                self.values.append(value)

    def profile(self):
        values = self.values

        def share(test):
            return sum(1 for v in values if test(v)) / len(values) if values else 0.0

        pnr = self.pnr_header or share(normalize.looks_like_personnummer) > 0.5
        looks = ""
        if values and not pnr:
            if share(_looks_email) >= 0.8:
                looks = "email"
            elif share(_looks_org) >= 0.8:
                looks = "org_number"
            elif share(_looks_phone) >= 0.8:
                looks = "phone"
        kind = FieldDef.Kind.TEXT
        if values and not pnr:
            # Datum och tal bara när alla värden i provet går att läsa så
            # (ett värde som ändå inte duger hoppas över vid importen).
            if all(_looks_date(v) for v in values):
                kind = FieldDef.Kind.DATE
            elif all(_NUMBERISH.match(v) for v in values):
                kind = FieldDef.Kind.NUMBER
        return {"filled": self.filled, "pnr": bool(pnr), "looks": looks, "kind": kind}


def _trimmed(cells):
    while cells and not cells[-1]:
        cells.pop()
    return cells


def _normalize(job, rows):
    """Skriv CSV:n ur rows ((radnummer, celler), rubrikraden först) och fyll
    i header, sample, row_count, counts["columns"] och in_request.

    Bredden är den bredaste av rubrikraden och de första WIDTH_ROWS raderna:
    en kolumn med värden men utan rubrik längst till höger tappas inte, den
    får rubriken "Kolumn N"."""
    out_name = _new_name(".csv")
    out = _abs(out_name)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = None
    stats = []
    width = 0
    count = 0
    sample = []
    pending = []

    def write(writer, number, cells):
        nonlocal count
        cells = (cells + [""] * width)[:width]
        if not any(cells):
            return
        count += 1
        if count > MAX_ROWS:
            raise ImportRefused(TOO_MANY_ROWS_TEXT)
        if job.kind == ImportJob.Kind.PASTE and count > PASTE_MAX_ROWS:
            raise ImportRefused(PASTE_TOO_MANY_TEXT)
        for i, stat in enumerate(stats):
            if stat.pnr_header:
                cells[i] = ""
            stat.add(cells[i])
        writer.writerow([number, *cells])
        if len(sample) < SAMPLE_ROWS:
            sample.append(list(cells))

    def settle(writer):
        """Bredden ur rubriken och raderna som väntar, sedan raderna."""
        nonlocal width, stats
        width = max([len(header), *(len(cells) for _, cells in pending)])
        if width > MAX_COLUMNS:
            raise ImportRefused(TOO_MANY_COLUMNS_TEXT)
        header.extend(f"Kolumn {i + 1}" for i in range(len(header), width))
        stats = [_ColumnStats(name) for name in header]
        for number, cells in pending:
            write(writer, number, cells)
        pending.clear()

    try:
        with open(out, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            for number, raw_cells in rows:
                cells = _trimmed([_clean_cell(c) for c in raw_cells[: MAX_COLUMNS + 1]])
                if header is None:
                    if not cells:
                        continue
                    if len(cells) > MAX_COLUMNS:
                        raise ImportRefused(TOO_MANY_COLUMNS_TEXT)
                    header = [(c[:HEADER_MAX] or f"Kolumn {i + 1}") for i, c in enumerate(cells)]
                    continue
                if stats:
                    write(writer, number, cells)
                    continue
                if cells:
                    pending.append((number, cells))
                if len(pending) >= WIDTH_ROWS:
                    settle(writer)
            if header is not None and not stats:
                settle(writer)
    except BaseException:
        _remove(out_name)
        raise
    if header is None:
        _remove(out_name)
        raise ImportRefused(EMPTY_TEXT)
    if count == 0:
        _remove(out_name)
        raise ImportRefused(NO_ROWS_TEXT)
    profile = [stat.profile() for stat in stats]
    hidden = [i for i, column in enumerate(profile) if column["pnr"]]
    late = [i for i in hidden if not stats[i].pnr_header]
    if late:
        _blank_columns(out, late)
    for row in sample:
        for i in hidden:
            row[i] = ""
    job.csv_path = out_name
    job.header = header
    job.sample = sample
    job.row_count = count
    job.counts = {"columns": profile}
    job.in_request = count <= IN_REQUEST_ROWS
    job.byte_offset = 0
    job.progress = 0
    job.errors = []


def _blank_columns(path, columns):
    """Töm kolumnerna (personnummer som hittades först på värdena) i CSV:n."""
    temp = path.with_name(path.name + ".tmp")
    with (
        open(path, encoding="utf-8", newline="") as src,
        open(temp, "w", encoding="utf-8", newline="") as dst,
    ):
        writer = csv.writer(dst, lineterminator="\n")
        for row in csv.reader(src):
            for i in columns:
                if i + 1 < len(row):
                    row[i + 1] = ""
            writer.writerow(row)
    os.replace(temp, path)


# ---------------------------------------------------------------------------
# Steg 2: kolumnerna
# ---------------------------------------------------------------------------


def _profile(job):
    columns = (job.counts or {}).get("columns") or []
    return [
        columns[i] if i < len(columns) else {"filled": 0, "pnr": False, "looks": "", "kind": "text"}
        for i in range(len(job.header or []))
    ]


def _field_match(name, defs):
    key = _header_key(name)
    if not key:
        return ""
    for definition in defs.values():
        if key in (_header_key(definition.label), _header_key(definition.key)):
            return definition.key
    return ""


def new_field_keys(job, defs):
    """Nyckeln ett nytt extrafält får per kolumn (stabil mellan sidvisningar)."""
    taken = set(defs)
    keys_out = {}
    for i, name in enumerate(job.header or []):
        base = slugify(name)[:36].strip("-") or "falt"
        key, n = base, 2
        while key in taken:
            key = f"{base}-{n}"
            n += 1
        taken.add(key)
        keys_out[i] = key
    return keys_out


def _new_value(kind, key):
    return f"new:{kind}:{key}"


def _phone_column(header, profile):
    """Kolumnen som blir mobilnummer när flera rubriker låter som telefon
    ("Telefon" och "Mobil"): den vars värden ser ut som mobilnummer, annars
    den vars rubrik börjar med mobil, annars den första. None med högst en."""
    claims = [
        i
        for i, name in enumerate(header)
        if header_target(name) == "phone" and not profile[i]["pnr"]
    ]
    if len(claims) < 2:
        return None
    for i in claims:
        if profile[i]["looks"] == "phone":
            return i
    for i in claims:
        if _header_key(header[i]).startswith(("mobil", "mobile", "cell")):
            return i
    return claims[0]


def guess_mapping(job, defs):
    """Förvalen i steg 2: rubriken först, sedan värdena; okända kolumner med
    värden blir nya extrafält (tills 30 är nådda), personnummer hoppas
    alltid över. Låter flera rubriker som telefon blir den med
    mobilnummer mobilnumret (_phone_column), de andra extrafält."""
    profile = _profile(job)
    new_keys = new_field_keys(job, defs)
    room = FieldDef.MAX_PER_ACCOUNT - len(defs)
    header = list(job.header or [])
    phone_column = _phone_column(header, profile)
    used = set()
    mapping = {}
    for i, name in enumerate(header):
        column = profile[i]
        chosen = SKIP
        target = header_target(name)
        if target == "phone" and phone_column is not None and i != phone_column:
            target = ""
        existing = _field_match(name, defs)
        if column["pnr"]:
            chosen = SKIP
        elif target in SINGLE_TARGETS and target not in used:
            chosen = target
        elif existing and f"field:{existing}" not in used:
            chosen = f"field:{existing}"
        elif column["looks"] in SINGLE_TARGETS and column["looks"] not in used:
            chosen = column["looks"]
        elif column["filled"] and room > 0:
            chosen = _new_value(column["kind"], new_keys[i])
            room -= 1
        mapping[str(i)] = chosen
        if chosen != SKIP:
            used.add(chosen)
    return mapping


def column_cards(job, defs):
    """Korten i steg 2, ett per kolumn i filen: {"index", "name", "example",
    "empty", "pnr", "value", "options", "limit"}."""
    profile = _profile(job)
    new_keys = new_field_keys(job, defs)
    room = FieldDef.MAX_PER_ACCOUNT - len(defs)
    mapping = job.mapping or {}
    field_options = [
        (f"field:{d.key}", f"Extrafält: {d.label}")
        for d in sorted(defs.values(), key=lambda d: (d.order, d.pk or 0))
    ]
    cards = []
    for i, name in enumerate(job.header or []):
        column = profile[i]
        options = list(TARGETS)
        if not column["pnr"]:
            if room > 0:
                options.append((_new_value(column["kind"], new_keys[i]), f"Nytt extrafält: {name}"))
            options.extend(field_options)
        value = mapping.get(str(i), SKIP)
        if column["pnr"] or value not in {v for v, _ in options}:
            value = SKIP
        example = ""
        if not column["pnr"]:
            example = next((row[i] for row in job.sample or [] if i < len(row) and row[i]), "")
        cards.append(
            {
                "index": i,
                "name": name,
                "example": example,
                "empty": max(0, job.row_count - column["filled"]),
                "pnr": column["pnr"],
                "value": value,
                "options": options,
                "limit": (
                    value == SKIP and not column["pnr"] and column["filled"] > 0 and room <= 0
                ),
            }
        )
    return cards


def save_mapping(job, posted, defs):
    """Spara valen i steg 2 (posted: {"col-<i>": värde}). {} när de
    sparades och jobbet gick till samtycket, annars fel: {index: text} och
    "__all__" för hela formuläret."""
    cards = column_cards(job, defs)
    errors = {}
    values = {}
    for card in cards:
        raw = str(posted.get(f"col-{card['index']}", SKIP))
        if card["pnr"]:
            raw = SKIP
        if raw not in {v for v, _ in card["options"]}:
            errors[card["index"]] = PICK_TEXT
            raw = SKIP
        values[card["index"]] = raw
    seen = Tally(v for v in values.values() if v != SKIP)
    for index, value in values.items():
        if seen[value] > 1 and index not in errors:
            errors[index] = f"{_value_label(value, job)} är vald för flera kolumner."
    new_count = sum(1 for v in values.values() if v.startswith("new:"))
    if len(defs) + new_count > FieldDef.MAX_PER_ACCOUNT:
        errors["__all__"] = FIELD_LIMIT_ERROR
    if not ({"phone", "email"} & set(values.values())) and "__all__" not in errors:
        errors["__all__"] = NEED_ADDRESS_TEXT
    if errors:
        return errors
    job.mapping = {str(i): v for i, v in values.items()}
    job.status = S.CONSENT
    job.save(update_fields=["mapping", "status"])
    return {}


def _value_label(value, job):
    if value in TARGET_LABELS:
        return TARGET_LABELS[value]
    if value.startswith("field:"):
        return "Extrafältet " + value.split(":", 1)[1]
    return "Extrafältet"


def mapped_channels(job):
    """Kanalerna som filen har en kolumn för: sms (mobilnummer), email."""
    values = set((job.mapping or {}).values())
    pairs = ((CHANNEL_SMS, "phone"), (CHANNEL_EMAIL, "email"))
    return [channel for channel, target in pairs if target in values]


# ---------------------------------------------------------------------------
# Steg 3: samtycket
# ---------------------------------------------------------------------------


def save_consent(job, data):
    """Spara valet i steg 3. data: choice, sms, email, where. {} när det
    sparades (jobbet går till analysen), annars fel per fält."""
    choice = str(data.get("choice") or "")
    where = " ".join(str(data.get("where") or "").split())[:300]
    available = mapped_channels(job)
    ticked = {
        ch: str(data.get(ch) or "") in ("1", "on", "true") and ch in available
        for ch in (CHANNEL_SMS, CHANNEL_EMAIL)
    }
    errors = {}
    if choice not in CHOICES:
        errors["choice"] = "Välj hur kontakterna har sagt ja."
    elif choice != CHOICE_UNKNOWN:
        if not any(ticked.values()):
            errors["channels"] = "Kryssa i vilka kanaler de har sagt ja till."
        if not where:
            errors["where"] = "Skriv var och när de sa ja, till exempel kassan, från 2024."
    if errors:
        return errors
    known = choice != CHOICE_UNKNOWN
    job.consent = {
        "choice": choice,
        "sms": known and ticked[CHANNEL_SMS],
        "email": known and ticked[CHANNEL_EMAIL],
        "where": where if known else "",
    }
    job.status = S.ANALYSING
    job.byte_offset = 0
    job.progress = 0
    job.errors = []
    job.started_at = timezone.now()
    counts = dict(job.counts or {})
    counts.pop("preview", None)
    job.counts = counts
    job.save()
    _remove(_state_name(job))
    return {}


def consent_channels(job):
    consent = job.consent or {}
    return [ch for ch in (CHANNEL_SMS, CHANNEL_EMAIL) if consent.get(ch)]


# ---------------------------------------------------------------------------
# Raderna
# ---------------------------------------------------------------------------


def _rows_from(job, offset=0):
    """(radnummer, celler, byteposition efter raden) ur CSV:n från offset."""
    with open(_abs(job.csv_path), "rb") as fh:
        fh.seek(offset)
        while True:
            line = fh.readline()
            if not line:
                return
            end = fh.tell()
            try:
                row = next(csv.reader([line.decode("utf-8")]))
            except (csv.Error, StopIteration, UnicodeDecodeError):
                continue
            if not row:
                continue
            try:
                number = int(row[0])
            except ValueError:
                continue
            yield number, row[1:], end


class _Plan:
    """job.mapping som det används på varje rad."""

    def __init__(self, job, defs):
        self.header = list(job.header or [])
        self.columns = []
        self.column_of = {}
        for index, target in sorted(
            ((int(k), v) for k, v in (job.mapping or {}).items()), key=lambda kv: kv[0]
        ):
            if target == SKIP or index >= len(self.header):
                continue
            if target.startswith("new:"):
                _, kind, key = target.split(":", 2)
                if key not in defs:
                    defs[key] = FieldDef(
                        account=job.account, key=key, label=self.header[index][:60], kind=kind
                    )
                target = f"field:{key}"
            self.columns.append((index, target))
            self.column_of[target] = index

    def data(self, cells):
        """(data för contacts.clean, avregistrerad i filen)."""
        data = {}
        fields = {}
        unsubscribed = False
        for index, target in self.columns:
            value = cells[index] if index < len(cells) else ""
            if not value:
                continue
            if target == "unsubscribed":
                unsubscribed = _header_key(value) in _TRUTHY
            elif target.startswith("field:"):
                fields[target.split(":", 1)[1]] = value
            else:
                data[target] = value
        if fields:
            data["fields"] = fields
        if data.get("org_number"):
            data["kind"] = Contact.Kind.COMPANY
        return data, unsubscribed

    def column(self, field):
        """Rubriken för ett fält i ContactError (phone, email, field:<nyckel> ...)."""
        candidates = {
            "first_name": ("first_name", "full_name"),
            "last_name": ("last_name", "full_name"),
        }.get(field, (field,))
        for target in candidates:
            if target in self.column_of:
                return self.header[self.column_of[target]]
        return ""

    def address_column(self):
        return self.column("phone") or self.column("email")


@dataclass
class _Row:
    number: int
    cleaned: object = None
    error: tuple = None  # (kolumn, orsak)
    unsubscribed: bool = False
    phone: str = ""
    email: str = ""
    #: Extrafältens värden som hoppades över: [(kolumn, orsak)].
    skipped: list = field(default_factory=list)


def _parse(plan, account, number, cells, defs):
    data, unsubscribed = plan.data(cells)
    row = _Row(number=number, unsubscribed=unsubscribed)
    while True:
        try:
            row.cleaned = contacts.clean(account, data, defs)
        except contacts.ContactError as exc:
            name = exc.field or ""
            key = name.split(":", 1)[1] if name.startswith("field:") else ""
            if key and key in (data.get("fields") or {}):
                # Ett extrafält som inte duger kostar värdet, inte kontakten.
                row.skipped.append((plan.column(name), exc.message))
                del data["fields"][key]
                continue
            row.error = (plan.column(name), exc.message)
        break
    if row.cleaned is not None:
        row.phone, row.email = row.cleaned.phone, row.cleaned.email
    elif unsubscribed:
        # En avregistrering ska gå igenom även om något annat på raden är fel.
        row.phone = normalize.phone(data.get("phone")).e164
        try:
            row.email = normalize.email(data.get("email"))
        except normalize.InvalidValue:
            row.email = ""
    if unsubscribed:
        if row.phone or row.email:
            row.error = None
        elif row.error is None:
            row.error = (plan.address_column(), NO_ADDRESS_TEXT)
    elif row.error is None and not (row.phone or row.email):
        row.error = (plan.address_column(), NO_ADDRESS_TEXT)
    return row


def _addresses(row):
    return [(ch, v) for ch, v in ((CHANNEL_SMS, row.phone), (CHANNEL_EMAIL, row.email)) if v]


def _suppressed_set(account, rows):
    """{(kanal, hash)} som är spärrade bland raderna, två frågor per omgång."""
    out = set()
    for channel in (CHANNEL_SMS, CHANNEL_EMAIL):
        hashes = {
            keys.value_hash(channel, v) for r in rows for ch, v in _addresses(r) if ch == channel
        }
        out |= {(channel, h) for h in suppressions.suppressed_hashes(account, channel, hashes)}
    return out


def _all_suppressed(row, blocked):
    addresses = _addresses(row)
    return bool(addresses) and all((ch, keys.value_hash(ch, v)) in blocked for ch, v in addresses)


def _company_freemail(row, job):
    cleaned = row.cleaned
    if cleaned is None or cleaned.kind != Contact.Kind.COMPANY or not cleaned.org_number:
        return False
    if not cleaned.email or not is_freemail(cleaned.email):
        return False
    return CHANNEL_EMAIL not in consent_channels(job)


def blank_counts():
    return {
        "new": 0,
        "updated": 0,
        "suppressed": 0,
        "marked": 0,
        "errors": 0,
        "conflicts": 0,
        "limit": 0,
        "company_freemail": 0,
        "values": 0,
    }


def _add_error(job, counts, number, column, reason, kind="errors"):
    counts[kind] = counts.get(kind, 0) + 1
    if kind == "limit":
        counts["errors"] = counts.get("errors", 0) + 1
    if len(job.errors) < ERRORS_MAX:
        job.errors.append({"row": number, "column": column, "reason": reason})


def _add_skipped(job, counts, row):
    """Extrafältens värden som hoppades över på en rad som importeras."""
    for column, reason in row.skipped:
        _add_error(job, counts, row.number, column, f"{reason} {VALUE_SKIPPED_TEXT}", "values")


# ---------------------------------------------------------------------------
# Steg 4: analysen (granska)
# ---------------------------------------------------------------------------


class _Index:
    """Kontots adresser i minnet, plus det filen hittills skulle ändra:
    samma regler som contacts.match, utan en fråga per rad."""

    def __init__(self, account, changes):
        self.addr = {}
        self.by_phone = {}
        self.by_email = {}
        rows = Contact.objects.filter(account=account).values_list("pk", "phone", "email")
        for pk, phone, email in rows.iterator(chunk_size=5000):
            self._set(str(pk), phone, email)
        self.changes = dict(changes)
        for cid, (phone, email) in self.changes.items():
            self._set(cid, phone, email)

    def _set(self, cid, phone, email):
        self.addr[cid] = [phone, email]
        if phone:
            self.by_phone[phone] = cid
        if email:
            self.by_email[email] = cid

    def match(self, phone, email):
        by_phone = self.by_phone.get(phone) if phone else None
        by_email = self.by_email.get(email) if email else None
        if by_phone and by_email and by_phone != by_email:
            return None, "two_contacts"
        cid = by_phone or by_email
        if cid is None:
            return None, ""
        known_phone, known_email = self.addr[cid]
        if phone and known_phone and known_phone != phone:
            return cid, "phone_differs"
        if email and known_email and known_email != email:
            return cid, "email_differs"
        return cid, ""

    def add(self, number, phone, email):
        cid = f"f{number}"
        self._set(cid, phone, email)
        self.changes[cid] = [phone, email]

    def fill(self, cid, phone, email):
        known_phone, known_email = self.addr[cid]
        self._set(cid, known_phone or phone, known_email or email)
        self.changes[cid] = self.addr[cid]


def _load_state(job):
    name = _state_name(job)
    if not name or not job.byte_offset:
        return None
    try:
        state = json.loads(_abs(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if state.get("offset") != job.byte_offset:
        return None
    return state


def _save_state(job, state):
    path = _abs(_state_name(job))
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temp, path)


def _preview_defs(job):
    defs = contacts.field_defs(job.account)
    if normalize.LANDLINE_FIELD not in defs:
        # Analysen skapar inget: fältet för fast telefon skapas vid importen.
        defs[normalize.LANDLINE_FIELD] = FieldDef(
            account=job.account,
            key=normalize.LANDLINE_FIELD,
            label=contacts.LANDLINE_LABEL,
            kind=FieldDef.Kind.TEXT,
        )
    return defs


def analyse(job, deadline=None):
    """Räkna vad importen skulle göra (steg 4): nya, uppdateras,
    avregistrerade (stannar avregistrerade), fel och krockar, och företag med
    gratis-e-post. Skriver ingenting i registret. Läget sparas efter varje
    omgång (byte_offset och en lägesfil), så att ticken kan fortsätta.
    True när analysen är klar (jobbet står på review)."""
    if job.status != S.ANALYSING:
        return True
    try:
        keys.require_fingerprints()
    except keys.KeyMismatch:
        _fail(job, KEYS_TEXT)
        return True
    account = job.account
    defs = _preview_defs(job)
    plan = _Plan(job, defs)
    state = _load_state(job)
    if state is None:
        job.byte_offset = 0
        job.progress = 0
        job.errors = []
        state = {"offset": 0, "counts": blank_counts(), "changes": {}, "new": 0}
    counts = state["counts"]
    index = _Index(account, state["changes"])
    room = contacts.room_left(account) - state["new"]
    size = _abs(job.csv_path).stat().st_size
    rows_iter = _rows_from(job, job.byte_offset)
    limit_text = ""
    first = True
    while job.byte_offset < size:
        # Tiden prövas före nästa omgång, inte efter: en fil som tar slut i
        # den här omgången blir klar nu i stället för i nästa tick.
        if not first and deadline is not None and time.monotonic() > deadline:
            return False
        first = False
        batch = list(islice(rows_iter, CHUNK_ROWS))
        if not batch:
            break
        parsed = [_parse(plan, account, number, cells, defs) for number, cells, _ in batch]
        blocked = _suppressed_set(account, parsed)
        for row in parsed:
            if row.unsubscribed and not row.error:
                counts["suppressed"] += 1
                counts["marked"] += 1
                continue
            if row.error:
                _add_error(job, counts, row.number, *row.error)
                continue
            if _all_suppressed(row, blocked):
                counts["suppressed"] += 1
                continue
            cid, conflict = index.match(row.phone, row.email)
            if conflict:
                _add_error(
                    job,
                    counts,
                    row.number,
                    plan.column("phone" if conflict == "phone_differs" else "email"),
                    CONFLICT_TEXTS[conflict],
                    kind="conflicts",
                )
                continue
            if cid is not None:
                counts["updated"] += 1
                index.fill(cid, row.phone, row.email)
            elif room <= 0:
                limit_text = limit_text or _limit_text(account)
                _add_error(job, counts, row.number, "", limit_text, kind="limit")
                continue
            else:
                counts["new"] += 1
                state["new"] += 1
                room -= 1
                index.add(row.number, row.phone, row.email)
            _add_skipped(job, counts, row)
            if _company_freemail(row, job):
                counts["company_freemail"] += 1
        job.byte_offset = batch[-1][2]
        job.progress += len(batch)
        job.counts = {**(job.counts or {}), "preview": counts}
        if not _save_while(job, S.ANALYSING, "byte_offset", "progress", "counts", "errors"):
            return True
        # Efter databasen: en lägesfil som inte hann skrivas har fel offset
        # och gör att analysen börjar om, aldrig att något räknas två gånger.
        state.update(offset=job.byte_offset, counts=counts, changes=index.changes)
        _save_state(job, state)
    job.counts = {**(job.counts or {}), "preview": counts}
    job.byte_offset = 0
    saved = _save_while(job, S.ANALYSING, "byte_offset", "counts", status=S.REVIEW)
    _remove(_state_name(job))
    if not saved:
        job.refresh_from_db()
    return True


def _save_while(job, current, *fields, **changes):
    """Spara fälten (och changes) bara om jobbet fortfarande har statusen
    current, så att en avbruten import inte skrivs över av ticken. False när
    jobbet bytt status under tiden (avbrutet)."""
    values = {name: getattr(job, name) for name in fields}
    values.update(changes)
    updated = ImportJob.objects.filter(pk=job.pk, status=current).update(**values)
    for name, value in changes.items():
        setattr(job, name, value)
    return bool(updated)


def _limit_text(account):
    from .access import settings_for

    limit = f"{int(settings_for(account).contact_limit):,}".replace(",", " ")
    return contacts.LIMIT_TEXT.format(limit=limit)


# ---------------------------------------------------------------------------
# Importen
# ---------------------------------------------------------------------------


def source_detail(job):
    """Var en importerad rad kom ifrån, som samtyckesloggen och tidslinjen
    visar den: filens namn, eller "Inklistrade rader"."""
    if job.kind == ImportJob.Kind.PASTE:
        return "Inklistrade rader"
    return (job.original_name or "Fil")[:200]


def job_actor(job):
    """Vem importen görs av (samtyckesloggen): den som startade importen,
    byrån i kundvyn som "ADX (Giovanni)"."""
    user = job.created_by
    if job.created_as_staff:
        first = ((user.first_name or user.get_username()) if user else "").strip()
        return Actor(user=user, label=(f"ADX ({first})" if first else "ADX")[:120], staff=True)
    if user is None:
        return SYSTEM
    return Actor(user=user, label=user_label(user), staff=False)


def _create_fields(job):
    """De nya extrafälten ur kolumnerna, och mapping med deras nycklar.
    Över 30 fält hoppas resten över."""
    account = job.account
    existing = contacts.field_defs(account)
    order = (FieldDef.objects.filter(account=account).aggregate(m=Max("order"))["m"] or 0) + 1
    mapping = dict(job.mapping or {})
    for index, target in sorted(mapping.items(), key=lambda kv: int(kv[0])):
        if not target.startswith("new:"):
            continue
        _, kind, key = target.split(":", 2)
        if key in existing:
            mapping[index] = f"field:{key}"
            continue
        if len(existing) >= FieldDef.MAX_PER_ACCOUNT:
            mapping[index] = SKIP
            continue
        label = (job.header[int(index)] if int(index) < len(job.header) else key)[:60]
        if kind not in FieldDef.Kind.values:
            kind = FieldDef.Kind.TEXT
        definition, _ = FieldDef.objects.get_or_create(
            account=account, key=key, defaults={"label": label, "kind": kind, "order": order}
        )
        order += 1
        existing[key] = definition
        mapping[index] = f"field:{key}"
    job.mapping = mapping


def begin_import(job, actor, target_list=None, target_tag=None):
    """Granska -> importeras: skapar de nya extrafälten och sparar lista och
    tagg. False om jobbet inte stod på review (dubbelklick, annan flik).
    Byrån larmas när fler än ALERT_ROWS rader importeras som ja eller
    befintliga kunder."""
    actor = actor or SYSTEM
    with transaction.atomic():
        locked = ImportJob.objects.select_for_update().filter(pk=job.pk, status=S.REVIEW).first()
        if locked is None:
            job.refresh_from_db()
            return False
        _create_fields(locked)
        locked.target_list = target_list
        locked.target_tag = target_tag
        locked.created_by = actor.user
        locked.created_as_staff = bool(actor.staff)
        locked.status = S.IMPORTING
        locked.byte_offset = 0
        locked.progress = 0
        locked.errors = []
        locked.started_at = timezone.now()
        locked.counts = {
            k: v for k, v in (locked.counts or {}).items() if k in ("columns", "preview")
        } | blank_counts()
        locked.save()
    job.refresh_from_db()
    choice = (job.consent or {}).get("choice")
    if job.row_count > ALERT_ROWS and choice in _CHOICE_STATUS:
        _alert_large(job)
    return True


def _alert_large(job):
    account = job.account
    name = account.customer.name if account.customer_id else f"konto {account.pk}"
    channels = " och ".join(
        "sms" if ch == CHANNEL_SMS else "e-post" for ch in consent_channels(job)
    )
    alerts.agency(
        f"Utskick: import av {job.row_count} kontakter med samtycke ({name})",
        [
            f"Kund: {name} (konto {account.pk}).",
            f"Import {job.pk}: {job.row_count} rader.",
            f"Valt samtycke: {CHOICE_LABELS[job.consent['choice']]}, kanaler: {channels}.",
            f"Var och när enligt kunden: {job.consent.get('where', '')}",
            "Startad av ADX i kundvyn." if job.created_as_staff else "Startad av kunden.",
        ],
        once=f"import-{job.pk}",
    )


def run_import(job, deadline=None):
    """Importera raderna från byte_offset, BATCH_ROWS per transaktion under
    kontaktgränsens lås. Avbruten import slutar vid nästa omgång. True när
    jobbet inte längre har något kvar (klart, misslyckat eller avbrutet)."""
    if job.status != S.IMPORTING:
        return True
    try:
        keys.require_fingerprints()
        while True:
            more = _import_batch(job)
            if not more:
                break
            if deadline is not None and time.monotonic() > deadline:
                return False
    except _Stop:
        job.refresh_from_db()
        return True
    except keys.KeyMismatch:
        job.refresh_from_db()
        _fail(job, KEYS_TEXT)
        return True
    _finish(job)
    return True


def _finish(job):
    with transaction.atomic():
        locked = ImportJob.objects.select_for_update().filter(pk=job.pk, status=S.IMPORTING).first()
        if locked is None:
            job.refresh_from_db()
            return
        locked.status = S.DONE
        locked.finished_at = timezone.now()
        locked.sample = []
        locked.save()
    job.refresh_from_db()
    logger.info(
        "Import %s klar: %s nya, %s uppdaterade, %s fel",
        job.pk,
        job.counts.get("new", 0),
        job.counts.get("updated", 0),
        job.counts.get("errors", 0) + job.counts.get("conflicts", 0),
    )


def _import_batch(job):
    """En omgång. False när filen är slut (också när den tog slut nu)."""
    account = job.account
    if not can_collect(account):
        # Utskick avstängt eller ett nytt biträdesavtal att godkänna: det
        # som redan importerats ligger kvar, resten importeras inte.
        job.refresh_from_db()
        if job.status == S.IMPORTING:
            _fail(job, collect_block_reason(account) or contacts.COLLECT_TEXT)
        raise _Stop
    now = timezone.now()
    with transaction.atomic():
        locked = (
            ImportJob.objects.select_for_update(of=("self",))
            .select_related("account", "target_list", "target_tag", "created_by")
            .filter(pk=job.pk, status=S.IMPORTING)
            .first()
        )
        if locked is None:
            raise _Stop
        limits.lock_contacts(account)
        batch = list(islice(_rows_from(locked, locked.byte_offset), BATCH_ROWS))
        if not batch:
            return False
        defs = contacts.field_defs(account)
        plan = _Plan(locked, defs)
        actor = job_actor(locked)
        detail = source_detail(locked)
        counts = result(locked)
        parsed = [_parse(plan, account, number, cells, defs) for number, cells, _ in batch]
        blocked = _suppressed_set(account, parsed)
        touched = []
        for row in parsed:
            contact = _import_row(locked, plan, row, blocked, counts, actor, detail, defs, now)
            if contact is not None:
                touched.append(contact.pk)
        if touched and locked.target_list_id:
            contacts.add_to_list(locked.target_list, touched, source=ListMembership.Source.IMPORT)
        if touched and locked.target_tag_id:
            contacts.add_tag(locked.target_tag, touched)
        locked.byte_offset = batch[-1][2]
        locked.progress += len(batch)
        locked.counts = {**locked.counts, **counts}
        locked.save(update_fields=["byte_offset", "progress", "counts", "errors"])
    job.refresh_from_db()
    return job.byte_offset < _abs(job.csv_path).stat().st_size


def _import_row(job, plan, row, blocked, counts, actor, detail, defs, now):
    """En rad in i registret, i en egen sparpunkt (ett fel på raden rullar
    bara tillbaka raden). Kontakten som skapades eller uppdaterades, annars
    None (fel, krock, avregistrerad)."""
    if row.error:
        _add_error(job, counts, row.number, *row.error)
        return None
    if not row.unsubscribed and _all_suppressed(row, blocked):
        counts["suppressed"] += 1
        return None
    try:
        with transaction.atomic():
            if row.unsubscribed:
                _suppress_row(job, row, actor, detail, now)
                kind, contact = "suppressed", None
            else:
                kind, contact = _write_row(job, plan, row, counts, actor, detail, defs, now)
    except contacts.ContactLimitReached as exc:
        _add_error(job, counts, row.number, "", exc.message, kind="limit")
        return None
    except contacts.ContactError as exc:
        _add_error(job, counts, row.number, plan.column(exc.field or ""), exc.message)
        return None
    except keys.KeyMismatch:
        raise
    except Exception:
        logger.exception("Import %s: rad %s kunde inte sparas", job.pk, row.number)
        _add_error(job, counts, row.number, "", ROW_FAILED_TEXT)
        return None
    if kind == "suppressed":
        counts["suppressed"] += 1
        counts["marked"] += 1
        return None
    if contact is None:
        return None
    counts[kind] += 1
    _add_skipped(job, counts, row)
    if _company_freemail(row, job):
        counts["company_freemail"] += 1
    return contact


def _suppress_row(job, row, actor, detail, now):
    """En rad som är avregistrerad i filen: varje adress på spärrlistan
    (orsak import), och kontakten som har adressen blir avregistrerad.
    Ingen ny kontakt skapas för den."""
    for channel, value in _addresses(row):
        suppressions.suppress(
            job.account,
            channel,
            value,
            Suppression.Reason.IMPORT,
            Consent.Source.IMPORT,
            source_detail=detail,
            actor=actor,
            now=now,
        )


def _write_row(job, plan, row, counts, actor, detail, defs, now):
    """Matcha, skapa eller fyll i, sätt samtycket och skriv händelsen.
    (sort, kontakt); kontakten är None vid en krock."""
    account = job.account
    match = contacts.match(account, row.phone, row.email)
    if match.conflict:
        _add_error(
            job,
            counts,
            row.number,
            plan.column("phone" if match.conflict == "phone_differs" else "email"),
            CONFLICT_TEXTS[match.conflict],
            kind="conflicts",
        )
        return "conflicts", None
    if match.contact is not None:
        contact = match.contact
        contacts.fill_from(contact, row.cleaned, actor=actor, now=now)
        kind = "updated"
    else:
        contact = contacts.create(
            account,
            {},
            source=Contact.Source.IMPORT,
            actor=actor,
            source_detail=detail,
            check_collect=False,
            cleaned=row.cleaned,
            defs=defs,
            now=now,
        )
        kind = "new"
    status = _CHOICE_STATUS.get((job.consent or {}).get("choice"))
    if status:
        for channel in consent_channels(job):
            if contact.address(channel):
                consents.set_status(
                    contact,
                    channel,
                    status,
                    source=Consent.Source.IMPORT,
                    actor=actor,
                    source_detail=detail,
                    evidence=job.consent.get("where", ""),
                    now=now,
                )
    # En uppdatering från kundens fil är inget personen gjort: den räknas
    # inte som aktivitet (annars flaggas ingen inaktiv kontakt som finns i
    # en fil som importeras om, E.7).
    contacts.record_event(
        contact,
        Event.IMPORTED,
        {"import": job.pk, "fil": detail[:100]},
        at=now,
        activity=kind == "new",
    )
    return kind, contact


def run_in_request(job, step):
    """Kör analysen eller importen (step) i förfrågan för ett litet jobb,
    högst IN_REQUEST_SECONDS. Det som inte hann bli klart lämnas till ticken
    (in_request blir False) och sidan väntar. True när steget är klart."""
    finished = step(job, deadline=time.monotonic() + IN_REQUEST_SECONDS)
    if not finished:
        ImportJob.objects.filter(pk=job.pk, status__in=BACKGROUND).update(in_request=False)
        job.refresh_from_db()
    return finished


# ---------------------------------------------------------------------------
# Ticken (D.2 fas 8)
# ---------------------------------------------------------------------------


def _orphans(now):
    return Q(status__in=BACKGROUND, in_request=True, started_at__lt=now - ORPHAN_AFTER)


def _stuck_uploads(now):
    """Jobb som fastnat på uploaded: förfrågan som läste in filen dog (en
    Excel-fil får ta en minut i barnprocessen). Ingen annan tar över dem."""
    return Q(status=S.UPLOADED, created_at__lt=now - ORPHAN_AFTER)


def recover(now=None):
    """Tickens fas 1 (D.2): ett jobb som en förfrågan började men inte
    blev klar med på tio minuter (arbetaren dog eller tog slut på tid) tas
    över av ticken; ett jobb som fastnat på uploaded misslyckas och filen
    tas bort (kunden laddar upp igen). Antal jobb."""
    now = now or timezone.now()
    taken = ImportJob.objects.filter(_orphans(now)).update(in_request=False)
    stuck = list(ImportJob.objects.filter(_stuck_uploads(now)).values_list("pk", flat=True)[:50])
    for pk in stuck:
        with transaction.atomic():
            job = ImportJob.objects.select_for_update().filter(pk=pk, status=S.UPLOADED).first()
            if job is None:
                continue
            _fail(job, STUCK_TEXT, delete_files=True)
            taken += 1
            logger.warning("Import %s fastnade när filen lästes in och stoppades", pk)
    return taken


def work_exists(now=None):
    """Har ticken något att göra med importer? En fråga."""
    now = now or timezone.now()
    return ImportJob.objects.filter(
        Q(status__in=BACKGROUND, in_request=False) | _orphans(now) | _stuck_uploads(now)
    ).exists()


def _claim(skip):
    with transaction.atomic():
        return (
            ImportJob.objects.select_for_update(skip_locked=True, of=("self",))
            .filter(status__in=BACKGROUND, in_request=False)
            .exclude(pk__in=skip)
            .select_related("account", "account__customer")
            .order_by("created_at", "pk")
            .first()
        )


def import_chunk(now=None, seconds=TICK_SECONDS):
    """Tickens fas 8: konvertera, analysera och importera stora filer tills
    tiden (seconds) är slut. Ett jobb i förfrågan som inte blev klart tas
    över. Returnerar {"jobs", "rows"} (antal, inga värden)."""
    now = now or timezone.now()
    deadline = time.monotonic() + seconds
    ImportJob.objects.filter(_orphans(now)).update(in_request=False)
    summary = {"jobs": 0, "rows": 0}
    seen = []
    while time.monotonic() < deadline:
        job = _claim(seen)
        if job is None:
            break
        seen.append(job.pk)
        summary["jobs"] += 1
        before = job.progress
        try:
            if job.status == S.CONVERTING:
                if not can_collect(job.account):
                    _fail(job, collect_block_reason(job.account), delete_files=True)
                else:
                    convert(job)
            elif job.status == S.ANALYSING:
                analyse(job, deadline)
            elif job.status == S.IMPORTING:
                run_import(job, deadline)
        except keys.KeyMismatch:
            logger.error("Import %s väntar: nycklarna stämmer inte", job.pk)
            break
        except Exception:
            logger.exception("Import %s misslyckades i ticken", job.pk)
            job.refresh_from_db()
            if job.status not in FINAL:
                _fail(job, FAILED_TEXT)
        summary["rows"] += max(0, job.progress - before)
    return summary


# ---------------------------------------------------------------------------
# Felrapporten, avbryt och städning
# ---------------------------------------------------------------------------


def errors_csv(job):
    """Felen som CSV (semikolon, UTF-8 med BOM för Excel): rad, kolumn,
    orsak och, så länge filen finns kvar, radens värden ur filen."""
    from apps.flamingo.exports import safe_cell

    wanted = {e["row"] for e in job.errors or []}
    values = {}
    if wanted and job.csv_path and _abs(job.csv_path).exists():
        for number, cells, _ in _rows_from(job):
            if number in wanted:
                values[number] = cells
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    head = ["Rad", "Kolumn", "Orsak"]
    if values:
        head += list(job.header or [])
    # Rubrikerna kommer ur kundens fil: samma skydd som cellerna.
    writer.writerow([safe_cell(cell) for cell in head])
    for error in job.errors or []:
        line = [error.get("row", ""), error.get("column", ""), error.get("reason", "")]
        if values:
            line += values.get(error.get("row"), [])
        writer.writerow([safe_cell(cell) for cell in line])
    total = error_total(job)
    if total > len(job.errors or []):
        writer.writerow(["", "", f"Visar de första {len(job.errors)} raderna av {total}."])
    return buffer.getvalue().encode("utf-8-sig")


def error_total(job):
    """Raderna i felrapporten: fel, krockar och överhoppade värden."""
    counts = job.counts or {}
    if job.status in (S.REVIEW, S.ANALYSING):
        counts = counts.get("preview") or {}
    return sum(int(counts.get(kind, 0)) for kind in ("errors", "conflicts", "values"))


def cancel(job):
    """Avbryt jobbet (alla steg utom de färdiga). Filerna tas bort direkt.
    Det som redan importerats ligger kvar."""
    with transaction.atomic():
        locked = ImportJob.objects.select_for_update().filter(pk=job.pk).first()
        if locked is None or locked.status in FINAL:
            return False
        locked.status = S.CANCELLED
        locked.finished_at = timezone.now()
        _delete_files(locked)
        locked.save()
    job.refresh_from_db()
    return True


def delete_account_jobs(account):
    """Alla kontots importer bort, med filerna (byråns "Avsluta utskick och
    radera allt" och demots återställning). Antal jobb."""
    jobs = list(ImportJob.objects.filter(account=account))
    for job in jobs:
        _delete_files(job)
    ImportJob.objects.filter(pk__in=[job.pk for job in jobs]).delete()
    return len(jobs)


def cleanup(now=None):
    """utskick_daily (E.7): filerna bort ett dygn efter klar, misslyckad
    eller avbruten; jobb som lämnats halvvägs avbryts efter sju dagar; jobb
    äldre än 90 dagar tas bort; filer i mappen som inget jobb känner till
    tas bort efter åtta dagar. Returnerar antal per sort."""
    now = now or timezone.now()
    summary = {"abandoned": 0, "files": 0, "jobs": 0, "orphans": 0}
    stale = ImportJob.objects.filter(
        status__in=USER_STEPS + BACKGROUND, created_at__lt=now - ABANDON_AFTER
    )
    for job in stale:
        if cancel(job):
            summary["abandoned"] += 1
    finished = ImportJob.objects.filter(status__in=FINAL, file_deleted_at__isnull=True).filter(
        Q(finished_at__lt=now - KEEP_FILES)
        | Q(finished_at__isnull=True, created_at__lt=now - KEEP_FILES)
    )
    for job in finished:
        _delete_files(job, now)
        job.save(update_fields=["file", "csv_path", "sample", "file_deleted_at"])
        summary["files"] += 1
    old = ImportJob.objects.filter(created_at__lt=now - KEEP_JOBS)
    for job in old:
        _delete_files(job, now)
    summary["jobs"], _ = old.delete()
    summary["orphans"] = _remove_orphan_files(now)
    return summary


def _remove_orphan_files(now):
    root = _abs(FOLDER)
    if not root.is_dir():
        return 0
    known = set()
    for file_name, csv_path in ImportJob.objects.values_list("file", "csv_path"):
        for name in (file_name, csv_path):
            if name:
                known.add(str(_abs(name)))
                known.add(str(_abs(name)) + ".state.json")
    limit = (now - ABANDON_AFTER - timedelta(days=1)).timestamp()
    removed = 0
    for path in root.rglob("*"):
        if not path.is_file() or str(path) in known:
            continue
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# För vyerna
# ---------------------------------------------------------------------------


def preview(job):
    return {**blank_counts(), **((job.counts or {}).get("preview") or {})}


def result(job):
    counts = job.counts or {}
    return {**blank_counts(), **{k: v for k, v in counts.items() if k in blank_counts()}}


def failure_text(job):
    return (job.counts or {}).get("failure", "")


def suggested_tag_name(now=None):
    """ "Import okt 2026": förslaget på tagg i steg 4."""
    months = ("jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec")
    local = timezone.localtime(now or timezone.now())
    return f"Import {months[local.month - 1]} {local.year}"
