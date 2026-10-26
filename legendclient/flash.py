"""Obtém o controle Flash ActiveX 64 bits do instalador oficial da Adobe.

O runtime não é redistribuído pelo projeto. Este módulo baixa o arquivo de
versões antigas publicado pela Adobe (hoje só disponível pela Wayback Machine),
confere o SHA-256 e extrai o OCX sem executar nada:

    fp_15.0.0.167_archive.zip
      └ flashplayer15_0r0_167_winax.exe   instalador 32 bits
          └ RCDATA 206 (LZMA)             instalador 64 bits
              └ RCDATA 203 (LZMA)         Flash64_15_0_0_167.ocx

É a mesma versão, byte a byte, que o cliente do Brov distribuía.

    python -m legendclient.flash
"""
import hashlib
import io
import logging
import lzma
from pathlib import Path
import struct
import sys
import tempfile
import urllib.request
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
OCX_PATH = PROJECT / ".local/flash/Flash64_15_0_0_167.ocx"
OCX_SHA256 = "7ac444d19ad9d7c8a26a1fe09a7052fb7c6c922ce6c4ee38798a963df42e38ec"
ARCHIVE_URL = ("https://web.archive.org/web/20200813041620id_/http://fpdownload.macromedia.com"
               "/pub/flashplayer/installers/archive/fp_15.0.0.167_archive.zip")
ARCHIVE_SHA256 = "5b1cf02d31d41a1780ec0fc1e3698c8246ac20218fe61d219d5b474761bc18f5"
ARCHIVE_MEMBER = "fp_15.0.0.167_archive/15_0_r0_167/flashplayer15_0r0_167_winax.exe"
# IDs de RCDATA dentro dos instaladores (ver docstring).
X64_INSTALLER_ID = 206
X64_OCX_ID = 203
RT_RCDATA = 10
LOG = logging.getLogger("legendclient.flash")


class FlashError(RuntimeError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def rcdata(pe, wanted):
    """Primeiro idioma do recurso RCDATA `wanted` de um PE (32 ou 64 bits)."""
    pe_offset = struct.unpack_from("<I", pe, 0x3C)[0]
    if pe[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise FlashError("instalador não é um executável PE")
    sections, optional_size = struct.unpack_from("<H12xH", pe, pe_offset + 6)
    optional = pe_offset + 24
    magic = struct.unpack_from("<H", pe, optional)[0]
    # Diretório de recursos: entrada 2 da tabela de diretórios de dados.
    directories = optional + (112 if magic == 0x20B else 96)
    rsrc_rva = struct.unpack_from("<I", pe, directories + 2 * 8)[0]
    for index in range(sections):
        header = optional + optional_size + index * 40
        size, rva, _, raw = struct.unpack_from("<IIII", pe, header + 8)
        if rva <= rsrc_rva < rva + size:
            base = raw + rsrc_rva - rva
            delta = rva - raw
            break
    else:
        raise FlashError("instalador sem seção de recursos")

    def entries(offset):
        named, numbered = struct.unpack_from("<HH", pe, base + offset + 12)
        for item in range(named + numbered):
            name, target = struct.unpack_from("<II", pe, base + offset + 16 + item * 8)
            yield name, target

    def child(offset, key):
        for name, target in entries(offset):
            if key is None or name == key:
                return target & 0x7FFFFFFF
        raise FlashError(f"recurso RCDATA {wanted} ausente do instalador")

    leaf = child(child(child(0, RT_RCDATA), wanted), None)
    data_rva, size = struct.unpack_from("<II", pe, base + leaf)
    return pe[data_rva - delta:data_rva - delta + size]


def unpack(blob):
    """Recursos do instalador: cabeçalho de 11 bytes e um fluxo LZMA cru."""
    props, dict_size = struct.unpack_from("<BI", blob, 11)
    lc, rest = props % 9, props // 9
    filters = [{"id": lzma.FILTER_LZMA1, "dict_size": dict_size,
                "lc": lc, "lp": rest % 5, "pb": rest // 5}]
    try:
        return lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=filters).decompress(blob[16:])
    except lzma.LZMAError as error:
        raise FlashError(f"recurso do instalador corrompido: {error}") from None


def extract_ocx(archive):
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        installer = bundle.read(ARCHIVE_MEMBER)
    ocx = unpack(rcdata(unpack(rcdata(installer, X64_INSTALLER_ID)), X64_OCX_ID))
    if sha256(ocx) != OCX_SHA256:
        raise FlashError("o OCX extraído não confere com o SHA-256 esperado")
    return ocx


def download(url=ARCHIVE_URL):
    LOG.info("baixando %s", url)
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = response.read()
    if sha256(archive) != ARCHIVE_SHA256:
        raise FlashError("o arquivo baixado não confere com o SHA-256 esperado")
    return archive


def install(target=OCX_PATH):
    """Garante o OCX em `target`; baixa e extrai apenas se faltar."""
    if target.is_file() and sha256(target.read_bytes()) == OCX_SHA256:
        return target
    ocx = extract_ocx(download())
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        stream.write(ocx)
    Path(stream.name).replace(target)
    return target


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        print(install())
    except (FlashError, OSError) as error:
        sys.exit(f"erro: {error}")


if __name__ == "__main__":
    main()
