#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="$project_dir/.venv/bin/python"
[[ -x "$python" ]] || python=python3
cd "$project_dir"
# Baixa o Flash na primeira execução (ver legendclient/flash.py).
if [[ -z "${LEGENDCLIENT_FLASH_OCX:-}" && ! -f .local/flash/Flash64_15_0_0_167.ocx ]]; then
    "$python" -m legendclient.flash || echo "aviso: não foi possível obter o Flash" >&2
fi
exec "$python" -m legendclient "$@"
