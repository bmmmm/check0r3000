#!/usr/bin/env sh
# Run the full comparison pipeline: ingest -> extract -> render.
# Portable POSIX sh. Requires: uv (for ingest) and the `claude` CLI (extract/render).
#
# Options (any order):
#   ./pipeline.sh                          # CLI default model, no AVB filter
#   ./pipeline.sh --model haiku --filter   # cheap model + trimmed AVBs (recommended)
#   ./pipeline.sh --model ollama:llama3.1:8b   # local model via OpenAI-compatible API
# --model is forwarded to extract + render; --filter only to extract.
# For stage-specific flags (--force, --no-llm) run the scripts directly.
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

MODEL_ARGS=""
FILTER_ARGS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --model) MODEL_ARGS="--model $2"; shift 2 ;;
    --filter) FILTER_ARGS="--filter"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

echo "==> ingest (PDF -> text, dedup)"
uv run scripts/ingest.py

echo "==> extract (structured facts via model)"
# shellcheck disable=SC2086
uv run scripts/extract.py $MODEL_ARGS $FILTER_ARGS

echo "==> render (matrix + pros/cons -> out/)"
# shellcheck disable=SC2086
uv run scripts/render.py $MODEL_ARGS

echo "==> regression check (document-grounded golden invariants)"
# Non-fatal: the output is already written; surface drift loudly but don't abort.
# (Run scripts/regression.py standalone for a hard pass/fail gate, e.g. in CI.)
if uv run scripts/regression.py; then :; else
  echo "WARNING: extraction no longer matches benchmarks/golden.json — review out/tariffs." >&2
fi

echo "==> done. See out/vergleich.md and out/index.html"
