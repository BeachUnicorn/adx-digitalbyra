"""
Excel till CSV i en egen process (README J S1, "Import details").

importer.convert kör filen som ett skript, aldrig som en import i
webbarbetaren eller ticken:

    python -I /.../apps/utskick/xlsx2csv.py <in.xlsx> <out.csv>

med 60 sekunders tidsgräns och en tom miljö. Processen sätter själv
RLIMIT_AS till 512 MB innan openpyxl läses in, så att en fil som är byggd för
att äta minne bara fäller den här processen. -I gör att varken PYTHONPATH,
arbetskatalogen eller användarens site-packages följer med: filen använder
bara standardbiblioteket och openpyxl, som tolkar XML med defusedxml när det
är installerat (det är det, pyproject.toml). Därför skriptsökväg och inte
"-m apps.utskick.xlsx2csv": med -I hittar Python inte paketet apps.

Innan barnprocessen startar har importer.convert redan öppnat filen med
zipfile och nekat zip-bomber (total storlek, packningsgrad, sharedStrings).

Utdata är UTF-8-CSV med radnumret i bladet först och cellerna efter, en rad
per rad i bladet. Tomma rader hoppas över, radbrytningar i celler blir
mellanslag och celler kortas till CELL_MAX tecken. importer.convert
normaliserar resten precis som för en CSV-fil.

Slutkoder: 0 klart, 2 för många rader, 3 filen gick inte att läsa, 64 fel
anrop. Inga värden skrivs till stderr (de är personuppgifter).
"""

import csv
import datetime
import sys

#: Högsta antal rader med data (utöver rubrikraden), samma som importer.MAX_ROWS.
MAX_ROWS = 50_000
#: Högsta antal kolumner som läses (importer.MAX_COLUMNS).
MAX_COLUMNS = 100
#: Längsta cell som sparas.
CELL_MAX = 1000
#: Så här många tomma rader i följd räknas som slutet på bladet (en fil
#: som säger att den har en miljon rader ska inte hålla processen i gång).
EMPTY_RUN_MAX = 10_000
#: Minnestaket för processen (RLIMIT_AS).
MEMORY_MB = 512

EXIT_OK = 0
EXIT_TOO_MANY_ROWS = 2
EXIT_UNREADABLE = 3
EXIT_USAGE = 64


class TooManyRows(Exception):
    pass


def limit_memory(megabytes=MEMORY_MB):
    """RLIMIT_AS för den här processen. Linux (servern) följer det; macOS
    vägrar sänka taket, och då gäller bara tidsgränsen."""
    try:
        import resource

        limit = megabytes * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, ValueError, OSError):
        pass


def cell_text(value):
    """En cell som text: datum som åååå-mm-dd, heltal utan decimaler,
    sant och falskt som ja och nej."""
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "ja" if value else "nej"
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            text = str(int(value))
        else:
            text = repr(value)
    elif isinstance(value, datetime.datetime):
        if (value.hour, value.minute, value.second) == (0, 0, 0):
            text = value.date().isoformat()
        else:
            text = value.strftime("%Y-%m-%d %H:%M")
    elif isinstance(value, datetime.date):
        text = value.isoformat()
    elif isinstance(value, datetime.time):
        text = value.strftime("%H:%M")
    else:
        text = str(value)
    text = text.replace("\x00", "")
    text = " ".join(text.split()) if any(ch in text for ch in "\r\n\t") else text.strip()
    return text[:CELL_MAX]


def convert(src, dst):
    """Läs det aktiva bladet i src (xlsx) och skriv dst. TooManyRows när
    bladet har fler än MAX_ROWS rader med data efter rubrikraden."""
    import openpyxl

    workbook = openpyxl.load_workbook(src, read_only=True, data_only=True)
    try:
        sheet = workbook.active or workbook.worksheets[0]
        # Bladets egen storleksuppgift (<dimension>) kan vara fel och skulle
        # då kapa rader och kolumner: läs till slutet i stället. Raderna
        # begränsas av MAX_ROWS och EMPTY_RUN_MAX, kolumnerna här (en
        # kolumn extra, så att importen kan neka för många kolumner).
        if hasattr(sheet, "reset_dimensions"):
            sheet.reset_dimensions()
        written = 0
        empty_run = 0
        with open(dst, "w", encoding="utf-8", newline="") as out:
            writer = csv.writer(out, lineterminator="\n")
            rows = sheet.iter_rows(max_col=MAX_COLUMNS + 1, values_only=True)
            for number, row in enumerate(rows, start=1):
                cells = [cell_text(v) for v in row[: MAX_COLUMNS + 1]]
                while cells and not cells[-1]:
                    cells.pop()
                if not cells:
                    empty_run += 1
                    if empty_run > EMPTY_RUN_MAX:
                        break
                    continue
                empty_run = 0
                written += 1
                if written > MAX_ROWS + 1:
                    raise TooManyRows
                writer.writerow([number, *cells])
    finally:
        workbook.close()
    return EXIT_OK


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        return EXIT_USAGE
    limit_memory()
    try:
        return convert(argv[0], argv[1])
    except TooManyRows:
        return EXIT_TOO_MANY_ROWS
    except Exception as exc:  # noqa: BLE001 - allt annat är en fil vi inte kan läsa
        print(f"xlsx2csv: {type(exc).__name__}", file=sys.stderr)
        return EXIT_UNREADABLE


if __name__ == "__main__":
    sys.exit(main())
