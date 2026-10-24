"""Lista pública de servidores publicada pelo portal oficial de cada região."""
from dataclasses import dataclass
from html.parser import HTMLParser
import re
import urllib.error
import urllib.request

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

REGIONS = {
    "lobr": "Brasil",
    "lopl": "Polska",
    "lode": "Deutschland",
    "loes": "España",
    "lonl": "Nederland",
    "lotr": "Türkiye",
    "loar": "العربية",
}

_SERVER_HREF = re.compile(r"^/serverlist/s(\d+)$")
# Link de "jogar rápido" do portal; não é um servidor real.
_PLACEHOLDER_IDS = {9999}


class ServerListError(RuntimeError):
    pass


@dataclass(frozen=True)
class Server:
    id: int
    code: str
    name: str

    @property
    def label(self):
        return f"{self.code} — {self.name}" if self.code else self.name


def portal_url(region):
    if region not in REGIONS:
        raise ValueError(f"região desconhecida: {region}")
    return f"https://{region}.creaction-network.com"


def play_url(region, server_id):
    return f"{portal_url(region)}/serverlist/s{int(server_id)}"


class _ServerLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = {}
        self._current = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        match = _SERVER_HREF.match(dict(attrs).get("href") or "")
        if match:
            self._current = int(match.group(1))
            self._text = []

    def handle_data(self, data):
        if self._current is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or self._current is None:
            return
        text = " ".join("".join(self._text).split())
        if text and self._current not in _PLACEHOLDER_IDS:
            self.found.setdefault(self._current, text)
        self._current = None


def parse_servers(html):
    """Extrai os servidores dos links /serverlist/sN, do mais novo ao mais antigo."""
    parser = _ServerLinks()
    parser.feed(html)
    servers = []
    for server_id, text in parser.found.items():
        code, sep, name = text.partition(":")
        if not sep:
            code, name = "", text
        servers.append(Server(server_id, code.strip(), name.strip()))
    return sorted(servers, key=lambda server: server.id, reverse=True)


def fetch_servers(region, timeout=30):
    request = urllib.request.Request(
        f"{portal_url(region)}/serverlist",
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            html = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise ServerListError(f"portal respondeu HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ServerListError(f"falha de rede: {exc}") from exc
    servers = parse_servers(html)
    if not servers:
        raise ServerListError("a página do portal não contém servidores")
    return servers
