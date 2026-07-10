#!/usr/bin/env sh
# Full market refresh: scan+snapshot -> ratings -> docs -> analysis pipeline.
#
# Phase 1 (scan): scripts/fetch_ratings.py --snapshot launches headless Chromium
# via Playwright to scrape the CHECK24 result page and write a dated snapshot
# into data/snapshots/. Headless Chromium CANNOT run inside the Claude Code
# sandbox — run this script from a normal terminal, or pass --no-scan to skip
# phase 1 and go straight to docs + pipeline.
#
# Phase 2 (docs): scripts/fetch_docs.py --apply --into-raw downloads any
# manifest PDFs into data/raw/.
#
# Phase 3 (pipeline): ./pipeline.sh runs ingest -> extract -> render -> regression.
#
# Options (any order):
#   ./update-all.sh                                   # full refresh, defaults
#   ./update-all.sh --model haiku --filter --repeat 3 --jobs 3   # matches current record provenance
#   ./update-all.sh --no-scan                         # skip Playwright scan (sandbox-safe)
# NOTE: match --model/--filter/--repeat to the existing records' provenance
# (currently haiku --filter --repeat 3) — the extract cache signature includes
# them, so a mismatch silently re-extracts EVERY tariff at cost.
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [ -x "$ROOT/.venv/bin/python" ]; then
  PYRUN="$ROOT/.venv/bin/python"
else
  PYRUN="uv run"
fi

MODEL_ARGS=""
FILTER_ARGS=""
JOBS_ARGS=""
REPEAT_ARGS=""
NO_SCAN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --model) MODEL_ARGS="--model $2"; shift 2 ;;
    --filter) FILTER_ARGS="--filter"; shift ;;
    --jobs) JOBS_ARGS="--jobs $2"; shift 2 ;;
    --repeat) REPEAT_ARGS="--repeat $2"; shift 2 ;;
    --no-scan) NO_SCAN=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -n "$NO_SCAN" ]; then
  echo "==> scan (skipped: --no-scan)"
else
  echo "==> scan (headless Chromium -> dated snapshot in data/snapshots/)"
  # Non-fatal: this needs a real Chromium — fails loudly inside sandboxes/CI
  # without headed-browser support, but the rest of the refresh still runs.
  if $PYRUN scripts/fetch_ratings.py --snapshot; then :; else
    echo "WARNING: fetch_ratings.py --snapshot failed — likely Playwright/Chromium" >&2
    echo "  cannot launch here (e.g. the Claude Code sandbox). Run this script from" >&2
    echo "  a normal terminal, or pass --no-scan to skip this phase." >&2
  fi
fi

echo "==> docs (download manifest PDFs into data/raw/)"
# Non-fatal: a download hiccup (network, dead URL) is already logged per-tariff
# by fetch_docs.py — surface it loudly but let the pipeline run on what's local.
if $PYRUN scripts/fetch_docs.py --apply --into-raw; then :; else
  echo "WARNING: fetch_docs.py --apply --into-raw failed — see the log above." >&2
fi

echo "==> pipeline (ingest -> extract -> render -> regression)"
# shellcheck disable=SC2086
exec ./pipeline.sh $MODEL_ARGS $FILTER_ARGS $JOBS_ARGS $REPEAT_ARGS
