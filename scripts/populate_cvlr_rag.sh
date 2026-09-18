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

# Read CVLR_DEFAULT_CONNECTION so CERTORA_AI_COMPOSER_PGHOST/PGPORT apply. A copied
# DSN would send a container ingest to the wrong host.
conn="$(cd "$parent"; uv run --isolated --group ragbuild \
    python -c 'from composer.rag.db import CVLR_DEFAULT_CONNECTION as c; print(c)')"

(cd "$parent"; uv run --isolated --group ragbuild \
    python -m composer.scripts.ragbuild --output "$conn" "$docs_dir/solana.html")
