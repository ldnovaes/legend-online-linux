#!/usr/bin/env python3
"""Local baseline: run the original Flash/ActiveX client with its original proxy."""
import argparse
import fcntl
import hashlib
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_PREFIX = Path.home() / "Games/legend-online"
APP = "drive_c/Program Files/Legend Online Client by Brov (H2_x64)"
LOGDIR = PROJECT / ".local/logs"
LOG = logging.getLogger("legend-online")


def configure_logging(logdir=LOGDIR):
    logdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    for handler in LOG.handlers[:]:
        LOG.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [launcher=%(process)d] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z")
    file_handler = RotatingFileHandler(logdir / "legend-online.log",
                                      maxBytes=5 * 1024 * 1024, backupCount=3,
                                      encoding="utf-8")
    console = logging.StreamHandler()
    console.addFilter(lambda record: not getattr(record, "child_output", False))
    for handler in (file_handler, console):
        handler.setFormatter(formatter)
        LOG.addHandler(handler)


def launch(name, command, root, env):
    process = subprocess.Popen(command, cwd=root, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True)
    LOG.info("[%s] iniciado pid=%s", name, process.pid)

    def capture():
        try:
            with process.stdout:
                for line in iter(process.stdout.readline, b""):
                    message = line.decode("utf-8", errors="replace").rstrip()
                    level = logging.ERROR if (":err:" in message or "Unhandled page fault" in message) else (
                        logging.WARNING if ":warn:" in message else logging.INFO)
                    LOG.log(level, "[%s pid=%s] %s", name, process.pid, message,
                            extra={"child_output": True})
        except OSError:
            LOG.exception("[%s] falha ao ler saída", name)

    reader = threading.Thread(target=capture, daemon=True, name=f"log-{name}")
    reader.start()
    return process, reader


def wait_client(client, proxy, smoke_seconds):
    deadline = time.monotonic() + smoke_seconds if smoke_seconds else None
    while True:
        result = client.poll()
        if result is not None:
            LOG.info("[client] encerrado código=%s", result)
            if smoke_seconds:
                raise RuntimeError(f"Cliente encerrou antes do fim do teste (código {result})")
            if result:
                raise RuntimeError(f"Cliente encerrou com erro (código {result})")
            return 0
        if proxy is not None and proxy.poll() is not None:
            raise RuntimeError(f"H2Proxy encerrou durante a sessão (código {proxy.returncode})")
        if deadline is not None and time.monotonic() >= deadline:
            LOG.info("Cliente ativo por %.1fs; login e jogo não validados.", smoke_seconds)
            return 0
        time.sleep(0.25)


def probe(path, port=8125):
    # Direct loopback connection: ignore HTTP_PROXY and never follow redirects.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        body = response.read(65537)
        if len(body) > 65536:
            raise RuntimeError("Resposta local maior que o limite de 64 KiB")
        return {"path": path, "status": response.status,
                "content_type": response.getheader("Content-Type"),
                "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}, body
    finally:
        connection.close()


def check_proxy(port=8125):
    health, _ = probe("/polipo/", port)
    policy, body = probe("/crossdomain.xml", port)
    if health["status"] != 200 or policy["status"] != 200 or b"<cross-domain-policy" not in body:
        raise RuntimeError("Porta ocupada por serviço incompatível com o H2Proxy")
    return [health, policy]


def listening():
    try:
        with socket.create_connection(("127.0.0.1", 8125), timeout=1):
            return True
    except ConnectionRefusedError:
        return False


def stop_owned(process):
    if process is None or process.poll() is not None:
        return
    # A dedicated process group; never taskkill/wineserver -k across user sessions.
    try:
        LOG.info("Encerrando processo iniciado pelo launcher: pid=%s", process.pid)
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        LOG.warning("Processo pid=%s não respondeu ao SIGTERM; enviando SIGKILL", process.pid)
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    except ProcessLookupError:
        pass


def run(args):
    root = args.prefix / APP
    required = ["LegendOnline.exe", "H2Proxy.exe", "Adobe.Flash.Control.manifest",
                "flash/Flash64_15_0_0_167.ocx"]
    missing = [name for name in required if not (root / name).is_file()]
    wine = shutil.which("wine")
    if missing or not wine or not (args.prefix / "system.reg").is_file():
        raise RuntimeError(f"Instalação incompleta: wine={wine}, ausentes={missing}, prefixo={args.prefix}")
    if args.command == "doctor":
        print(json.dumps({"wine": wine, "prefix": str(args.prefix),
                          "application_dir": str(root), "files_present": required,
                          "display": os.environ.get("DISPLAY"),
                          "runtime": "Windows ActiveX via Wine (não nativo)"}, indent=2))
        return 0
    if args.command == "probe":
        print(json.dumps(check_proxy(), indent=2))
        return 0
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        raise RuntimeError("Sessão gráfica ausente; defina DISPLAY ou WAYLAND_DISPLAY")
    logdir = LOGDIR
    env = dict(os.environ, WINEPREFIX=str(args.prefix.resolve()), WINEDEBUG=args.wine_debug)
    LOG.info("Wine=%s; prefixo=%s; aplicativo=%s; WINEDEBUG=%s",
             wine, args.prefix.resolve(), root, args.wine_debug)
    LOG.info("Log interno do H2Proxy: %s", args.prefix / "drive_c/cache/logfile.txt")
    proxy = client = None
    readers = []
    lock = (logdir / "run.lock").open("a")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Já existe uma sessão aberta por este launcher") from None
        if not listening():
            LOG.info("Iniciando H2Proxy original…")
            proxy, reader = launch("proxy", [wine, "H2Proxy.exe", "--tray"], root, env)
            readers.append(reader)
            deadline = time.monotonic() + 30
            while True:
                if proxy.poll() is not None:
                    raise RuntimeError(f"H2Proxy encerrou com código {proxy.returncode}; consulte {logdir}")
                try:
                    check_proxy()
                    break
                except (ConnectionError, TimeoutError, http.client.HTTPException):
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"H2Proxy não respondeu em 30s; consulte {logdir}")
                    time.sleep(0.25)
        else:
            LOG.info("Porta 8125 ocupada; verificando proxy existente antes de reutilizar")
        results = check_proxy()
        (logdir / "proxy-probe.json").write_text(json.dumps(results, indent=2) + "\n")
        for result in results:
            LOG.info("[proxy] GET %s status=%s bytes=%s", result["path"], result["status"], result["bytes"])
        LOG.info("Proxy pronto. Abrindo cliente…")
        client, reader = launch("client", [wine, "LegendOnline.exe"], root, env)
        readers.append(reader)
        return wait_client(client, proxy, args.smoke_seconds)
    finally:
        stop_owned(client)
        stop_owned(proxy)
        for reader in readers:
            reader.join(timeout=2)
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["doctor", "probe", "run"])
    parser.add_argument("--prefix", type=Path, default=DEFAULT_PREFIX)
    parser.add_argument("--smoke-seconds", type=float, default=0,
                        help="Encerra somente os processos iniciados aqui após N segundos")
    parser.add_argument("--wine-debug", default="-all,err+all,warn+wininet,warn+winhttp,warn+ole,warn+seh",
                        help="Canais do Wine capturados no log (use --wine-debug=VALOR)")
    args = parser.parse_args()
    if not math.isfinite(args.smoke_seconds) or args.smoke_seconds < 0:
        parser.error("--smoke-seconds deve ser finito e >= 0")
    configure_logging()
    LOG.info("=== Início comando=%s; log=%s ===", args.command, LOGDIR / "legend-online.log")
    def terminate(signum, frame):
        raise KeyboardInterrupt
    old_handler = signal.signal(signal.SIGTERM, terminate)
    result = 1
    try:
        result = run(args)
    except KeyboardInterrupt:
        LOG.info("Encerramento solicitado pelo usuário/sinal")
        result = 130
    except Exception:
        LOG.exception("Falha no launcher")
    finally:
        signal.signal(signal.SIGTERM, old_handler)
        LOG.info("=== Fim comando=%s código=%s ===", args.command, result)
    return result


if __name__ == "__main__":
    sys.exit(main())
