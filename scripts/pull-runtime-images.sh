#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "scripts/pull-runtime-images.sh is kept for compatibility."
echo "Use scripts/retag-runtime-images.sh after docker pull or docker load."
exec bash scripts/retag-runtime-images.sh "$@"
