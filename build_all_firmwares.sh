#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

command -v pio >/dev/null 2>&1 || { echo "ERROR: 'pio' is not in PATH"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: 'python3' is not in PATH"; exit 1; }

OUT_DIR="${1:-firmware-build}"
python3 ci/firmware_matrix.py build-all --output "${OUT_DIR}"

echo
echo "DONE. Firmware and manifests: ${OUT_DIR}/"
