"""
Testkörningen når aldrig nätet (apps/utskick/README.md, C.3).

Testerna läser utvecklarens .env, som kan ha riktiga nycklar till 46elks,
AWS och Google. NoNetworkRunner gör därför tre saker för hela körningen:

- socket.connect (och connect_ex) vägrar allt utom unix-socklar och
  loopback (Postgres på localhost går fram);
- namnuppslag (getaddrinfo) vägrar andra namn än localhost och IP-adresser,
  så att inte ens en DNS-fråga lämnar datorn;
- ADX_AWS_PROFILE="__test__", SMS_SEND_LIVE=False och UTSKICK_EMAIL_LIVE=False
  gäller oavsett .env, och UTSKICK_TICK_MAX_MB=0 (ticken sätter inget
  minnestak på testprocessen). Från S3 är också rollen för utskick, köerna
  och hinken för inkommande post tomma (UTSKICK_AWS_ROLE_ARN,
  UTSKICK_SQS_*, UTSKICK_SES_INBOUND_BUCKET): ett test som vill pröva dem
  sätter egna värden med override_settings och en attrapp för boto3.
  UTSKICK_KICK=False: webbanropet för inkommande sms startar ingen
  bakgrundstråd (testerna kör apps/utskick/sending/kick.run synkront).

Ett test som vill pröva en integration lägger sin egen attrapp ovanpå
(FakeElks, FakeSes, mock av urlopen). Ett försök att nå nätet blir
ConnectionRefusedError och skrivs ut en gång per adress, så att det syns
vilket test som försökte.
"""

import ipaddress
import socket
import sys

from django.test.runner import DiscoverRunner, ParallelTestSuite
from django.test.utils import override_settings

_LOCAL_NAMES = frozenset({"", "localhost", "localhost.localdomain", "ip6-localhost"})


class NetworkBlocked(ConnectionRefusedError):
    """Ett test försökte nå nätet."""


def _is_loopback(host):
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    host = str(host).strip("[]").lower()
    if host in _LOCAL_NAMES or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _is_ip_literal(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    try:
        ipaddress.ip_address(str(host).strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    return True


class _Guard:
    """Lappar socket-modulen och lägger tillbaka den efteråt."""

    def __init__(self):
        self.seen = set()
        self._connect = socket.socket.connect
        self._connect_ex = socket.socket.connect_ex
        self._getaddrinfo = socket.getaddrinfo

    def _refuse(self, where):
        if where not in self.seen:
            self.seen.add(where)
            print(f"\n[no-network] Testet försökte nå {where}: nekat.", file=sys.stderr)
        raise NetworkBlocked(f"Testerna får inte nå nätet ({where}).")

    def _allowed(self, sock, address):
        if sock.family == getattr(socket, "AF_UNIX", object()):
            return True
        host = address[0] if isinstance(address, tuple) and address else address
        return _is_loopback(host)

    def install(self):
        guard = self

        def connect(sock, address):
            if not guard._allowed(sock, address):
                guard._refuse(repr(address))
            return guard._connect(sock, address)

        def connect_ex(sock, address):
            if not guard._allowed(sock, address):
                guard._refuse(repr(address))
            return guard._connect_ex(sock, address)

        def getaddrinfo(host, *args, **kwargs):
            if not (_is_loopback(host) or _is_ip_literal(host)):
                guard._refuse(f"namnet {host!r}")
            return guard._getaddrinfo(host, *args, **kwargs)

        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        socket.getaddrinfo = getaddrinfo

    def uninstall(self):
        socket.socket.connect = self._connect
        socket.socket.connect_ex = self._connect_ex
        socket.getaddrinfo = self._getaddrinfo


#: Inställningar som gäller hela körningen, oavsett .env.
SAFE_SETTINGS = {
    "ADX_AWS_PROFILE": "__test__",
    "SMS_SEND_LIVE": False,
    "UTSKICK_EMAIL_LIVE": False,
    # Ett test som kör utskick_tick får aldrig sätta minnestaket på hela
    # testkörningen (RLIMIT_AS gäller processen).
    "UTSKICK_TICK_MAX_MB": 0,
    # S3: ingen roll, inga köer och ingen hink ur utvecklarens .env.
    "UTSKICK_AWS_ROLE_ARN": "",
    "UTSKICK_SQS_EVENTS_URL": "",
    "UTSKICK_SQS_INBOUND_URL": "",
    "UTSKICK_SES_INBOUND_BUCKET": "",
    # Knuffen efter webbanropet (apps/utskick/sending/kick.py) startar ingen
    # tråd i testerna: testerna anropar kick.run själva.
    "UTSKICK_KICK": False,
}


def _worker_setup(*args):
    """Varje arbetsprocess med --parallel (spawn på macOS ärver inget).

    Django anropar den här före sin egen django.setup() i arbetsprocessen,
    och då är inställningarna inte inlästa än: override_settings skulle
    lägga sig ovanpå en tom platshållare (LOGGING_CONFIG saknas och varje
    arbetare dör). Därför läses inställningarna in först; Djangos andra
    django.setup() efteråt gör ingenting nytt."""
    import django

    django.setup()
    _Guard().install()
    override_settings(**SAFE_SETTINGS).enable()


class NoNetworkParallelSuite(ParallelTestSuite):
    process_setup = _worker_setup


class NoNetworkRunner(DiscoverRunner):
    """DiscoverRunner utan nät, se modulens docstring."""

    parallel_test_suite = NoNetworkParallelSuite

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._no_network = _Guard()
        self._no_network.install()
        self._safe_settings = override_settings(**SAFE_SETTINGS)
        self._safe_settings.enable()

    def teardown_test_environment(self, **kwargs):
        self._safe_settings.disable()
        self._no_network.uninstall()
        super().teardown_test_environment(**kwargs)
