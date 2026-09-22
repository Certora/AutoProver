#!/bin/bash
# Ingest solana.html (from gen_docs.sh) into the cvlr_rag schema, not the default CVL database.
set -euo pipefail

script_dir="$(realpath "$(dirname "$0")")"
parent="$(realpath "$script_dir/..")"

docs_dir="$script_dir/prover-docs"
if [[ ! -f "$docs_dir/solana.html" ]]; then
    echo "Error: $docs_dir/solana.html not found. Run ./gen_docs.sh first." >&2
    exit 1
fi

(cd "$parent"; uv run --isolated --group ragbuild \
    python -m composer.scripts.ragbuild --corpus cvlr "$docs_dir/solana.html")
