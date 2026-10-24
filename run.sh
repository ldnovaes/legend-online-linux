#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="$project_dir/.venv/bin/python"
[[ -x "$python" ]] || python=python3
cd "$project_dir"
exec "$python" -m legendclient "$@"
