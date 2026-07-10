#!/bin/bash
# Build the `crucible_kb` RAG from the Crucible markdown docs (the §7.5 knowledge
# base — search tools over the harness guide / API reference / writing-tests / …).
#
# Unlike the foundry cheatcode build there's nothing to clone or convert: the
# Crucible docs are markdown in the local checkout, so we ingest them directly via
# composer.scripts.crucible_ragbuild (add_chunks_batch + add_manual_section).
#
#   CRUCIBLE_REPO=/path/to/crucible ./scripts/populate_crucible_rag.sh [--print] [--output <conn>]
#
# Any extra args are forwarded to crucible_ragbuild.
set -euo pipefail

script_dir="$(realpath "$(dirname "$0")")"
parent="$(realpath "$script_dir/..")"

if [[ -z "${CRUCIBLE_REPO:-}" ]]; then
    echo "Error: set CRUCIBLE_REPO to a local crucible clone." >&2
    exit 1
fi
docs_dir="$CRUCIBLE_REPO/docs"
if [[ ! -d "$docs_dir" ]]; then
    echo "Error: crucible docs not found at '$docs_dir'. Set CRUCIBLE_REPO to a local crucible clone." >&2
    exit 1
fi

shopt -s nullglob
md_files=("$docs_dir"/*.md "$CRUCIBLE_REPO"/README.md)
if [[ ${#md_files[@]} -eq 0 ]]; then
    echo "Error: no markdown docs found under $docs_dir" >&2
    exit 1
fi

echo "Ingesting ${#md_files[@]} crucible doc(s) into the crucible_kb RAG ..." >&2
(cd "$parent"; uv run --isolated --group ragbuild \
    python -m composer.scripts.crucible_ragbuild "${md_files[@]}" "$@")
