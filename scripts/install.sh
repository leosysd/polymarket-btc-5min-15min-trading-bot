#!/usr/bin/env bash
# ==========================================================================
#  scripts/install.sh -- one-shot installer for the jy-bot trading bot.
#
#  - creates a Python virtualenv (venv/)
#  - installs dependencies from requirements.txt
#  - creates .env from .env.example if missing
#  - checks for Redis (optional)
#  - validates the configuration
#
#  Usage:   bash scripts/install.sh
# ==========================================================================
set -euo pipefail

# Resolve project root (this script lives in scripts/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT}"

echo "=========================================================================="
echo "  jy-bot installer"
echo "  project: ${ROOT}"
echo "=========================================================================="

# ── 1. Python ─────────────────────────────────────────────────────────────
PY="${PYTHON:-python3}"
if ! command -v "${PY}" >/dev/null 2>&1; then
  PY="python"
fi
if ! command -v "${PY}" >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3.9+ first." >&2
  exit 1
fi
echo "[1/5] Python: $(${PY} --version 2>&1)"

# ── 2. venv ───────────────────────────────────────────────────────────────
if [ ! -d "venv" ]; then
  echo "[2/5] Creating virtualenv: venv/"
  "${PY}" -m venv venv
else
  echo "[2/5] virtualenv already exists: venv/"
fi
# shellcheck disable=SC1091
source venv/bin/activate
python -m pip install --upgrade pip >/dev/null

# ── 3. dependencies ───────────────────────────────────────────────────────
echo "[3/5] Installing dependencies from requirements.txt ..."
if ! pip install -r requirements.txt; then
  echo "WARNING: some dependencies failed to install." >&2
  echo "         The dry-run engine still works on the standard library;" >&2
  echo "         live trading needs py-clob-client." >&2
fi

# ── 4. .env ───────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
  echo "[4/5] Creating .env from .env.example -- EDIT IT with your values."
  cp .env.example .env
else
  echo "[4/5] .env already exists (left untouched)."
fi

# ── Redis (optional) ──────────────────────────────────────────────────────
if command -v redis-cli >/dev/null 2>&1; then
  if redis-cli ping >/dev/null 2>&1; then
    echo "      Redis: running (PONG)"
  else
    echo "      Redis: installed but not responding (optional -- live mode switch)"
  fi
else
  echo "      Redis: not installed (optional). Install with your package manager"
  echo "             e.g. 'sudo apt install redis-server' if you want live switching."
fi

# ── 5. validate config ────────────────────────────────────────────────────
echo "[5/5] Validating configuration ..."
set +e
python scripts/check_config.py
CFG_RC=$?
set -e

echo "=========================================================================="
echo "  Done."
echo "  Next steps:"
echo "    1) edit  .env       (fill credentials only if you intend to go live)"
echo "    2) test  python main.py --test-mode"
echo "    3) run   python main.py --simulation"
echo "    4) live  set DRY_RUN=false & LIVE_TRADING=true, then: python main.py --live"
echo "=========================================================================="
exit ${CFG_RC}
