#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Missing .venv. Run: python3 -m venv .venv && .venv/bin/pip install -e ."
  exit 1
fi

export APP_HOST="${APP_HOST:-0.0.0.0}"
export APP_PORT="${APP_PORT:-8080}"
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://grafinder:grafinder@localhost:5432/grafinder}"
export GRAFANA_API_URL="${GRAFANA_API_URL:-http://localhost:3001}"
export GRAFANA_PUBLIC_URL="${GRAFANA_PUBLIC_URL:-http://localhost:3001}"
export HTTP_PROXY="${HTTP_PROXY:-${http_proxy:-}}"
export HTTPS_PROXY="${HTTPS_PROXY:-${https_proxy:-}}"
export NO_PROXY="${NO_PROXY:-${no_proxy:-}}"

exec .venv/bin/python -m app
