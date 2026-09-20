#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${QUANT_WSL_VENV:-$HOME/.venvs/quant-engine}"
export PATH="/usr/local/cuda/bin:$VENV/bin:$PATH"
export LD_LIBRARY_PATH="/usr/local/cuda/lib64:/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
export PYTHONPATH="$PROJECT_ROOT/services/quant-engine/src"

cd "$PROJECT_ROOT"
exec "$VENV/bin/python" -c '
from pathlib import Path
import yaml
from quant_engine.stock_ml import run_walkforward

root = Path.cwd()
cfg = yaml.safe_load((root / "config/stock-ml.yaml").read_text(encoding="utf-8"))
result = run_walkforward(
    root / "data/factors.db",
    root / "data/market.db",
    root / "artifacts/stock-ml",
    cfg,
    progress=lambda p: print(f"PROGRESS {p:.1f}", flush=True),
)
print(result)
'
