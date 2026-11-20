#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then echo "Run ./setup_linux.sh first"; exit 1; fi
if ! .venv/bin/python -c "from app.engine import openmp_kernels" 2>/dev/null; then echo "Native OpenMP engine missing; run ./setup_linux.sh"; exit 1; fi
exec .venv/bin/python scripts/launcher.py
