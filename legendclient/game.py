"""Abre o jogo no nosso host Flash, executado pelo Wine."""
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import shutil
import re
import signal
import struct
import subprocess
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from . import flash

PROJECT = Path(__file__).resolve().parents[1]
HOST_DIR = PROJECT / "flashhost"
# winegcc gera flashhost.exe (script de shell) e flashhost.exe.so (o programa);
# executamos o .so diretamente para que o PID acompanhado seja o do host.
HOST_EXE = PROJECT / "build/flashhost.exe.so"
DEFAULT_PREFIX = Path.home() / "Games/legend-online"
# Recursos do jogo baixados pelo Flash (ver flashhost/http_cache.c).
CACHE_DIR = PROJECT / ".local/http-cache"
CACHE_MAX_BYTES = 1 << 30
LOG = logging.getLogger("legendclient.game")

# Parâmetros HTML do <object>/<embed> → propriedades do controle ActiveX.
HTML_TO_ACTIVEX = {
    "wmode": "WMode",
    "quality": "Quality2",  # "Quality" é numérico no OCX; Quality2 aceita "high" etc.
    "scale": "Scale",
    "salign": "SAlign",
    "bgcolor": "BGColor",
    "menu": "Menu",
    "base": "Base",
    "allowscriptaccess": "AllowScriptAccess",
    "allowfullscreen": "AllowFullScreen",
    "allowfullscreeninteractive": "AllowFullScreenInteractive",
    "allownetworking": "AllowNetworking",
}


class GameError(RuntimeError):
    pass


@dataclass
class GameStart:
    movie: str
    flashvars: str = ""
    params: dict = field(default_factory=dict)


# Parâmetros que o game.jsp oficial repassa da sua URL para o Loading.swf.
GAME_FRAME_PARAMS = ("user", "key", "site", "p2", "rid", "logintype")


def start_from_game_frame(url):
    """Monta a inicialização do jogo a partir do endereço do game.jsp.

    O portal entrega, já autenticado, um iframe para
    https://sN.creaction-network.com/client/game.jsp?user=…&key=…&site=…
    e o game.jsp apenas carrega Loading.swf com esses valores em FlashVars.
    Reproduzimos essa chamada diretamente, sem depender da detecção de Flash
    da página.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.path.endswith("/game.jsp"):
        raise GameError(f"endereço de jogo inesperado: {parts.scheme}://{parts.netloc}{parts.path}")
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if not query.get("user") or not query.get("key"):
        raise GameError("o portal não entregou a sessão do jogo (user/key)")
    flashvars = [(name, query.get(name, "")) for name in GAME_FRAME_PARAMS]
    flashvars.append(("http", parts.scheme + ":"))
    return GameStart(
        movie=urljoin(url, f"Loading.swf?rnd={int(time.time() * 1000)}"),
        flashvars=urlencode(flashvars),
        params={"menu": "false", "quality": "high", "allowscriptaccess": "always",
                "allowfullscreeninteractive": "true", "wmode": "direct",
                "bgcolor": "#000000", "base": urljoin(url, ".")})


def windows_path(path):
    """Caminho Unix visto pelo Wine através do drive Z:."""
    return "Z:" + str(Path(path).resolve()).replace("/", "\\")


def find_prefix():
    prefix = Path(os.environ.get("WINEPREFIX") or DEFAULT_PREFIX)
    if not (prefix / "drive_c").is_dir():
        raise GameError(f"prefixo Wine não encontrado: {prefix}")
    return prefix


# O chat (e outras telas) usa o Flash Text Engine, que procura a fonte pelo nome
# gravado no arquivo e ignora as substituições do Wine (Arial → Liberation Sans).
# Sem esses arquivos no prefixo o texto vira quadrados; `winetricks corefonts`
# os instala.
REQUIRED_FONTS = ("arial.ttf", "verdana.ttf")


def missing_fonts(prefix):
    fonts = prefix / "drive_c/windows/Fonts"
    present = {path.name.lower() for path in fonts.iterdir()} if fonts.is_dir() else set()
    return [name for name in REQUIRED_FONTS if name not in present]


def find_ocx(prefix):
    configured = os.environ.get("LEGENDCLIENT_FLASH_OCX")
    if configured:
        if not Path(configured).is_file():
            raise GameError(f"LEGENDCLIENT_FLASH_OCX não existe: {configured}")
        return Path(configured)
    if flash.OCX_PATH.is_file():
        return flash.OCX_PATH
    drive = prefix / "drive_c"
    candidates = [
        *drive.glob("windows/system32/Macromed/Flash/Flash*.ocx"),
        *drive.glob("Program Files/*/flash/Flash64*.ocx"),
        *drive.glob("Program Files/*/Flash64*.ocx"),
    ]
    if not candidates:
        raise GameError("controle Flash (OCX 64 bits) não encontrado; instale-o com "
                        "`python -m legendclient.flash` ou defina LEGENDCLIENT_FLASH_OCX")
    return sorted(candidates)[-1]


def ensure_host():
    """Compila o host com winegcc se o binário faltar ou estiver desatualizado."""
    sources = [*HOST_DIR.glob("*.[ch]"), HOST_DIR / "Makefile"]
    newest = max(source.stat().st_mtime for source in sources)
    if HOST_EXE.exists() and HOST_EXE.stat().st_mtime >= newest:
        return HOST_EXE
    if not shutil.which("winegcc"):
        raise GameError("winegcc não encontrado; instale as ferramentas de desenvolvimento do Wine")
    LOG.info("compilando o host Flash")
    result = subprocess.run(["make", "-C", str(HOST_DIR), "-s"],
                            capture_output=True, text=True)
    if result.returncode:
        raise GameError(f"falha ao compilar o host Flash:\n{result.stderr.strip()}")
    return HOST_EXE


# Limite de armazenamento local (em KB) concedido ao servidor do jogo. O jogo
# pede 20 MB de cache; com o padrão do Flash (100 KB) ele abre o diálogo de
# permissão do Flash, que não recebe cliques dentro do Wine. 1048576 (1 GB) é
# o maior valor aceito: valores acima são descartados e o padrão volta a valer.
STORAGE_KLIMIT = 1048576


def _sol(name, entries):
    """Serializa um arquivo .sol (AMF0) com valores booleanos e numéricos."""
    encoded = name.encode()
    body = b"TCSO\x00\x04\x00\x00\x00\x00" + struct.pack(">H", len(encoded)) + encoded
    body += b"\x00\x00\x00\x00"
    for key, value in entries:
        raw = key.encode()
        body += struct.pack(">H", len(raw)) + raw
        if isinstance(value, bool):
            body += b"\x01" + bytes([value])
        else:
            body += b"\x00" + struct.pack(">d", value)
        body += b"\x00"
    return b"\x00\xbf" + struct.pack(">I", len(body)) + body


def _sol_bool(data, key, default=False):
    match = re.search(struct.pack(">H", len(key)) + key.encode() + b"\x01(.)", data, re.S)
    return bool(match.group(1)[0]) if match else default


def _sol_number(data, key):
    match = re.search(struct.pack(">H", len(key)) + key.encode() + b"\x00(.{8})", data, re.S)
    return struct.unpack(">d", match.group(1))[0] if match else None


def allow_local_storage(prefix, host):
    """Concede armazenamento local ao domínio do jogo, preservando as demais escolhas."""
    user = os.environ.get("USER") or Path.home().name
    path = (prefix / "drive_c/users" / user / "AppData/Roaming/Macromedia/Flash Player"
            / "macromedia.com/support/flashplayer/sys" / f"#{host}" / "settings.sol")
    data = path.read_bytes() if path.exists() else b""
    if _sol_number(data, "klimit") == STORAGE_KLIMIT:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_sol(f"{host}/settings", [
        ("allow", _sol_bool(data, "allow")),
        ("always", _sol_bool(data, "always")),
        ("klimit", float(STORAGE_KLIMIT)),
    ]))
    LOG.info("armazenamento local do Flash liberado para %s", host)


def trim_cache(directory=CACHE_DIR, max_bytes=CACHE_MAX_BYTES):
    """Mantém o cache dentro do limite, removendo primeiro os arquivos mais antigos."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = sorted((path.stat().st_mtime, path.stat().st_size, path)
                   for path in directory.iterdir() if path.is_file())
    total = sum(size for _, size, _ in files)
    for _, size, path in files:
        if total <= max_bytes:
            break
        path.unlink(missing_ok=True)
        total -= size


def _line(key, value):
    value = str(value)
    if "\n" in value or "\r" in value:
        raise GameError(f"valor inválido para {key}: contém quebra de linha")
    return f"{key}={value}\n"


def host_config(start, ocx, title, width=1280, height=760, cache_dir=None):
    lines = [_line("ocx", windows_path(ocx)), _line("title", title),
             _line("width", width), _line("height", height)]
    if cache_dir:
        # Caminho Unix: o host grava o cache com a libc do Linux.
        lines.append(_line("cache", Path(cache_dir).resolve()))
    props = {"AllowScriptAccess": "always", "Quality2": "high"}
    for name, value in start.params.items():
        prop = HTML_TO_ACTIVEX.get(name.lower())
        if prop:
            props[prop] = value
    lines += [_line(f"prop.{name}", value) for name, value in props.items()]
    if start.flashvars:
        lines.append(_line("flashvars", start.flashvars))
    lines.append(_line("movie", start.movie))
    return "".join(lines)


def launch(start, title, logfile):
    wine = shutil.which("wine")
    if not wine:
        raise GameError("wine não encontrado no PATH")
    prefix = find_prefix()
    ocx = find_ocx(prefix)
    missing = missing_fonts(prefix)
    if missing:
        LOG.warning("fontes ausentes no prefixo (%s): o chat aparecerá com quadrados; "
                    "instale com WINEPREFIX=%s winetricks corefonts", ", ".join(missing), prefix)
    host = ensure_host()
    allow_local_storage(prefix, urlsplit(start.movie).hostname)
    trim_cache()
    config = host_config(start, ocx, title, cache_dir=CACHE_DIR)
    env = dict(os.environ, WINEPREFIX=str(prefix))
    env.setdefault("WINEDEBUG", "-all,err+all")
    logfile.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # FlashVars contêm o token da sessão: registramos apenas os nomes das variáveis.
    names = [pair.split("=", 1)[0] for pair in start.flashvars.split("&") if pair]
    LOG.info("abrindo %s (flashvars: %s) com %s", start.movie, ", ".join(names) or "nenhuma", ocx)
    with open(logfile, "ab") as output:
        process = subprocess.Popen([wine, str(host)], env=env, stdin=subprocess.PIPE,
                                   stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    process.stdin.write(config.encode("utf-8"))
    process.stdin.close()
    LOG.info("host Flash iniciado pid=%s", process.pid)
    return process


def stop(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
