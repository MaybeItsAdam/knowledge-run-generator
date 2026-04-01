#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

LIMIT="${LIMIT:-}"
OUTPUT="${OUTPUT:-constants/runPoints.json}"
FORMAT="${FORMAT:-json}"
EXTRA_ARGS=("$@")

CMD=(python -m knowledge_run_generator.blue_book_demo.run_pipeline --output "$OUTPUT" --format "$FORMAT")

if [[ -n "$LIMIT" ]]; then
  CMD+=(--limit "$LIMIT")
fi

if [[ "$FORMAT" == "geojson" ]]; then
  CMD+=(--geojson)
fi

if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
  CMD+=("${EXTRA_ARGS[@]}")
fi

printf 'Running: %q ' "${CMD[@]}"
printf '\n'

"${CMD[@]}"
