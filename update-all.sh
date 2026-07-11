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
#   ./update-all.sh                       # full refresh; extract flags derived from records
#   ./update-all.sh --model haiku --filter --repeat 3 --jobs 3   # explicit override
#   ./update-all.sh --no-scan             # skip Playwright scan (sandbox-safe)
#
# Extract flags: when NONE of --model/--filter/--repeat are given, they are
# derived from the dominant record provenance (tui_data.py --provenance) so the
# extract cache signature matches the existing records — unchanged tariffs cost
# nothing. Passing ANY of the three switches to fully explicit mode: exactly the
# given flags are used (a mismatched spec re-extracts EVERY tariff at cost and
# replaces union-of-N records with single runs — know what you're doing).
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

if [ -z "$MODEL_ARGS" ] && [ -z "$FILTER_ARGS" ] && [ -z "$REPEAT_ARGS" ]; then
  PROV="$($PYRUN scripts/tui_data.py --provenance 2>/dev/null || true)"
  PROV_MODEL="${PROV%%|*}"
  rest="${PROV#*|}"
  PROV_FILTER="${rest%%|*}"
  PROV_REPEAT="${rest#*|}"
  if [ -n "$PROV_MODEL" ]; then
    MODEL_ARGS="--model $PROV_MODEL"
    [ "$PROV_FILTER" = "1" ] && FILTER_ARGS="--filter"
    [ "${PROV_REPEAT:-1}" -gt 1 ] 2>/dev/null && REPEAT_ARGS="--repeat $PROV_REPEAT"
    echo "==> extract flags from record provenance: $MODEL_ARGS ${FILTER_ARGS:-} ${REPEAT_ARGS:-}"
  else
    echo "==> no existing records — extract runs with its CLI defaults"
  fi
fi

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

if [ -z "$NO_SCAN" ]; then
  # Warn-only, and only in scan mode: the check needs outbound net to the
  # rating sites (finanztip.de), which sandboxed --no-scan runs don't have.
  echo "==> external ratings staleness (warn-only)"
  if $PYRUN scripts/check_external_ratings.py; then :; else
    echo "WARNING: external test verdicts may be stale — review" >&2
    echo "  data/sources/external-ratings.json (see the check output above)." >&2
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
