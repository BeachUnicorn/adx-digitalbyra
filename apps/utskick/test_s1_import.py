"""
Importen av kontakter (README J S1 "Import details" och I.7).

Filerna skrivs till en tillfällig mapp (ImportJob.file:s lagring byts ut i
setUp), aldrig till utvecklarens PRIVATE_MEDIA_ROOT. Inget här når nätet;
Excel-filerna läses av xlsx2csv.py i en egen process precis som i drift.
Där ticken (import_chunk) eller förfrågan importerar går budgeten på
testklockan (testing.TickClock), inte på maskinens last.
"""

import csv
import datetime
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core import mail
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import consent, importer, keys, suppression, xlsx2csv
from .access import SYSTEM, Actor
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    Consent,
    ConsentLog,
    Contact,
    ContactList,
    DpaAcceptance,
    DpaVersion,
    Event,
    FieldDef,
    ImportJob,
    Suppression,
    Tag,
)
from .testing import (
    PHONE_ANNA,
    PHONE_BO,
    PHONE_CILLA,
    OnTickClock,
    UtskickFixture,
    make_contact,
    tick_clock,
)

S = ImportJob.Status
MALL = Path(settings.BASE_DIR) / "static" / "utskick" / "kontakter-mall.xlsx"


def pnr(yymmdd, serial="123"):
    """Ett personnummer med rätt kontrollsiffra (Luhn), för testerna."""
    digits = yymmdd + serial
    total = 0
    for i, ch in enumerate(digits):
        n = int(ch) * (2 if i % 2 == 0 else 1)
        total += n - 9 if n > 9 else n
    return f"{yymmdd}-{serial}{(10 - total % 10) % 10}"


def phone(n):
    """Fiktiva mobilnummer i PTS-serien 070-174 xx xx."""
    return f"070174{n:04d}"


def csv_text(header, rows, delimiter=";"):
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def xlsx_bytes(header, rows):
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class ImportFiles(UtskickFixture):
    """Lagringen i en tillfällig mapp, och hjälp för flödet genom vyerna."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="utskick-import-test-")
        field = ImportJob._meta.get_field("file")
        patcher = mock.patch.object(field, "storage", FileSystemStorage(location=self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.actor = Actor(user=self.anna, label="Anna Lindqvist")

    def files(self):
        return sorted(p.name for p in Path(self.tmp).rglob("*") if p.is_file())

    def upload(self, content, name="kunder.csv", account=None):
        account = account or self.account
        upload = SimpleUploadedFile(name, content, content_type="application/octet-stream")
        return importer.start_upload(account, upload, self.actor)

    def job_url(self, job):
        return reverse("flamingo:app_import_job", args=[job.pk])

    def flow(
        self,
        text,
        choice="unknown",
        sms=False,
        email=False,
        where="",
        client=None,
        columns=None,
        **form,
    ):
        """Hela vägen genom vyerna: ladda upp, förvalda kolumner (columns
        ändrar några: {"2": "new:date:besok"}), samtycke, importera.
        Returnerar jobbet."""
        client = client or self.client_for(self.anna)
        upload = SimpleUploadedFile("kunder.csv", text.encode("utf-8"), content_type="text/csv")
        response = client.post(reverse("flamingo:app_import"), {"action": "upload", "file": upload})
        self.assertEqual(response.status_code, 302, response.content[:400])
        job = ImportJob.objects.filter(account=self.account).latest("pk")
        url = self.job_url(job)
        mapping = {f"col-{k}": v for k, v in {**job.mapping, **(columns or {})}.items()}
        self.assertEqual(client.post(url, {"action": "map", **mapping}).status_code, 302)
        data = {"action": "consent", "choice": choice, "where": where}
        if sms:
            data["sms"] = "1"
        if email:
            data["email"] = "1"
        self.assertEqual(client.post(url, data).status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.status, S.REVIEW)
        self.review = client.get(url).content.decode()
        response = client.post(url, {"action": "import", "list": "", "tag": "", **form})
        self.assertEqual(response.status_code, 302)
        job.refresh_from_db()
        return job


# ---------------------------------------------------------------------------
# Steg 1: filen läses en gång till en normaliserad CSV
# ---------------------------------------------------------------------------


class ConvertTests(ImportFiles, TestCase):
    def test_swedish_excel_csv_cp1252_semicolon(self):
        text = "Namn;Mobil;E-post\r\nÅsa Öberg;070-174 06 01;asa@exempel.example\r\n"
        job = self.upload(text.encode("cp1252"))
        self.assertEqual(job.status, S.MAPPING)
        self.assertEqual((job.encoding, job.delimiter), ("cp1252", ";"))
        self.assertEqual(job.header, ["Namn", "Mobil", "E-post"])
        self.assertEqual(job.sample[0][0], "Åsa Öberg")
        self.assertEqual(job.mapping, {"0": "full_name", "1": "phone", "2": "email"})
        self.assertTrue(job.in_request)
        # Originalet är borta så fort CSV:n finns; CSV:n ligger i den privata lagringen.
        self.assertFalse(job.file)
        self.assertTrue((Path(self.tmp) / job.csv_path).is_file())
        self.assertTrue(job.csv_path.startswith("utskick-import/"))

    def test_english_csv_with_bom_and_commas(self):
        text = "﻿First name,Last name,Phone,Email\nBo,Ek,+46701740602,bo@exempel.example\n"
        job = self.upload(text.encode("utf-8"))
        self.assertEqual((job.encoding, job.delimiter), ("utf-8-sig", ","))
        self.assertEqual(job.header[0], "First name")
        self.assertEqual(
            job.mapping, {"0": "first_name", "1": "last_name", "2": "phone", "3": "email"}
        )

    def test_utf16_from_excel_unicode_text(self):
        text = "Namn\tMobil\nCilla Berg\t0701740603\n"
        job = self.upload(text.encode("utf-16"), name="kunder.txt")
        self.assertEqual((job.encoding, job.delimiter), ("utf-16", "\t"))
        self.assertEqual(job.mapping["1"], "phone")

    def test_paste_with_tabs(self):
        job = importer.start_paste(
            self.account, "Förnamn\tMobil\nAnna\t070-174 06 01\n", self.actor
        )
        self.assertEqual(job.kind, ImportJob.Kind.PASTE)
        self.assertEqual(job.delimiter, "\t")
        self.assertEqual(job.row_count, 1)
        self.assertEqual(job.original_name, "Inklistrat")

    def test_paste_is_limited_to_2000_rows(self):
        rows = "\n".join(f"Person {i}\t{phone(i)}" for i in range(2001))
        with self.assertRaisesMessage(importer.ImportRefused, importer.PASTE_TOO_MANY_TEXT):
            importer.start_paste(self.account, "Namn\tMobil\n" + rows, self.actor)
        self.assertFalse(ImportJob.objects.exists())
        rows = "\n".join(f"Person {i}\t{phone(i)}" for i in range(2000))
        job = importer.start_paste(self.account, "Namn\tMobil\n" + rows, self.actor)
        self.assertEqual(job.row_count, 2000)
        self.assertTrue(job.in_request)

    def test_rows_are_numbered_like_the_spreadsheet_and_empty_rows_skipped(self):
        text = "\n\nNamn;Mobil\nAnna;0701740601\n;\nBo;0701740602\n"
        job = self.upload(text.encode())
        self.assertEqual(job.row_count, 2)
        numbers = [n for n, _, _ in importer._rows_from(job)]
        self.assertEqual(numbers, [4, 6])

    def test_newlines_in_cells_are_flattened(self):
        text = 'Namn;Kommentar\nAnna;"Ring efter\nlunch"\n'
        job = self.upload(text.encode())
        rows = list(importer._rows_from(job))
        self.assertEqual(rows[0][1], ["Anna", "Ring efter lunch"])

    def test_in_request_up_to_2000_rows_then_the_tick(self):
        rows = [[f"Person {i}", phone(i)] for i in range(2001)]
        small = self.upload(csv_text(["Namn", "Mobil"], rows[:2000]).encode())
        self.assertTrue(small.in_request)
        large = self.upload(csv_text(["Namn", "Mobil"], rows).encode())
        self.assertFalse(large.in_request)
        self.assertEqual(large.row_count, 2001)

    def test_refuses_more_than_50000_rows(self):
        lines = ["Mobil"] + [phone(i % 10000) for i in range(50_001)]
        with self.assertRaisesMessage(importer.ImportRefused, importer.TOO_MANY_ROWS_TEXT):
            self.upload("\n".join(lines).encode())
        job = ImportJob.objects.get()
        self.assertEqual(job.status, S.FAILED)
        self.assertEqual(self.files(), [])

    def test_refuses_big_files_old_excel_and_unknown_types(self):
        with mock.patch.object(importer, "MAX_BYTES", 10):
            with self.assertRaisesMessage(importer.ImportRefused, importer.TOO_BIG_TEXT):
                self.upload(b"Namn;Mobil\nAnna;0701740601\n")
        with self.assertRaisesMessage(importer.ImportRefused, importer.OLD_EXCEL_TEXT):
            self.upload(b"x", name="kunder.xls")
        with self.assertRaisesMessage(importer.ImportRefused, importer.KIND_TEXT):
            self.upload(b"x", name="kunder.pdf")
        with self.assertRaisesMessage(importer.ImportRefused, importer.NO_ROWS_TEXT):
            self.upload(b"Namn;Mobil\n")
        self.assertFalse(ImportJob.objects.exclude(status=S.FAILED).exists())
        self.assertEqual(self.files(), [])

    def test_too_many_columns(self):
        header = [f"Kolumn {i}" for i in range(101)]
        with self.assertRaisesMessage(importer.ImportRefused, importer.TOO_MANY_COLUMNS_TEXT):
            self.upload(csv_text(header, [["x"] * 101]).encode())


class XlsxTests(ImportFiles, TestCase):
    def test_xlsx_is_read_in_a_child_process(self):
        content = xlsx_bytes(
            ["Namn", "Mobil", "Senaste besök"],
            [["Anna Lindqvist", 701740601, datetime.date(2025, 11, 4)], [None, None, None]],
        )
        real_run = subprocess.run
        with mock.patch.object(importer.subprocess, "run", side_effect=real_run) as run:
            job = self.upload(content, name="kunder.xlsx")
        args = run.call_args.args[0]
        self.assertEqual(args[1], "-I")
        self.assertTrue(args[2].endswith("apps/utskick/xlsx2csv.py"))
        self.assertEqual(run.call_args.kwargs["timeout"], importer.CHILD_TIMEOUT)
        self.assertNotIn("SECRET_KEY", run.call_args.kwargs["env"])
        self.assertEqual(job.status, S.MAPPING)
        self.assertEqual(job.sample, [["Anna Lindqvist", "701740601", "2025-11-04"]])
        self.assertEqual(job.mapping["1"], "phone")
        self.assertEqual(job.mapping["2"], "new:date:senaste-besok")
        # Bara CSV:n finns kvar (inte originalet och inte den råa CSV:n).
        self.assertEqual(self.files(), [Path(job.csv_path).name])

    def test_cell_text(self):
        self.assertEqual(xlsx2csv.cell_text(701740601.0), "701740601")
        self.assertEqual(xlsx2csv.cell_text(True), "ja")
        self.assertEqual(
            xlsx2csv.cell_text(datetime.datetime(2025, 1, 2, 9, 30)), "2025-01-02 09:30"
        )
        self.assertEqual(xlsx2csv.cell_text("Rad ett\nrad två"), "Rad ett rad två")
        self.assertEqual(len(xlsx2csv.cell_text("x" * 5000)), xlsx2csv.CELL_MAX)

    def test_large_xlsx_is_converted_by_the_tick(self):
        content = xlsx_bytes(["Mobil"], [[phone(1)], [phone(2)]])
        with mock.patch.object(importer, "INLINE_XLSX_BYTES", 10):
            job = self.upload(content, name="stor.xlsx")
        self.assertEqual((job.status, job.in_request), (S.CONVERTING, False))
        self.assertTrue(importer.work_exists())
        with tick_clock():
            summary = importer.import_chunk()
        job.refresh_from_db()
        self.assertEqual(job.status, S.MAPPING)
        self.assertEqual((summary["jobs"], job.row_count), (1, 2))

    def _zip(self, members):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        return buffer.getvalue()

    def test_zip_bombs_are_refused_before_the_child_starts(self):
        bomb = self._zip({"xl/workbook.xml": "<x/>", "xl/worksheets/sheet1.xml": b"0" * 2_000_000})
        with mock.patch.object(importer.subprocess, "run") as run:
            with self.assertRaisesMessage(importer.ImportRefused, importer.UNREADABLE_TEXT):
                self.upload(bomb, name="bomb.xlsx")
            run.assert_not_called()
        total = self._zip({"xl/workbook.xml": "<x/>", "xl/a.xml": os.urandom(5000)})
        with mock.patch.object(importer, "XLSX_TOTAL_MAX", 1000):
            with self.assertRaisesMessage(importer.ImportRefused, importer.UNREADABLE_TEXT):
                self.upload(total, name="total.xlsx")
        strings = self._zip({"xl/workbook.xml": "<x/>", "xl/sharedStrings.xml": os.urandom(5000)})
        with mock.patch.object(importer, "XLSX_SHARED_STRINGS_MAX", 1000):
            with self.assertRaisesMessage(importer.ImportRefused, importer.UNREADABLE_TEXT):
                self.upload(strings, name="strings.xlsx")
        with self.assertRaisesMessage(importer.ImportRefused, importer.UNREADABLE_TEXT):
            self.upload(b"inte en zip", name="trasig.xlsx")
        self.assertEqual(self.files(), [])

    def test_child_failure_and_timeout_are_refused(self):
        content = xlsx_bytes(["Mobil"], [[phone(1)]])
        failed = subprocess.CompletedProcess([], xlsx2csv.EXIT_UNREADABLE, b"", b"")
        with mock.patch.object(importer.subprocess, "run", return_value=failed):
            with self.assertRaisesMessage(importer.ImportRefused, importer.UNREADABLE_TEXT):
                self.upload(content, name="a.xlsx")
        many = subprocess.CompletedProcess([], xlsx2csv.EXIT_TOO_MANY_ROWS, b"", b"")
        with mock.patch.object(importer.subprocess, "run", return_value=many):
            with self.assertRaisesMessage(importer.ImportRefused, importer.TOO_MANY_ROWS_TEXT):
                self.upload(content, name="b.xlsx")
        timeout = subprocess.TimeoutExpired("x", importer.CHILD_TIMEOUT)
        with mock.patch.object(importer.subprocess, "run", side_effect=timeout):
            with self.assertRaisesMessage(importer.ImportRefused, importer.UNREADABLE_TEXT):
                self.upload(content, name="c.xlsx")
        self.assertEqual(self.files(), [])

    def test_example_file(self):
        """Exempelfilen i static/utskick: fiktiva rader som går rakt igenom."""
        job = self.upload(MALL.read_bytes(), name="kontakter-mall.xlsx")
        self.assertEqual(
            job.mapping,
            {
                "0": "first_name",
                "1": "last_name",
                "2": "phone",
                "3": "email",
                "4": "company_name",
                "5": "org_number",
                "6": "new:number:kundnummer",
                "7": "new:date:senaste-besok",
                "8": "unsubscribed",
            },
        )
        job.status = S.CONSENT
        job.save()
        self.assertEqual(importer.save_consent(job, {"choice": "unknown"}), {})
        importer.analyse(job)
        self.assertEqual(importer.preview(job)["new"], 4)
        self.assertEqual(importer.preview(job)["suppressed"], 1)
        self.assertTrue(importer.begin_import(job, self.actor))
        importer.run_import(job)
        self.assertEqual(job.status, S.DONE)
        company = Contact.objects.get(account=self.account, company_name="Exempelbolaget AB")
        self.assertEqual(company.kind, Contact.Kind.COMPANY)
        self.assertEqual(company.consents.get(channel=CHANNEL_EMAIL).status, consent.COMPANY)
        anna = Contact.objects.get(account=self.account, first_name="Anna")
        self.assertEqual(anna.fields, {"kundnummer": "1001", "senaste-besok": "2025-11-04"})
        self.assertEqual(
            FieldDef.objects.get(account=self.account, key="senaste-besok").kind, "date"
        )
        # Johan är avregistrerad i filen: på spärrlistan, ingen kontakt.
        self.assertFalse(Contact.objects.filter(account=self.account, first_name="Johan").exists())
        self.assertTrue(suppression.is_suppressed(self.account, CHANNEL_SMS, "+46701740615"))


# ---------------------------------------------------------------------------
# Steg 2: kolumnerna
# ---------------------------------------------------------------------------


class MappingTests(ImportFiles, TestCase):
    def test_header_and_value_guesses(self):
        header = ["Kund", "Mail", "Telefon", "Orgnr", "Företag", "Senaste besök", "Kommentar", "X"]
        rows = [
            [
                "Anna",
                "anna@exempel.example",
                phone(1),
                "5560000003",
                "Ab",
                "2025-01-02",
                "Hej",
                "a@b.example",
            ],
            ["Bo", "bo@exempel.example", phone(2), "", "", "2025-03-04", "Då", "c@d.example"],
        ]
        job = self.upload(csv_text(header, rows).encode())
        self.assertEqual(
            job.mapping,
            {
                "0": "full_name",
                "1": "email",
                "2": "phone",
                "3": "org_number",
                "4": "company_name",
                "5": "new:date:senaste-besok",
                "6": "new:text:kommentar",
                # Okänd rubrik med bara e-post, men e-post är redan vald: extrafält.
                "7": "new:text:x",
            },
        )

    def test_value_guesses_when_header_is_unknown(self):
        job = self.upload(
            csv_text(
                ["A", "B", "Kund sedan"], [["anna@exempel.example", phone(1), "2024-05-01"]]
            ).encode()
        )
        self.assertEqual(job.mapping["0"], "email")
        self.assertEqual(job.mapping["1"], "phone")
        # "Kund sedan" är ett datum, inget namn.
        self.assertEqual(job.mapping["2"], "new:date:kund-sedan")

    def test_existing_field_is_matched_by_label(self):
        FieldDef.objects.create(account=self.account, key="regnr", label="Regnummer")
        job = self.upload(csv_text(["Mobil", "Regnummer"], [[phone(1), "ABC 123"]]).encode())
        self.assertEqual(job.mapping["1"], "field:regnr")

    def test_personnummer_column_is_forced_to_skip(self):
        header = ["Namn", "Personnummer", "Id", "Mobil"]
        rows = [
            ["Anna", pnr("900101"), pnr("850505", "234"), phone(1)],
            ["Bo", pnr("800202"), pnr("750303", "345"), phone(2)],
        ]
        job = self.upload(csv_text(header, rows).encode())
        self.assertEqual((job.mapping["1"], job.mapping["2"]), ("skip", "skip"))
        cards = importer.column_cards(job, {})
        self.assertTrue(cards[1]["pnr"] and cards[2]["pnr"])
        self.assertEqual((cards[1]["example"], cards[2]["example"]), ("", ""))
        # Varken sample eller CSV:n bär personnumren.
        self.assertEqual([row[1:3] for row in job.sample], [["", ""], ["", ""]])
        raw = (Path(self.tmp) / job.csv_path).read_text()
        self.assertNotIn(pnr("900101"), raw)
        self.assertNotIn(pnr("850505", "234"), raw)
        # Ett postat val ändrar inget: kolumnen hoppas över.
        posted = {"col-0": "full_name", "col-1": "phone", "col-2": "new:text:id", "col-3": "email"}
        self.assertEqual(importer.save_mapping(job, posted, {}), {})
        self.assertEqual(job.mapping["1"], "skip")
        self.assertEqual(job.mapping["2"], "skip")
        self.assertEqual(job.status, S.CONSENT)
        job.status = S.MAPPING
        job.save()
        html = self.client_for(self.anna).get(self.job_url(job)).content.decode()
        self.assertIn("Personnummer sparas inte i Kontakter.", html)
        self.assertNotIn('name="col-1"', html)

    def test_mapping_validation(self):
        job = self.upload(
            csv_text(["Namn", "Mobil", "Mobil 2"], [["Anna", phone(1), phone(2)]]).encode()
        )
        errors = importer.save_mapping(
            job, {"col-0": "full_name", "col-1": "phone", "col-2": "phone"}, {}
        )
        self.assertIn("är vald för flera kolumner", errors[2])
        errors = importer.save_mapping(
            job, {"col-0": "full_name", "col-1": "skip", "col-2": "skip"}, {}
        )
        self.assertEqual(errors["__all__"], importer.NEED_ADDRESS_TEXT)
        errors = importer.save_mapping(job, {"col-0": "field:finns-inte", "col-1": "phone"}, {})
        self.assertEqual(errors[0], importer.PICK_TEXT)
        job.refresh_from_db()
        self.assertEqual(job.status, S.MAPPING)

    def test_thirty_field_limit(self):
        for i in range(FieldDef.MAX_PER_ACCOUNT):
            FieldDef.objects.create(account=self.account, key=f"f{i}", label=f"Fält {i}")
        job = self.upload(csv_text(["Mobil", "Okänd"], [[phone(1), "värde"]]).encode())
        self.assertEqual(job.mapping["1"], "skip")
        defs = importer.contacts.field_defs(self.account)
        cards = importer.column_cards(job, defs)
        self.assertTrue(cards[1]["limit"])
        self.assertNotIn("new:", " ".join(v for v, _ in cards[1]["options"]))
        html = self.client_for(self.anna).get(self.job_url(job)).content.decode()
        self.assertIn(importer.FIELD_LIMIT_TEXT, html)

    def test_map_page_renders_one_card_per_column(self):
        job = self.upload(
            csv_text(
                ["Namn", "Mobil", "Orgnr"], [["Anna", phone(1), ""], ["Bo", phone(2), ""]]
            ).encode()
        )
        html = self.client_for(self.anna).get(self.job_url(job)).content.decode()
        self.assertEqual(html.count('class="fl-im-col"'), 3)
        self.assertIn("(tom på 2 rader)", html)
        self.assertIn("kunder.csv", html)


# ---------------------------------------------------------------------------
# Steg 3 och 4, och importen
# ---------------------------------------------------------------------------


class FlowTests(ImportFiles, TestCase):
    HEADER = ["Namn", "Mobil", "E-post"]

    def test_consent_step_cannot_be_skipped(self):
        client = self.client_for(self.anna)
        job = self.upload(csv_text(self.HEADER, [["Anna", phone(1), ""]]).encode())
        url = self.job_url(job)
        client.post(
            url, {"action": "map", "col-0": "full_name", "col-1": "phone", "col-2": "email"}
        )
        job.refresh_from_db()
        self.assertEqual(job.status, S.CONSENT)
        # Importera direkt från samtyckessteget går inte.
        client.post(url, {"action": "import"})
        job.refresh_from_db()
        self.assertEqual(job.status, S.CONSENT)
        self.assertFalse(Contact.objects.filter(account=self.account).exists())
        response = client.post(url, {"action": "consent"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Välj hur kontakterna har sagt ja.", status_code=400)
        response = client.post(url, {"action": "consent", "choice": "consent"})
        self.assertContains(response, "Kryssa i vilka kanaler", status_code=400)
        self.assertContains(response, "Skriv var och när", status_code=400)
        # Inget är förvalt eller förkryssat (H.5).
        html = client.get(url).content.decode()
        self.assertIn('name="sms"', html)
        self.assertIn('name="email"', html)
        self.assertNotIn(" checked", html)

    def test_unknown_gives_no_reklam(self):
        rows = [["Anna Lindqvist", phone(1), "anna@exempel.example"], ["Bo Ek", phone(2), ""]]
        job = self.flow(csv_text(self.HEADER, rows))
        self.assertEqual(job.status, S.DONE)
        self.assertEqual((job.counts["new"], job.counts["updated"]), (2, 0))
        contacts_qs = Contact.objects.filter(account=self.account)
        self.assertEqual(contacts_qs.count(), 2)
        for channel in (CHANNEL_SMS, CHANNEL_EMAIL):
            self.assertEqual(
                consent.eligible_contacts(contacts_qs, channel, consent.REKLAM).count(), 0
            )
        anna = contacts_qs.get(first_name="Anna")
        # Filens namn, inte importens nummer (tidslinjen visar det).
        self.assertEqual(
            (anna.last_name, anna.source, anna.source_detail),
            ("Lindqvist", "import", job.original_name),
        )
        self.assertTrue(
            Event.objects.filter(
                contact=anna, kind=Event.IMPORTED, data={"import": job.pk, "fil": job.original_name}
            ).exists()
        )
        # Klar: sample töms, filen finns kvar ett dygn för felfilen.
        self.assertEqual(job.sample, [])
        self.assertTrue(job.csv_path)
        self.assertIsNotNone(job.finished_at)

    def test_consent_per_channel_with_evidence(self):
        rows = [["Anna", phone(1), "anna@exempel.example"]]
        job = self.flow(
            csv_text(self.HEADER, rows), choice="consent", sms=True, where="kassan, från 2024"
        )
        anna = Contact.objects.get(account=self.account)
        sms = anna.consents.get(channel=CHANNEL_SMS)
        self.assertEqual(
            (sms.status, sms.evidence, sms.source), ("yes", "kassan, från 2024", "import")
        )
        self.assertEqual(anna.consents.get(channel=CHANNEL_EMAIL).status, consent.MISSING)
        self.assertEqual(
            job.consent,
            {"choice": "consent", "sms": True, "email": False, "where": "kassan, från 2024"},
        )
        log = ConsentLog.objects.get(contact=anna, new_status="yes")
        self.assertEqual(
            (log.by_label, log.by_staff, log.source_detail),
            ("Anna Lindqvist", False, job.original_name),
        )

    def test_existing_customers_on_both_channels(self):
        rows = [["Anna", phone(1), "anna@exempel.example"]]
        self.flow(
            csv_text(self.HEADER, rows),
            choice="existing",
            sms=True,
            email=True,
            where="kunder sedan 2023",
        )
        anna = Contact.objects.get(account=self.account)
        self.assertEqual(
            sorted(anna.consents.values_list("channel", "status")),
            [("email", "existing"), ("sms", "existing")],
        )

    def test_staff_in_view_as_imports_for_real_and_is_logged(self):
        rows = [["Anna", phone(1), ""]]
        job = self.flow(
            csv_text(self.HEADER, rows),
            choice="consent",
            sms=True,
            where="kassan",
            client=self.client_for(self.staff),
        )
        self.assertTrue(job.created_as_staff)
        log = ConsentLog.objects.get(new_status="yes")
        self.assertTrue(log.by_staff)
        self.assertTrue(log.by_label.startswith("ADX ("))

    def test_updates_never_overwrite_an_address_and_conflicts_are_reported(self):
        anna = make_contact(
            self.account, first_name="Anna", phone=PHONE_ANNA, email="anna@exempel.example"
        )
        make_contact(self.account, first_name="Bo", phone=PHONE_BO)
        cilla = make_contact(self.account, first_name="", phone=PHONE_CILLA)
        rows = [
            # Samma nummer, annan e-post: krock, inget skrivs över.
            ["Anna Ny", "0701740601", "annan@exempel.example"],
            # Numret är Bos, e-posten Annas: två kontakter.
            ["X", "0701740602", "anna@exempel.example"],
            # Samma nummer, tom e-post på kontakten: fylls i.
            ["Cilla Berg", "0701740603", "cilla@exempel.example"],
        ]
        job = self.flow(csv_text(self.HEADER, rows))
        self.assertEqual(
            (job.counts["updated"], job.counts["conflicts"], job.counts["new"]), (1, 2, 0)
        )
        anna.refresh_from_db()
        self.assertEqual((anna.email, anna.first_name), ("anna@exempel.example", "Anna"))
        cilla.refresh_from_db()
        self.assertEqual(
            (cilla.email, cilla.first_name, cilla.last_name),
            ("cilla@exempel.example", "Cilla", "Berg"),
        )
        reasons = {e["row"]: (e["column"], e["reason"]) for e in job.errors}
        self.assertEqual(reasons[2], ("E-post", importer.CONFLICT_TEXTS["email_differs"]))
        self.assertEqual(reasons[3][1], importer.CONFLICT_TEXTS["two_contacts"])
        # Granska räknade likadant som importen.
        self.assertIn("Varav 2 krockar", self.review)

    def test_duplicates_inside_the_file_are_counted_like_the_import(self):
        rows = [
            ["Anna", "0701740601", ""],
            ["Anna L", "0701740601", "anna@exempel.example"],
            ["Anna", "0701740601", "annan@exempel.example"],
        ]
        job = self.flow(csv_text(self.HEADER, rows))
        preview = job.counts["preview"]
        self.assertEqual((preview["new"], preview["updated"], preview["conflicts"]), (1, 1, 1))
        self.assertEqual(
            (job.counts["new"], job.counts["updated"], job.counts["conflicts"]), (1, 1, 1)
        )

    def test_suppressed_stays_unsubscribed_and_marked_rows_are_suppressed(self):
        suppression.add(
            self.account,
            CHANNEL_SMS,
            keys.value_hash(CHANNEL_SMS, PHONE_ANNA),
            Suppression.Reason.STOP,
        )
        bo = make_contact(self.account, first_name="Bo", phone=PHONE_BO)
        header = ["Namn", "Mobil", "Avregistrerad"]
        rows = [
            ["Anna", "0701740601", ""],
            ["Bo", "0701740602", "ja"],
            ["Cilla", "0701740603", "nej"],
        ]
        job = self.flow(csv_text(header, rows), choice="consent", sms=True, where="kassan")
        self.assertEqual(
            (job.counts["suppressed"], job.counts["marked"], job.counts["new"]), (2, 1, 1)
        )
        self.assertFalse(Contact.objects.filter(account=self.account, phone=PHONE_ANNA).exists())
        bo.refresh_from_db()
        self.assertEqual(bo.consents.get(channel=CHANNEL_SMS).status, consent.UNSUBSCRIBED)
        self.assertEqual(
            Suppression.objects.get(
                account=self.account, value_hash=keys.value_hash(CHANNEL_SMS, PHONE_BO)
            ).reason,
            Suppression.Reason.IMPORT,
        )
        cilla = Contact.objects.get(account=self.account, phone=PHONE_CILLA)
        self.assertEqual(cilla.consents.get(channel=CHANNEL_SMS).status, consent.YES)
        # Spärren för Anna står kvar: importen lyfter aldrig en spärr.
        self.assertTrue(suppression.is_suppressed(self.account, CHANNEL_SMS, PHONE_ANNA))

    def test_import_cannot_flip_a_persons_own_choice(self):
        anna = make_contact(self.account, first_name="Anna", phone=PHONE_ANNA)
        consent.set_status(anna, CHANNEL_SMS, consent.DECLINED, source=Consent.Source.PREFERENCE)
        self.flow(
            csv_text(self.HEADER, [["Anna", "0701740601", ""]]),
            choice="consent",
            sms=True,
            where="kassan",
        )
        self.assertEqual(anna.consents.get(channel=CHANNEL_SMS).status, consent.DECLINED)

    def test_errors_and_errors_csv(self):
        rows = [
            ["Anna", "0701740601", "anna@exempel.example"],
            ["Bo", "123", ""],
            ["Cilla", "", "inte-en-adress"],
            ["Bara namn", "", ""],
        ]
        job = self.flow(csv_text(self.HEADER, rows))
        self.assertEqual((job.counts["new"], job.counts["errors"]), (1, 3))
        self.assertEqual([e["row"] for e in job.errors], [3, 4, 5])
        self.assertEqual(job.errors[0]["column"], "Mobil")
        self.assertEqual(job.errors[2]["reason"], importer.NO_ADDRESS_TEXT)
        # Felen bär inga värden.
        self.assertNotIn("inte-en-adress", str(job.errors))
        client = self.client_for(self.anna)
        response = client.get(reverse("flamingo:app_import_errors", args=[job.pk]))
        self.assertIn("no-store", response["Cache-Control"])
        body = response.content.decode("utf-8-sig")
        lines = body.strip().split("\r\n")
        self.assertEqual(lines[0], "Rad;Kolumn;Orsak;Namn;Mobil;E-post")
        self.assertIn("inte-en-adress", lines[2])
        # Efter städningen finns bara rad, kolumn och orsak.
        ImportJob.objects.filter(pk=job.pk).update(finished_at=timezone.now() - timedelta(hours=25))
        importer.cleanup()
        body = client.get(reverse("flamingo:app_import_errors", args=[job.pk])).content.decode(
            "utf-8-sig"
        )
        self.assertEqual(body.split("\r\n")[0], "Rad;Kolumn;Orsak")
        self.assertNotIn("inte-en-adress", body)

    def test_error_csv_cells_cannot_become_formulas(self):
        job = self.flow(csv_text(self.HEADER, [["=HYPERLINK(1)", "123", ""]]))
        body = (
            self.client_for(self.anna)
            .get(reverse("flamingo:app_import_errors", args=[job.pk]))
            .content.decode("utf-8-sig")
        )
        self.assertIn("'=HYPERLINK(1)", body)

    def test_contact_limit(self):
        make_contact(self.account, first_name="Finns", phone=PHONE_ANNA)
        self.settings.contact_limit = 2
        self.settings.save()
        rows = [
            ["Bo", "0701740602", ""],
            ["Cilla", "0701740603", ""],
            ["Anna", "0701740601", "anna@exempel.example"],
        ]
        job = self.flow(csv_text(self.HEADER, rows))
        self.assertIn("1 ny kontakt får inte plats under din gräns.", self.review)
        self.assertEqual((job.counts["new"], job.counts["updated"], job.counts["limit"]), (1, 1, 1))
        self.assertEqual(Contact.objects.filter(account=self.account).count(), 2)

    def test_company_with_private_email_is_counted(self):
        header = ["Företag", "Orgnr", "E-post"]
        rows = [
            ["Exempelbolaget AB", "5560000003", "bolaget@gmail.com"],
            ["Exempel Två AB", "5560000011", "info@exempeltva.example"],
        ]
        job = self.flow(csv_text(header, rows))
        self.assertIn(
            "1 företag med privat e-postadress får inte e-post utan samtycke.", self.review
        )
        self.assertEqual(job.counts["company_freemail"], 1)
        two = Contact.objects.get(account=self.account, company_name="Exempel Två AB")
        self.assertEqual(two.consents.get(channel=CHANNEL_EMAIL).status, consent.COMPANY)

    def test_target_list_and_tag(self):
        old = make_contact(self.account, first_name="Bo", phone=PHONE_BO)
        rows = [["Anna", "0701740601", ""], ["Bo", "0701740602", ""], ["Fel", "1", ""]]
        job = self.flow(
            csv_text(self.HEADER, rows),
            list="new",
            new_list="Däckhotell 2026",
            tag="Import okt 2026",
        )
        lista = ContactList.objects.get(account=self.account, name="Däckhotell 2026")
        self.assertEqual(job.target_list, lista)
        self.assertEqual(
            set(lista.memberships.values_list("contact__first_name", flat=True)), {"Anna", "Bo"}
        )
        self.assertEqual(lista.memberships.first().source, "import")
        tag = Tag.objects.get(account=self.account, name="Import okt 2026")
        self.assertIn(old, tag.contacts.all())
        self.assertEqual(tag.contacts.count(), 2)

    def test_existing_list_and_foreign_list_id(self):
        lista = ContactList.objects.create(account=self.account, name="Kunder")
        other = ContactList.objects.create(account=self.other_account, name="Andras")
        client = self.client_for(self.anna)
        job = self.upload(csv_text(self.HEADER, [["Anna", "0701740601", ""]]).encode())
        job.status = S.CONSENT
        job.save()
        importer.save_consent(job, {"choice": "unknown"})
        importer.analyse(job)
        response = client.post(self.job_url(job), {"action": "import", "list": str(other.pk)})
        self.assertEqual(response.status_code, 400)
        job.refresh_from_db()
        self.assertEqual(job.status, S.REVIEW)
        response = client.post(
            self.job_url(job), {"action": "import", "list": "new", "new_list": ""}
        )
        self.assertContains(response, "Skriv ett namn på den nya listan.", status_code=400)
        client.post(self.job_url(job), {"action": "import", "list": str(lista.pk)})
        job.refresh_from_db()
        self.assertEqual((job.status, job.target_list), (S.DONE, lista))
        self.assertEqual(lista.memberships.count(), 1)

    def test_double_post_imports_once(self):
        client = self.client_for(self.anna)
        job = self.upload(csv_text(self.HEADER, [["Anna", "0701740601", ""]]).encode())
        job.status = S.CONSENT
        job.save()
        importer.save_consent(job, {"choice": "unknown"})
        importer.analyse(job)
        client.post(self.job_url(job), {"action": "import"})
        client.post(self.job_url(job), {"action": "import"})
        self.assertEqual(Contact.objects.filter(account=self.account).count(), 1)
        self.assertEqual(Event.objects.filter(kind=Event.IMPORTED).count(), 1)

    def test_back_and_cancel(self):
        client = self.client_for(self.anna)
        job = self.upload(csv_text(self.HEADER, [["Anna", "0701740601", ""]]).encode())
        url = self.job_url(job)
        client.post(url, {"action": "map", "col-0": "full_name", "col-1": "phone", "col-2": "skip"})
        client.post(url, {"action": "back"})
        job.refresh_from_db()
        self.assertEqual(job.status, S.MAPPING)
        response = client.post(url, {"action": "cancel"})
        self.assertRedirects(
            response, reverse("flamingo:app_import"), fetch_redirect_response=False
        )
        job.refresh_from_db()
        self.assertEqual(job.status, S.CANCELLED)
        self.assertEqual((job.sample, job.csv_path), ([], ""))
        self.assertIsNotNone(job.file_deleted_at)
        self.assertEqual(self.files(), [])

    def test_no_mail_to_the_customer_and_agency_alert_for_large_consent_imports(self):
        rows = [[f"Person {i}", phone(i), ""] for i in range(3)]
        with (
            override_settings(INQUIRY_NOTIFICATION_EMAIL="byran@adx.example"),
            mock.patch.object(importer, "ALERT_ROWS", 2),
        ):
            self.flow(csv_text(self.HEADER, rows), choice="consent", sms=True, where="kassan")
            self.flow(csv_text(self.HEADER, rows))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["byran@adx.example"])
        self.assertIn("kassan", mail.outbox[0].body)
        self.assertNotIn("0701740", mail.outbox[0].body)


class GateTests(ImportFiles, TestCase):
    def test_dpa_gate_before_step_one(self):
        DpaAcceptance.objects.filter(account=self.account).delete()
        client = self.client_for(self.anna)
        response = client.get(reverse("flamingo:app_import"))
        self.assertContains(response, "godkänn biträdesavtalet först")
        self.assertNotContains(response, 'name="file"')
        upload = SimpleUploadedFile("k.csv", b"Mobil\n0701740601\n")
        client.post(reverse("flamingo:app_import"), {"action": "upload", "file": upload})
        self.assertFalse(ImportJob.objects.exists())
        with self.assertRaises(importer.ImportRefused):
            importer.start_paste(self.account, "Mobil\n0701740601", self.actor)

    def test_new_dpa_version_stops_the_import_midway(self):
        job = self.upload(csv_text(["Mobil"], [["0701740601"]]).encode())
        job.status = S.CONSENT
        job.save()
        importer.save_consent(job, {"choice": "unknown"})
        importer.analyse(job)
        DpaVersion.objects.update(is_current=False)
        DpaVersion.objects.create(version="2026-11", text="Nytt", sha256="1" * 64, is_current=True)
        client = self.client_for(self.anna)
        client.post(self.job_url(job), {"action": "import"})
        job.refresh_from_db()
        self.assertEqual(job.status, S.REVIEW)
        # Och i en import som redan startat (ticken): den stoppas med förklaringen.
        ImportJob.objects.filter(pk=job.pk).update(status=S.IMPORTING, in_request=False)
        job.refresh_from_db()
        importer.run_import(job)
        self.assertEqual(job.status, S.FAILED)
        self.assertIn("biträdesavtalet", importer.failure_text(job))
        self.assertFalse(Contact.objects.filter(account=self.account).exists())

    def test_other_accounts_job_is_404(self):
        job = self.upload(
            csv_text(["Mobil"], [["0701740601"]]).encode(), account=self.other_account
        )
        client = self.client_for(self.anna)
        self.assertEqual(client.get(self.job_url(job)).status_code, 404)
        self.assertEqual(client.post(self.job_url(job), {"action": "cancel"}).status_code, 404)
        self.assertEqual(
            client.get(reverse("flamingo:app_import_errors", args=[job.pk])).status_code, 404
        )
        job.refresh_from_db()
        self.assertEqual(job.status, S.MAPPING)

    def test_disabled_account_is_404(self):
        self.settings.is_enabled = False
        self.settings.save()
        client = self.client_for(self.anna)
        self.assertEqual(client.get(reverse("flamingo:app_import")).status_code, 404)

    def test_upload_page_lists_earlier_imports(self):
        job = self.upload(csv_text(["Mobil"], [["0701740601"]]).encode(), name="vår-fil.csv")
        html = self.client_for(self.anna).get(reverse("flamingo:app_import")).content.decode()
        self.assertIn("vår-fil.csv", html)
        self.assertIn(self.job_url(job), html)
        self.assertIn("kontakter-mall.xlsx", html)
        self.assertIn('data-label="Fil"', html)

    def test_upload_error_is_shown(self):
        client = self.client_for(self.anna)
        upload = SimpleUploadedFile("k.pdf", b"x")
        response = client.post(reverse("flamingo:app_import"), {"action": "upload", "file": upload})
        self.assertContains(response, importer.KIND_TEXT, status_code=400)
        response = client.post(reverse("flamingo:app_import"), {"action": "upload"})
        self.assertContains(response, importer.NO_FILE_TEXT, status_code=400)


# ---------------------------------------------------------------------------
# Stora filer i ticken, i omgångar
# ---------------------------------------------------------------------------


@mock.patch.object(importer, "IN_REQUEST_ROWS", 10)
@mock.patch.object(importer, "CHUNK_ROWS", 20)
@mock.patch.object(importer, "BATCH_ROWS", 7)
class ChunkTests(OnTickClock, ImportFiles, TestCase):
    def _job(self, n=45):
        rows = [[f"Person {i}", phone(i)] for i in range(n)]
        rows[5][1] = "123"
        rows[30][1] = phone(0)  # samma nummer som rad 0 i filen: uppdateras
        job = self.upload(csv_text(["Namn", "Mobil"], rows).encode())
        self.assertFalse(job.in_request)
        job.status = S.CONSENT
        job.save()
        self.assertEqual(importer.save_consent(job, {"choice": "unknown"}), {})
        return job

    def test_analysis_resumes_from_byte_offset(self):
        job = self._job()
        past = time.monotonic() - 1
        self.assertFalse(importer.analyse(job, deadline=past))
        self.assertEqual((job.status, job.progress), (S.ANALYSING, 20))
        first_offset = job.byte_offset
        self.assertGreater(first_offset, 0)
        self.assertTrue(Path(self.tmp, job.csv_path + ".state.json").is_file())
        self.assertFalse(importer.analyse(job, deadline=past))
        self.assertGreater(job.byte_offset, first_offset)
        self.assertTrue(importer.analyse(job))
        self.assertEqual(job.status, S.REVIEW)
        preview = importer.preview(job)
        self.assertEqual((preview["new"], preview["updated"], preview["errors"]), (43, 1, 1))
        self.assertFalse(Path(self.tmp, job.csv_path + ".state.json").exists())

    def test_lost_state_file_restarts_the_analysis(self):
        job = self._job()
        importer.analyse(job, deadline=time.monotonic() - 1)
        Path(self.tmp, job.csv_path + ".state.json").unlink()
        importer.analyse(job)
        preview = importer.preview(job)
        self.assertEqual((preview["new"], preview["updated"], preview["errors"]), (43, 1, 1))

    def test_the_tick_converts_analyses_and_imports_in_batches(self):
        job = self._job()
        self.assertTrue(importer.work_exists())
        importer.import_chunk()
        job.refresh_from_db()
        self.assertEqual(job.status, S.REVIEW)
        self.assertFalse(importer.work_exists())
        importer.begin_import(job, self.actor)
        job.refresh_from_db()
        self.assertTrue(importer.work_exists())
        # En omgång i taget: avbrutet efter första omgången, sedan vidare.
        self.assertFalse(importer.run_import(job, deadline=time.monotonic() - 1))
        self.assertEqual((job.status, job.progress), (S.IMPORTING, 7))
        # Rad 5 i filen har ett fel: sex kontakter av sju rader.
        self.assertEqual(Contact.objects.filter(account=self.account).count(), 6)
        summary = importer.import_chunk()
        job.refresh_from_db()
        self.assertEqual(job.status, S.DONE)
        self.assertEqual(summary["rows"], 38)
        self.assertEqual(
            (job.counts["new"], job.counts["updated"], job.counts["errors"]), (43, 1, 1)
        )
        self.assertEqual(Contact.objects.filter(account=self.account).count(), 43)
        self.assertEqual(importer.job_actor(job).label, "Anna Lindqvist")

    def test_cancel_stops_at_the_next_batch(self):
        job = self._job()
        importer.analyse(job)
        importer.begin_import(job, self.actor)
        job.refresh_from_db()
        importer.run_import(job, deadline=time.monotonic() - 1)
        importer.cancel(job)
        importer.import_chunk()
        job.refresh_from_db()
        self.assertEqual(job.status, S.CANCELLED)
        self.assertEqual(Contact.objects.filter(account=self.account).count(), 6)

    def test_orphaned_request_job_is_taken_over_by_the_tick(self):
        job = self._job()
        ImportJob.objects.filter(pk=job.pk).update(
            in_request=True, started_at=timezone.now() - timedelta(minutes=11)
        )
        self.assertTrue(importer.work_exists())
        importer.import_chunk()
        job.refresh_from_db()
        self.assertEqual((job.status, job.in_request), (S.REVIEW, False))

    def test_wait_page_and_status_json(self):
        job = self._job()
        client = self.client_for(self.anna)
        html = client.get(self.job_url(job)).content.decode()
        self.assertIn("data-im-poll", html)
        self.assertIn("Raderna gås igenom.", html)
        data = client.get(self.job_url(job) + "?status=json").json()
        self.assertEqual((data["status"], data["waiting"], data["total"]), ("analysing", True, 45))
        importer.import_chunk()
        data = client.get(self.job_url(job) + "?status=json").json()
        self.assertEqual((data["status"], data["waiting"]), ("review", False))


class InRequestTests(OnTickClock, ImportFiles, TestCase):
    def _review(self, n):
        rows = [[f"Person {i}", phone(i)] for i in range(n)]
        client = self.client_for(self.anna)
        job = self.upload(csv_text(["Namn", "Mobil"], rows).encode())
        url = self.job_url(job)
        client.post(url, {"action": "map", "col-0": "full_name", "col-1": "phone"})
        client.post(url, {"action": "consent", "choice": "unknown"})
        job.refresh_from_db()
        return client, job

    def test_2000_rows_are_analysed_in_the_request(self):
        client, job = self._review(2000)
        self.assertTrue(job.in_request)
        self.assertEqual(job.status, S.REVIEW)
        self.assertEqual(importer.preview(job)["new"], 2000)
        html = client.get(self.job_url(job)).content.decode()
        self.assertIn("Importera 2\xa0000 kontakter", html)

    @mock.patch.object(importer, "IN_REQUEST_SECONDS", 0)
    @mock.patch.object(importer, "BATCH_ROWS", 2)
    def test_what_the_request_does_not_finish_goes_to_the_tick(self):
        client, job = self._review(5)
        client.post(self.job_url(job), {"action": "import"})
        job.refresh_from_db()
        self.assertEqual((job.status, job.in_request, job.progress), (S.IMPORTING, False, 2))
        self.assertIn("Kontakterna importeras.", client.get(self.job_url(job)).content.decode())
        self.assertTrue(importer.work_exists())
        importer.import_chunk()
        job.refresh_from_db()
        self.assertEqual((job.status, job.counts["new"]), (S.DONE, 5))

    def test_the_tick_leaves_a_running_request_alone(self):
        client, job = self._review(3)
        ImportJob.objects.filter(pk=job.pk).update(status=S.IMPORTING, started_at=timezone.now())
        self.assertFalse(importer.work_exists())
        self.assertEqual(importer.import_chunk(), {"jobs": 0, "rows": 0})

    def test_key_mismatch_stops_the_import(self):
        client, job = self._review(3)
        importer.begin_import(job, self.actor)
        job.refresh_from_db()
        with mock.patch.object(
            importer.keys, "require_fingerprints", side_effect=keys.KeyMismatch("x")
        ):
            importer.run_import(job)
        self.assertEqual(job.status, S.FAILED)
        self.assertEqual(importer.failure_text(job), importer.KEYS_TEXT)
        self.assertFalse(Contact.objects.filter(account=self.account).exists())


# ---------------------------------------------------------------------------
# Städningen (E.7)
# ---------------------------------------------------------------------------


class CleanupTests(ImportFiles, TestCase):
    def test_cleanup(self):
        now = timezone.now()
        done = self.upload(csv_text(["Mobil"], [["0701740601"]]).encode())
        ImportJob.objects.filter(pk=done.pk).update(
            status=S.DONE, finished_at=now - timedelta(hours=25), sample=[["x"]]
        )
        fresh = self.upload(csv_text(["Mobil"], [["0701740602"]]).encode())
        ImportJob.objects.filter(pk=fresh.pk).update(
            status=S.DONE, finished_at=now - timedelta(hours=2)
        )
        abandoned = self.upload(csv_text(["Mobil"], [["0701740603"]]).encode())
        ImportJob.objects.filter(pk=abandoned.pk).update(created_at=now - timedelta(days=8))
        old = ImportJob.objects.create(
            account=self.account,
            original_name="gammal",
            kind="csv",
            status=S.DONE,
            file_deleted_at=now - timedelta(days=89),
        )
        ImportJob.objects.filter(pk=old.pk).update(created_at=now - timedelta(days=91))
        stray = Path(self.tmp, importer.FOLDER, "2026", "01", "lost.csv")
        stray.parent.mkdir(parents=True)
        stray.write_text("x")
        long_ago = (now - timedelta(days=9)).timestamp()
        os.utime(stray, (long_ago, long_ago))

        summary = importer.cleanup(now)

        self.assertEqual(summary, {"abandoned": 1, "files": 1, "jobs": 1, "orphans": 1})
        done.refresh_from_db()
        self.assertEqual((done.csv_path, done.sample), ("", []))
        self.assertIsNotNone(done.file_deleted_at)
        fresh.refresh_from_db()
        self.assertTrue(fresh.csv_path)
        abandoned.refresh_from_db()
        self.assertEqual(abandoned.status, S.CANCELLED)
        self.assertFalse(ImportJob.objects.filter(pk=old.pk).exists())
        self.assertFalse(stray.exists())
        self.assertEqual(self.files(), [Path(fresh.csv_path).name])

    def test_failed_conversion_leaves_no_files(self):
        with self.assertRaises(importer.ImportRefused):
            self.upload(b"\n\n", name="tom.csv")
        self.assertEqual(self.files(), [])
        self.assertEqual(ImportJob.objects.get().status, S.FAILED)


class ReviewFixTests(ImportFiles, TestCase):
    """Rättningarna efter granskningen av S1 (korrekthet 2 till 7,
    säkerhet 4)."""

    def test_a_bad_extra_field_value_costs_the_value_not_the_contact(self):
        header = ["Namn", "Mobil", "Besök", "Anteckning"]
        rows = [[f"Person {n}", phone(n), "2025-01-02", "kort"] for n in range(1, 11)]
        rows.append(["Okänd Datum", phone(11), "okänt", "x" * 600])
        text = csv_text(header, rows)
        # Ett enda värde som inte är ett datum: en ny kolumn gissas som text.
        self.assertEqual(self.upload(text.encode()).mapping["2"], "new:text:besok")
        # Ett befintligt datumfält med samma rubrik väljs ändå.
        FieldDef.objects.create(
            account=self.account, key="besok", label="Besök", kind=FieldDef.Kind.DATE
        )
        job = self.flow(text)
        self.assertEqual(job.mapping["2"], "field:besok")
        self.assertEqual(job.status, S.DONE)
        self.assertEqual((job.counts["new"], job.counts["errors"]), (11, 0))
        self.assertEqual(job.counts["values"], 2)
        self.assertIn("2 värden i extrafält sparas inte", self.review)
        okand = Contact.objects.get(account=self.account, phone="+46701740011")
        self.assertEqual(okand.fields, {})
        person = Contact.objects.get(account=self.account, phone="+46701740001")
        self.assertEqual(person.fields, {"besok": "2025-01-02", "anteckning": "kort"})
        reasons = [e["reason"] for e in job.errors]
        self.assertTrue(all(r.endswith(importer.VALUE_SKIPPED_TEXT) for r in reasons), reasons)
        self.assertEqual({e["column"] for e in job.errors}, {"Besök", "Anteckning"})
        client = self.client_for(self.anna)
        done = client.get(self.job_url(job)).content.decode()
        self.assertIn("2 värden i extrafält sparades inte", done)
        self.assertIn("Ladda ner felrapporten", done)
        body = client.get(reverse("flamingo:app_import_errors", args=[job.pk])).content
        self.assertIn(importer.VALUE_SKIPPED_TEXT, body.decode("utf-8-sig"))
        self.assertEqual(importer.error_total(job), 2)

    def test_a_personnummer_in_an_extra_field_is_never_stored(self):
        header = ["Namn", "Mobil", "Kundnummer"]
        rows = [["Anna", phone(1), "A-1"], ["Bo", phone(2), pnr("900101")]]
        job = self.flow(csv_text(header, rows), columns={"2": "new:text:kundnummer"})
        self.assertEqual(job.counts["new"], 2)
        bo = Contact.objects.get(account=self.account, phone="+46701740002")
        self.assertEqual(bo.fields, {})

    def test_xlsx_with_a_wrong_dimension_keeps_every_row(self):
        content = xlsx_bytes(
            ["Namn", "Mobil", "Ort"], [["Anna", phone(1), "Nacka"], ["Bo", phone(2), "Solna"]]
        )
        source = zipfile.ZipFile(io.BytesIO(content))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml":
                    data = re.sub(rb'<dimension ref="[^"]*"', b'<dimension ref="A1"', data)
                    self.assertIn(b'<dimension ref="A1"', data)
                target.writestr(item, data)
        job = self.upload(buffer.getvalue(), name="kunder.xlsx")
        self.assertEqual(job.status, S.MAPPING)
        self.assertEqual(job.row_count, 2)
        self.assertEqual(job.header, ["Namn", "Mobil", "Ort"])
        self.assertEqual(job.sample[1], ["Bo", "0701740002", "Solna"])

    def test_telefon_before_mobil_maps_the_mobile_column(self):
        header = ["Namn", "Telefon", "Mobil"]
        rows = [
            ["Anna", "08-465 004 01", phone(1)],
            ["Bo", "08-465 004 02", phone(2)],
        ]
        job = self.upload(csv_text(header, rows).encode())
        self.assertEqual(job.mapping["2"], "phone")
        self.assertEqual(job.mapping["1"], "new:text:telefon")
        # Och tvärtom: mobilnumren i Telefon, fasta nummer i Mobil.
        swapped = [["Anna", phone(1), "08-465 004 01"], ["Bo", phone(2), "08-465 004 02"]]
        job = self.upload(csv_text(header, swapped).encode())
        self.assertEqual(job.mapping["1"], "phone")
        self.assertNotEqual(job.mapping["2"], "phone")

    def test_a_column_without_a_header_is_kept(self):
        text = "Namn;Mobil\r\nAnna;0701740001;ringer helst kvällstid\r\nBo;0701740002\r\n"
        job = self.upload(text.encode())
        self.assertEqual(job.header, ["Namn", "Mobil", "Kolumn 3"])
        self.assertEqual(job.sample[0], ["Anna", "0701740001", "ringer helst kvällstid"])
        self.assertEqual(job.sample[1], ["Bo", "0701740002", ""])
        self.assertEqual(job.mapping["2"], "new:text:kolumn-3")

    def test_a_job_stuck_while_the_file_was_read_is_stopped(self):
        job = self.upload(csv_text(["Mobil"], [[phone(1)]]).encode())
        stale = timezone.now() - importer.ORPHAN_AFTER - timedelta(minutes=1)
        ImportJob.objects.filter(pk=job.pk).update(status=S.UPLOADED, created_at=stale)
        self.assertTrue(self.files())
        self.assertTrue(importer.work_exists())
        self.assertEqual(importer.recover(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, S.FAILED)
        self.assertEqual(importer.failure_text(job), importer.STUCK_TEXT)
        self.assertEqual(self.files(), [])
        self.assertFalse(importer.work_exists())
        # Ett färskt jobb på uploaded (förfrågan läser fortfarande) rörs inte.
        fresh = self.upload(csv_text(["Mobil"], [[phone(2)]]).encode())
        ImportJob.objects.filter(pk=fresh.pk).update(status=S.UPLOADED)
        self.assertEqual(importer.recover(), 0)

    def test_a_re_import_keeps_the_inactive_flag(self):
        old = timezone.now() - timedelta(days=800)
        kontakt = make_contact(self.account, first_name="Anna", phone=PHONE_ANNA)
        Contact.objects.filter(pk=kontakt.pk).update(
            last_activity_at=old, inactive_flagged_at=timezone.now()
        )
        job = self.flow(csv_text(["Namn", "Mobil"], [["Anna", "0701740601"]]))
        self.assertEqual(job.counts["updated"], 1)
        kontakt.refresh_from_db()
        self.assertIsNotNone(kontakt.inactive_flagged_at)
        self.assertEqual(kontakt.last_activity_at, old)
        self.assertTrue(Event.objects.filter(contact=kontakt, kind=Event.IMPORTED).exists())

    def test_the_error_csv_header_cannot_become_a_formula(self):
        header = ['=HYPERLINK("http://x.example","Mobil")', "Namn"]
        job = self.flow(csv_text(header, [["123", "Bo"]]), columns={"0": "phone"})
        body = (
            self.client_for(self.anna)
            .get(reverse("flamingo:app_import_errors", args=[job.pk]))
            .content.decode("utf-8-sig")
        )
        first = next(csv.reader(io.StringIO(body), delimiter=";"))
        self.assertEqual(first[:3], ["Rad", "Kolumn", "Orsak"])
        self.assertTrue(first[3].startswith("'=HYPERLINK("), first)

    def test_the_timeline_shows_the_file_name(self):
        from . import timeline

        self.flow(csv_text(["Namn", "Mobil"], [["Anna", "0701740601"]]))
        kontakt = Contact.objects.get(account=self.account)
        details = [item.detail for item in timeline.for_contact(kontakt).items]
        self.assertIn("kunder.csv", details)
        self.assertFalse([d for d in details if re.search(r"\bImport \d", d)])


class SystemActorTests(UtskickFixture, TestCase):
    def test_job_actor(self):
        job = ImportJob(account=self.account, created_by=self.staff, created_as_staff=True)
        self.assertEqual(importer.job_actor(job).label, "ADX (byra)")
        self.assertTrue(importer.job_actor(job).staff)
        self.assertEqual(importer.job_actor(ImportJob(account=self.account)), SYSTEM)

    def test_suggested_tag_name(self):
        moment = datetime.datetime(2026, 10, 9, 12, tzinfo=datetime.UTC)
        self.assertEqual(importer.suggested_tag_name(moment), "Import okt 2026")

    def test_sniff_and_decode(self):
        self.assertEqual(importer.sniff_delimiter("a;b\nc;d"), ";")
        self.assertEqual(importer.sniff_delimiter("a,b\nc,d"), ",")
        self.assertEqual(importer.sniff_delimiter('Namn;Ort\n"Berg, Lisa";Kil'), ";")
        self.assertEqual(importer.sniff_delimiter("bara en kolumn\nx"), ",")
        self.assertEqual(importer.decode("Åsa".encode("cp1252")), ("Åsa", "cp1252"))
        self.assertEqual(importer.decode("Åsa".encode()), ("Åsa", "utf-8"))

    def test_header_target(self):
        self.assertEqual(importer.header_target("Mobil (privat)"), "phone")
        self.assertEqual(importer.header_target("E-postadress"), "email")
        self.assertEqual(importer.header_target("Pnr"), "pnr")
        self.assertEqual(importer.header_target("Kund sedan"), "")
        self.assertEqual(importer.header_target("Kundnummer"), "")
        self.assertEqual(importer.header_target("Företagsnamn"), "company_name")


class ImportTemplateGuardTests(TestCase):
    """Mallarna i kontakter/import/, css:en och skriptet (README I.2 och I.3)."""

    FOLDER = Path(settings.BASE_DIR) / "templates" / "flamingo" / "app" / "kontakter" / "import"

    def templates(self):
        paths = sorted(self.FOLDER.glob("*.html"))
        self.assertGreaterEqual(len(paths), 7)
        return paths

    def test_no_inline_styles_or_scripts_and_labelled_cells(self):
        for path in self.templates():
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("style=", text)
                self.assertNotRegex(text, r"<script(?![^>]*\ssrc=)")
                for match in re.finditer(r"<td\b[^>]*>", text):
                    self.assertIn("data-label=", match.group(0))

    def test_copy_has_no_exclamation_marks_or_brackets(self):
        for path in self.templates():
            text = path.read_text(encoding="utf-8")
            text = re.sub(r"{% comment %}.*?{% endcomment %}", "", text, flags=re.S)
            text = re.sub(r"{%.*?%}|{{.*?}}|{#.*?#}", "", text, flags=re.S)
            text = re.sub(r"<[^>]+>", "", text)
            with self.subTest(path=path.name):
                self.assertNotIn("!", text)
                self.assertNotRegex(text, r"\[\s*\]")

    def test_static_files_exist(self):
        base = Path(settings.BASE_DIR) / "static"
        for name in (
            "css/flamingo-app-import.css",
            "js/flamingo-app-import.js",
            "utskick/kontakter-mall.xlsx",
        ):
            self.assertTrue((base / name).is_file(), name)
