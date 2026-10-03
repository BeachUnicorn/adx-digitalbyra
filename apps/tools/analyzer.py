"""
Hemsidekollen: teknisk granskning av en extern webbplats.

Tre kontrollgrupper i EN rapport - teknik, e-post/domän, tillgänglighet -
och bara sådant som går att verifiera server-side. Inga gissade betyg,
inga påhittade mätvärden: varje rad är en observation med källa.

SÄKERHET: verktyget hämtar adresser en användare skriver in (och ADX
Flamingo läser kundens hemsida med samma fetch). Utan skydd är det en
SSRF-kanon riktad mot vårt eget nät (169.254.169.254 är metadata-tjänsten
på EC2 med instansrollens nycklar). Därför, för VARJE hopp:
  * bara http/https, port 80/443
  * värdnamnet löses upp FÖRE anropet och varje IP måste vara publik
  * anslutningen görs till just den kontrollerade IP:n (Host-huvudet och
    TLS-namnet är fortfarande värdnamnet), så en ny DNS-uppslagning mellan
    kontrollen och anropet (DNS rebinding) kan inte byta mål
  * redirects följs manuellt, högst MAX_REDIRECTS, och varje ny adress går
    igenom samma kontroller innan något anrop görs
  * svaret läses i bitar, högst max_bytes, och hela hämtningen har en
    tidsgräns (time_limit), inte bara varje enskild läsning
"""

import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

MAX_BYTES = 2 * 1024 * 1024
#: Sekunder för varje enskild nätverksoperation (anslutning, en läsning).
TIMEOUT = 10
#: Sekunder för hela hämtningen: alla hopp och hela svaret.
TIME_LIMIT = 20
MAX_REDIRECTS = 5
ALLOWED_PORTS = (80, 443)
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
CHUNK = 64 * 1024
USER_AGENT = "ADX Hemsidekollen (+https://adx.se/)"

#: DKIM-selektorer som täcker de vanligaste leverantörerna. En träff räcker;
#: att ingen hittas betyder "ingen av de vanliga", inte "saknas garanterat" -
#: och så formuleras det i rapporten.
DKIM_SELECTORS = ["default", "google", "selector1", "selector2", "k1", "s1", "s2", "mail"]


class AnalysError(Exception):
    """Fel som ska visas för användaren (ogiltig adress, privat IP, ...)."""


@dataclass
class Sida:
    url: str = ""
    status: int = 0
    ms: int = 0
    bytes: int = 0
    html: str = ""
    headers: dict = field(default_factory=dict)
    redirects: list = field(default_factory=list)
    #: Svaret som bytes (högst max_bytes), för det som inte är text (bilderna
    #: i ADX Flamingos mediaarkiv). html är samma bytes avkodade som UTF-8.
    body: bytes = b""
    #: Svaret var större än max_bytes och är avkortat.
    truncated: bool = False


#: IPv6-adresser som bär en IPv4-adress inuti (NAT64): prövas som IPv4.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _blocked(ip):
    """Ska vi vägra ansluta till den här adressen? Allt som inte är en
    vanlig publik adress: privata nät, loopback, länklokalt (EC2-metadata),
    reserverat, multicast, delade nät (100.64/10) och dokumentationsnät.
    En IPv4-adress förklädd som IPv6 (::ffff:..., 64:ff9b::..., 2002:...)
    prövas som den IPv4-adress den är."""
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    )


def _assert_public(host):
    """Alla IP:n värden pekar på måste vara publika - annars vägrar vi.
    Returnerar den första (kontrollerade) IP:n, den som fetch ansluter till."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise AnalysError(f"Kunde inte slå upp {host}.") from exc
    if not infos:
        raise AnalysError(f"Kunde inte slå upp {host}.")
    for info in infos:
        if _blocked(ipaddress.ip_address(info[4][0])):
            raise AnalysError("Adressen pekar på ett internt nät och granskas inte.")
    return infos[0][4][0]


def _port_of(parts):
    try:
        return parts.port
    except ValueError:
        raise AnalysError("Bara standardportarna 80 och 443 granskas.") from None


def normalize_url(raw):
    raw = (raw or "").strip()
    if not raw:
        raise AnalysError("Ange en adress.")
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise AnalysError("Bara http och https granskas.")
    if not parts.hostname or "." not in parts.hostname:
        raise AnalysError("Det där ser inte ut som ett domännamn.")
    if _port_of(parts) not in (None, *ALLOWED_PORTS):
        raise AnalysError("Bara standardportarna 80 och 443 granskas.")
    return f"{parts.scheme}://{parts.netloc}{parts.path or '/'}"


@dataclass(frozen=True)
class _Target:
    """En kontrollerad adress: vart anropet går och vilken IP det går till."""

    url: str
    scheme: str
    host: str
    port: int
    path: str
    ip: str


def _target(url):
    """Kontrollera en adress (den första eller ett redirect-hopp) och lös
    upp den. Inget anrop görs här; fetch ansluter sedan till .ip."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise AnalysError("Bara http och https granskas.")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise AnalysError("Det där ser inte ut som ett domännamn.")
    port = _port_of(parts)
    if port not in (None, *ALLOWED_PORTS):
        raise AnalysError("Bara standardportarna 80 och 443 granskas.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise AnalysError("Det där ser inte ut som ett domännamn.") from None
    port = port or (443 if parts.scheme == "https" else 80)
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parts.query:
        path += "?" + quote(parts.query, safe="/%:@!$&'()*+,;=-._~?")
    return _Target(url, parts.scheme, host, port, path, _assert_public(host))


def _assert_host(url, hosts):
    """Går url till ett av värdnamnen i hosts? Annars AnalysError."""
    try:
        host = (urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        host = ""
    if host not in hosts:
        raise AnalysError("Adressen går till en annan webbplats än den som får hämtas.")


def _pinned_connection(target, timeout):
    """En HTTP(S)-anslutning som går till target.ip. Host-huvudet,
    TLS-namnet (SNI) och certifikatkontrollen gäller fortfarande värdnamnet:
    http.client tar dem från conn.host, bara själva uppkopplingen byts ut."""
    if target.scheme == "https":
        conn = http.client.HTTPSConnection(
            target.host, target.port, timeout=timeout, context=ssl.create_default_context()
        )
    else:
        conn = http.client.HTTPConnection(target.host, target.port, timeout=timeout)

    def connect_to_checked_ip(address, timeout=None, source_address=None):
        return socket.create_connection((target.ip, address[1]), timeout, source_address)

    conn._create_connection = connect_to_checked_ip
    return conn


class _Watchdog:
    """Stänger anslutningen när tiden för hela hämtningen är slut, även mitt
    i en läsning (en server som skickar en byte i taget håller annars
    tråden kvar så länge den vill)."""

    def __init__(self, conn, seconds):
        self.fired = threading.Event()
        self._conn = conn
        self._timer = threading.Timer(max(0.0, seconds), self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self):
        self.fired.set()
        sock = self._conn.sock
        if sock is not None:
            try:
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except OSError:
                pass

    def cancel(self):
        self._timer.cancel()


def _too_slow():
    return AnalysError("Sidan svarade inte i tid.")


def _request(target, deadline, max_bytes):
    """Ett anrop till en kontrollerad adress: (status, headers, body).
    body är högst max_bytes + 1 byte (så avkortning syns)."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _too_slow()
    conn = _pinned_connection(target, timeout=min(TIMEOUT, remaining))
    watchdog = _Watchdog(conn, remaining)
    try:
        conn.request(
            "GET",
            target.path,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "close"},
        )
        response = conn.getresponse()
        headers = {k.lower(): v for k, v in response.getheaders()}
        status = response.status
        chunks, size = [], 0
        if status not in REDIRECT_STATUSES:
            while size <= max_bytes:
                if time.monotonic() >= deadline:
                    raise _too_slow()
                chunk = response.read1(min(CHUNK, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
        if watchdog.fired.is_set():
            raise _too_slow()
        return status, headers, b"".join(chunks)
    except AnalysError:
        raise
    except Exception as exc:  # noqa: BLE001 - nätverksfel ÄR resultatet
        if watchdog.fired.is_set() or time.monotonic() >= deadline:
            raise _too_slow() from exc
        raise AnalysError(f"Kunde inte hämta {target.url}: {exc}") from exc
    finally:
        watchdog.cancel()
        conn.close()


def fetch(url, *, max_bytes=MAX_BYTES, time_limit=TIME_LIMIT, hosts=None):
    """Hämta en sida med SSRF-skydd och manuell redirect-följning.

    Varje hopp kontrolleras (schema, port, publik IP) innan något anrop görs
    och ansluts till den IP som kontrollerades. Hela hämtningen, alla hopp
    och hela svaret, får ta högst time_limit sekunder; svaret avkortas vid
    max_bytes. Med hosts får bara de värdnamnen (exakt) anropas, också
    efter en omdirigering: ett annat ger AnalysError innan det slås upp
    eller anropas (för en hämtning som bara får gå till en känd sajt)."""
    deadline = time.monotonic() + time_limit
    sida = Sida()
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if hosts is not None:
            _assert_host(current, hosts)
        target = _target(current)
        start = time.monotonic()
        status, headers, body = _request(target, deadline, max_bytes)
        location = headers.get("location", "")
        if status in REDIRECT_STATUSES and location:
            sida.redirects.append(current)
            current = urljoin(current, location.strip())
            continue
        if not 200 <= status < 300:
            raise AnalysError(f"Kunde inte hämta {current}: HTTP {status}")
        sida.status = status
        sida.headers = headers
        sida.url = current
        sida.ms = int((time.monotonic() - start) * 1000)
        sida.bytes = min(len(body), max_bytes)
        sida.body = body[:max_bytes]
        sida.truncated = len(body) > max_bytes
        sida.html = sida.body.decode("utf-8", "replace")
        return sida
    raise AnalysError("För många omdirigeringar.")


class _Extractor(HTMLParser):
    """Plockar ut det kontrollerna behöver ur HTML utan externa beroenden."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta_description = ""
        self.lang = ""
        self.viewport = ""
        self.generator = ""
        self.headings = []
        self.images = []
        self.inputs = []
        self.labels_for = set()
        self.links = []
        self.has_main = False
        self._in_title = False
        self._current_heading = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang", "")
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (a.get("name") or "").lower()
            if name == "description":
                self.meta_description = a.get("content", "")
            elif name == "viewport":
                self.viewport = a.get("content", "")
            elif name == "generator":
                self.generator = a.get("content", "")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.headings.append(int(tag[1]))
        elif tag == "img":
            self.images.append(
                {"alt": a.get("alt"), "loading": a.get("loading"), "src": a.get("src", "")}
            )
        elif tag in ("input", "textarea", "select"):
            if a.get("type") in ("hidden", "submit", "button"):
                return
            self.inputs.append(
                {
                    "id": a.get("id"),
                    "aria": bool(a.get("aria-label") or a.get("aria-labelledby")),
                    "title": bool(a.get("title")),
                }
            )
        elif tag == "label" and a.get("for"):
            self.labels_for.add(a["for"])
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "main":
            self.has_main = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _rad(status, titel, detalj=""):
    """En kontrollrad: ok/varning/fel + vad vi såg."""
    return {"status": status, "titel": titel, "detalj": detalj}


def check_teknik(sida, extractor):
    rader = []
    ok, varn, fel = "ok", "varning", "fel"

    rader.append(
        _rad(
            ok if sida.ms < 1500 else varn if sida.ms < 3000 else fel,
            "Svarstid",
            f"{sida.ms} ms till första svaret (mätt från vår server).",
        )
    )
    kb = sida.bytes // 1024
    rader.append(
        _rad(
            ok if kb < 500 else varn if kb < 1500 else fel,
            "Sidvikt (HTML)",
            f"{kb} kB HTML{' (avkortat vid 2 MB)' if sida.bytes >= MAX_BYTES else ''}.",
        )
    )
    rader.append(
        _rad(
            ok if sida.url.startswith("https://") else fel,
            "HTTPS",
            f"Slutlig adress: {sida.url}",
        )
    )

    title = extractor.title.strip()
    if not title:
        rader.append(_rad(fel, "Sidtitel", "Saknas helt - det är rubriken i sökresultatet."))
    elif len(title) > 65:
        rader.append(
            _rad(varn, "Sidtitel", f"{len(title)} tecken - klipps i sökresultatet: {title[:70]}")
        )
    else:
        rader.append(_rad(ok, "Sidtitel", title))

    desc = extractor.meta_description.strip()
    if not desc:
        rader.append(_rad(varn, "Metabeskrivning", "Saknas - Google hittar på en egen."))
    elif len(desc) > 165:
        rader.append(_rad(varn, "Metabeskrivning", f"{len(desc)} tecken - klipps i sökresultatet."))
    else:
        rader.append(_rad(ok, "Metabeskrivning", f"{len(desc)} tecken."))

    h1 = extractor.headings.count(1)
    rader.append(
        _rad(
            ok if h1 == 1 else varn,
            "H1-rubrik",
            f"{h1} stycken - en per sida är riktmärket." if h1 != 1 else "Exakt en, som det ska.",
        )
    )

    if not extractor.viewport:
        rader.append(
            _rad(fel, "Mobilanpassning", "Ingen viewport-meta - sidan förminskas på mobil.")
        )
    else:
        rader.append(_rad(ok, "Mobilanpassning", "Viewport-meta finns."))

    gen = extractor.generator
    if gen:
        rader.append(
            _rad(varn if "wordpress" in gen.lower() else ok, "Plattform", f"Uppger: {gen}")
        )

    comp = sida.headers.get("content-encoding", "")
    rader.append(_rad(ok if comp else varn, "Komprimering", f"content-encoding: {comp or 'ingen'}"))
    return rader


def check_tillganglighet(extractor):
    rader = []
    ok, varn, fel = "ok", "varning", "fel"

    if not extractor.lang:
        rader.append(
            _rad(fel, "Språkangivelse", "<html> saknar lang-attribut - skärmläsare gissar uttal.")
        )
    else:
        rader.append(_rad(ok, "Språkangivelse", f'lang="{extractor.lang}"'))

    imgs = extractor.images
    utan_alt = [i for i in imgs if i["alt"] is None]
    if imgs:
        rader.append(
            _rad(
                ok if not utan_alt else varn if len(utan_alt) < len(imgs) / 2 else fel,
                "Alt-texter",
                f"{len(imgs) - len(utan_alt)} av {len(imgs)} bilder har alt-attribut.",
            )
        )

    inputs = extractor.inputs
    omärkta = [
        i
        for i in inputs
        if not (i["aria"] or i["title"] or (i["id"] and i["id"] in extractor.labels_for))
    ]
    if inputs:
        rader.append(
            _rad(
                ok if not omärkta else fel,
                "Formuläretiketter",
                (
                    f"{len(omärkta)} av {len(inputs)} fält saknar etikett - "
                    "de är stumma i en skärmläsare."
                )
                if omärkta
                else f"Alla {len(inputs)} fält har etikett.",
            )
        )

    hopp = sum(1 for a, b in zip(extractor.headings, extractor.headings[1:]) if b - a > 1)
    if extractor.headings:
        rader.append(
            _rad(
                ok if not hopp else varn,
                "Rubrikordning",
                f"{hopp} hopp i rubriknivåerna (t.ex. H1 direkt till H3)."
                if hopp
                else "Nivåerna kommer i ordning.",
            )
        )

    if "user-scalable=no" in extractor.viewport.replace(" ", "") or re.search(
        r"maximum-scale=1(\.0)?\b", extractor.viewport
    ):
        rader.append(_rad(fel, "Zoom", "Viewporten förbjuder zoom - direkt WCAG-brott."))
    else:
        rader.append(_rad(ok, "Zoom", "Zoom är tillåten."))

    rader.append(
        _rad(
            ok if extractor.has_main else varn,
            "Landmärken",
            "main-element "
            + (
                "finns."
                if extractor.has_main
                else "saknas - skärmläsare kan inte hoppa till innehållet."
            ),
        )
    )
    return rader


def check_epost_doman(host):
    """DNS-hälsa för e-post: MX, SPF, DMARC, DKIM plus certifikatets utgång."""
    import dns.resolver

    rader = []
    ok, varn, fel = "ok", "varning", "fel"
    apex = re.sub(r"^www\.", "", host)

    def txt(name):
        try:
            return [
                b"".join(r.strings).decode("utf-8", "replace")
                for r in dns.resolver.resolve(name, "TXT", lifetime=TIMEOUT)
            ]
        except Exception:  # noqa: BLE001 - NXDOMAIN med mera ÄR svaret
            return []

    try:
        mx = sorted(
            (r.preference, str(r.exchange).rstrip("."))
            for r in dns.resolver.resolve(apex, "MX", lifetime=TIMEOUT)
        )
    except Exception:  # noqa: BLE001
        mx = []
    rader.append(
        _rad(
            ok if mx else varn,
            "MX (ta emot mejl)",
            ", ".join(m[1] for m in mx[:3])
            if mx
            else f"Inga MX-poster - {apex} kan inte ta emot e-post.",
        )
    )

    spf = [t for t in txt(apex) if t.lower().startswith("v=spf1")]
    rader.append(
        _rad(
            ok if spf else fel,
            "SPF",
            spf[0][:120]
            if spf
            else "Saknas - mottagare kan inte veta vilka servrar som får skicka för domänen.",
        )
    )

    dmarc = [t for t in txt(f"_dmarc.{apex}") if t.lower().startswith("v=dmarc1")]
    if not dmarc:
        rader.append(
            _rad(fel, "DMARC", "Saknas - utan policy går domänen att förfalska i avsändarfältet.")
        )
    elif "p=none" in dmarc[0].replace(" ", "").lower():
        rader.append(_rad(varn, "DMARC", f"Finns men p=none (övervakar bara): {dmarc[0][:100]}"))
    else:
        rader.append(_rad(ok, "DMARC", dmarc[0][:120]))

    hittade = [s for s in DKIM_SELECTORS if txt(f"{s}._domainkey.{apex}")]
    rader.append(
        _rad(
            ok if hittade else varn,
            "DKIM",
            f"Selektor hittad: {', '.join(hittade)}"
            if hittade
            else "Ingen av de vanliga selektorerna svarar - kan finnas under eget namn.",
        )
    )

    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
        exp = ssl.cert_time_to_seconds(cert["notAfter"])
        dagar = int((exp - time.time()) // 86400)
        rader.append(
            _rad(
                ok if dagar > 14 else varn if dagar > 0 else fel,
                "HTTPS-certifikat",
                f"Giltigt {dagar} dagar till.",
            )
        )
    except Exception as exc:  # noqa: BLE001
        rader.append(_rad(fel, "HTTPS-certifikat", f"Gick inte att verifiera: {exc}"))

    return rader


def analyze(raw_url):
    """Hela granskningen. Returnerar en JSON-vänlig rapport."""
    url = normalize_url(raw_url)
    sida = fetch(url)
    extractor = _Extractor()
    try:
        extractor.feed(sida.html)
    except Exception:  # noqa: BLE001 - trasig HTML är i sig ett fynd
        pass

    host = urlsplit(sida.url).hostname
    grupper = [
        {"namn": "Teknik och sökbarhet", "rader": check_teknik(sida, extractor)},
        {"namn": "Tillgänglighet", "rader": check_tillganglighet(extractor)},
        {"namn": "E-post och domän", "rader": check_epost_doman(host)},
    ]
    alla = [r for g in grupper for r in g["rader"]]
    return {
        "url": sida.url,
        "status": sida.status,
        "grupper": grupper,
        "summering": {
            "ok": sum(1 for r in alla if r["status"] == "ok"),
            "varningar": sum(1 for r in alla if r["status"] == "varning"),
            "fel": sum(1 for r in alla if r["status"] == "fel"),
        },
    }
