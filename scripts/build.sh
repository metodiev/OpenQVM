#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
if [[ "${1:-}" == "--clean" ]]; then
  rm -rf build dist
fi
python -m pip install --upgrade build
python -m build
echo "Artifacts written to $root/dist"
