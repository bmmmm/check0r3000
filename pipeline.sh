#!/usr/bin/env sh
# Run the full comparison pipeline: ingest -> extract -> render.
# Portable POSIX sh. Requires: uv (for ingest) and the `claude` CLI (extract/render).
#
# Optional model override forwarded to both LLM stages:
#   ./pipeline.sh                 # CLI default model
#   ./pipeline.sh --model opus    # override
# For stage-specific flags (--force, --no-llm) run the scripts directly.
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

MODEL_ARGS=""
if [ "${1:-}" = "--model" ] && [ -n "${2:-}" ]; then
  MODEL_ARGS="--model $2"
fi

echo "==> ingest (PDF -> text, dedup)"
uv run scripts/ingest.py

echo "==> extract (claude -p -> structured facts)"
# shellcheck disable=SC2086
uv run scripts/extract.py $MODEL_ARGS

echo "==> render (matrix + pros/cons -> out/)"
# shellcheck disable=SC2086
uv run scripts/render.py $MODEL_ARGS

echo "==> done. See out/vergleich.md and out/index.html"
