#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python build_engine.py build_ext --inplace
python -c "import numpy as np; from app.engine import openmp_kernels as k; x=np.arange(1,100001,dtype=np.float64); assert k.numeric_stats_seq(x)[:5] == k.numeric_stats_parallel(x,4,'static')[:5]; print(k.engine_info())"
echo "Setup complete. Run ./run_linux.sh"
