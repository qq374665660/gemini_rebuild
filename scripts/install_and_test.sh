#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ "${SKIP_VENV:-0}" != "1" ]]; then
  if [[ ! -d ".venv" ]]; then
    python -m venv .venv
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

python -m pip install --upgrade pip

if ! python -m pip install -r requirements.txt; then
  if [[ "${ALLOW_PANDAS_PATCH:-0}" == "1" ]]; then
    python -m pip install "pandas>=2.2.2,<2.3"
    python -m pip install -r requirements.txt --no-deps
  else
    echo "Failed to install requirements. Set ALLOW_PANDAS_PATCH=1 to allow pandas patch version."
    exit 1
  fi
fi

python manage.py migrate
python manage.py test core
