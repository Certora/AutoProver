#!/bin/bash
# Ingest the `cvlr_kb` corpus: the Solana manual ./gen_docs.sh builds beside the CVL one, into the
# cvlr_rag schema rather than the default CVL database.
set -euo pipefail

script_dir="$(realpath "$(dirname "$0")")"
parent="$(realpath "$script_dir/..")"

docs_dir="$script_dir/prover-docs"
if [[ ! -f "$docs_dir/solana.html" ]]; then
    echo "Error: $docs_dir/solana.html not found. Run ./gen_docs.sh first." >&2
    exit 1
fi

# Asked for rather than spelled out here: the constant follows CERTORA_AI_COMPOSER_PGHOST/PGPORT,
# so a copy of the DSN in this script would send a container's ingest to the wrong host.
conn="$(cd "$parent"; uv run --isolated --group ragbuild \
    python -c 'from composer.rag.db import CVLR_DEFAULT_CONNECTION as c; print(c)')"

(cd "$parent"; uv run --isolated --group ragbuild \
    python -m composer.scripts.ragbuild --output "$conn" "$docs_dir/solana.html")
