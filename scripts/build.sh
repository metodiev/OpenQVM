#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
args=("$@")
for arg in "${args[@]:-}"; do
  if [[ "$arg" == "--clean" ]]; then
    rm -rf build dist
  fi
done
python -m pip install --upgrade build
python -m build
echo "Wheels written to $root/dist"

for arg in "${args[@]:-}"; do
  if [[ "$arg" == "--bundle" ]]; then
    python -m pip install --upgrade pyinstaller
    python -m PyInstaller --noconfirm packaging/openqvm.spec
    bundle="$root/dist/OpenQVM"
    "$bundle/openqvm-cli" --version
    "$bundle/OpenQVM" --self-test
    echo "Portable bundle: $bundle"
  fi
done
