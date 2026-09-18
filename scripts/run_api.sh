#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec uvicorn ari_agent.main:app --app-dir src --host 0.0.0.0 --port 8000
