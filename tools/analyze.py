#!/usr/bin/env python3
"""Read-only inventory of the installed Brov binaries; never executes a PE."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import pefile

DEFAULT_PREFIX = Path.home() / "Games/legend-online"
APP = "drive_c/Program Files/Legend Online Client by Brov (H2_x64)"
TARGETS = ["LegendOnline.exe", "H2Proxy.exe", "flash/Flash64_15_0_0_167.ocx",
           "Qt5Core.dll", "WebView2Loader.dll"]
INTERESTING = re.compile(
    r"Flash|flash|LoadMovie|CallFunction|FSCommand|AllowScript|AllowFullScreen|"
    r"QAx|WebView2|Loading\.swf|/revproxy/|/addRevProxyMapping/|/getResServer/|"
    r"/known-hosts/|/polipo/|/crossdomain\.xml|812[0-9]|H2Proxy|"
    r"QNetworkDiskCache|WebSocketProxy|QWebSocket|RedirectCache|C:/cache|"
    r"^https?://(?:brov|cdn\.brov)|^WMode$|^Quality$|^Scale$|^ShowAll$")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect(path):
    data = path.read_bytes()
    pe = pefile.PE(data=data)
    try:
        imports = {entry.dll.decode(): [
            item.name.decode(errors="replace") if item.name else f"ordinal:{item.ordinal}"
            for item in entry.imports
        ] for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])}
        versions = {}
        for group in getattr(pe, "FileInfo", []):
            for info in group:
                for table in getattr(info, "StringTable", []):
                    versions.update({k.decode(): v.decode(errors="replace")
                                     for k, v in table.entries.items()})
        evidence = []
        if path.suffix.lower() == ".exe":
            for pattern, encoding in [(rb"[\x20-\x7e]{4,}", "ascii"),
                                      (rb"(?:[\x20-\x7e]\x00){4,}", "utf-16le")]:
                for match in re.finditer(pattern, data):
                    value = match[0].decode(encoding)
                    if len(value) > 250 or not INTERESTING.search(value):
                        continue
                    offset = match.start()
                    try:
                        va = hex(pe.OPTIONAL_HEADER.ImageBase + pe.get_rva_from_offset(offset))
                    except pefile.PEFormatError:
                        va = None
                    evidence.append({"file_offset": hex(offset), "va": va,
                                     "encoding": encoding, "text": value})
        return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "machine": hex(pe.FILE_HEADER.Machine), "version": versions,
                "image_base": hex(pe.OPTIONAL_HEADER.ImageBase),
                "sections": [{"name": s.Name.rstrip(b"\0").decode(),
                              "rva": hex(s.VirtualAddress),
                              "file_offset": hex(s.PointerToRawData),
                              "raw_bytes": s.SizeOfRawData} for s in pe.sections],
                "imports": imports, "evidence": evidence}
    finally:
        pe.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=DEFAULT_PREFIX)
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--output", type=Path, default=Path("artifacts/static-analysis.json"))
    args = parser.parse_args()
    root = args.prefix / APP
    missing = [name for name in TARGETS if not (root / name).is_file()]
    if missing:
        parser.error(f"Arquivos ausentes em {root}: {', '.join(missing)}")
    report = {"schema": 1, "method": "static PE metadata and strings; not decompilation",
              "application_dir": str(root),
              "binaries": {name: inspect(root / name) for name in TARGETS},
              "flash_manifest": (root / "Adobe.Flash.Control.manifest").read_text(),
              "installed_swf_files": [str(p.relative_to(root)) for p in root.rglob("*.swf")]}
    if args.installer:
        report["installer"] = {"name": args.installer.name,
                               "bytes": args.installer.stat().st_size,
                               "sha256": sha256(args.installer)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Análise de {len(TARGETS)} binários: {args.output}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, pefile.PEFormatError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        sys.exit(1)
