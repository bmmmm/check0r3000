#!/usr/bin/env sh
# Run the full comparison pipeline: ingest -> extract -> golden pins -> render.
# Portable POSIX sh. Requires: uv (for ingest) and the `claude` CLI (extract/render).
#
# Options (any order):
#   ./pipeline.sh                          # CLI default model, no AVB filter
#   ./pipeline.sh --model haiku --filter   # cheap model + trimmed AVBs (recommended)
#   ./pipeline.sh --model ollama:llama3.1:8b   # local model via OpenAI-compatible API
# --model is forwarded to extract + render; --filter/--jobs/--repeat only to extract.
# NOTE: match --repeat to the existing records' provenance (currently repeat=3) —
# the extract cache signature includes it, so a mismatch silently re-extracts
# EVERY tariff at cost and replaces union-of-3 records with single-run ones.
# For stage-specific flags (--force, --no-llm) run the scripts directly.
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# Same env selection as update-all.sh: prefer the checked-out venv (works in
# sandboxes where uv's cache dir is blocked), fall back to uv run.
if [ -x "$ROOT/.venv/bin/python" ]; then
  PYRUN="$ROOT/.venv/bin/python"
else
  PYRUN="uv run"
fi

MODEL_ARGS=""
FILTER_ARGS=""
JOBS_ARGS=""
REPEAT_ARGS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --model) MODEL_ARGS="--model $2"; shift 2 ;;
    --filter) FILTER_ARGS="--filter"; shift ;;
    --jobs) JOBS_ARGS="--jobs $2"; shift 2 ;;
    --repeat) REPEAT_ARGS="--repeat $2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Same provenance derivation as update-all.sh: a BARE ./pipeline.sh used to run
# extract with its CLI default (model "claude", no filter) — on a vertical whose
# records carry haiku+filter that is a cache-signature mismatch, i.e. a silent
# paid re-extract of every tariff (this fired for real on 2026-08-28). When none
# of --model/--filter/--repeat are given, match the existing records instead;
# passing ANY of the three keeps fully explicit mode.
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

echo "==> ingest (PDF -> text, dedup)"
$PYRUN scripts/ingest.py

echo "==> extract (structured facts via model)"
# Non-fatal: extract.py returns rc=1 when any single tariff failed all its runs
# (per-tariff resilience) — the records that did succeed are already written, so
# surface the failure loudly but don't abort the rest of the pipeline.
# shellcheck disable=SC2086
if $PYRUN scripts/extract.py $MODEL_ARGS $FILTER_ARGS $JOBS_ARGS $REPEAT_ARGS; then :; else
  echo "WARNING: extract.py reported failed tariff(s) — see out/<vertical>/tariffs/ and the log above." >&2
fi

echo "==> golden pins (auto-repair hallucinated isnull fields on golden stems)"
# Non-fatal by design and exits 0 anyway: re-nulls fields golden.json pins as
# structurally unsupported (module levels, beitrag) that a cheap-model re-extract
# hallucinated back. Repairs are logged + recorded in tmp/golden-pin-repairs.json.
if $PYRUN scripts/golden_pins.py; then :; else
  echo "WARNING: golden_pins.py failed — golden isnull pins were not enforced." >&2
fi

echo "==> overlay (structured price/Stufe from data/offers/, no model)"
# Non-fatal: a self-check mismatch must be loud, but the common case (no offer
# files yet) is normal and exits 0. Run scripts/overlay.py --check standalone
# (e.g. in CI) for a hard re-validation gate over existing enriched records.
if $PYRUN scripts/overlay.py; then :; else
  echo "WARNING: overlay self-check failed — see out/<vertical>/enriched/ and data/<vertical>/offers/." >&2
fi

echo "==> render (matrix + pros/cons -> out/)"
# Non-fatal: a render failure (e.g. an LLM error in the pros/cons synthesis) must
# not stop the regression gate below — the extracted records are already on disk.
# shellcheck disable=SC2086
if $PYRUN scripts/render.py $MODEL_ARGS; then :; else
  echo "WARNING: render.py failed — out/<vertical>/vergleich.md / index.html may be stale." >&2
fi

echo "==> regression check (document-grounded golden invariants)"
# Non-fatal: the output is already written; surface drift loudly but don't abort.
# (Run scripts/regression.py standalone for a hard pass/fail gate, e.g. in CI.)
if $PYRUN scripts/regression.py; then :; else
  echo "WARNING: extraction no longer matches the golden invariants — review out/<vertical>/tariffs." >&2
fi

echo "==> done. See out/<vertical>/vergleich.md and out/<vertical>/index.html"
