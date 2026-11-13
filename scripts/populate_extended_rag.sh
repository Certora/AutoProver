#!/bin/bash
# Build extended.sqlite (CVL manual + prover docs + user guide; the sanity analyzer's corpus) from
# the HTML gen_docs.sh renders.
set -euo pipefail

script_dir="$(realpath "$(dirname "$0")")"
parent="$(realpath "$script_dir/..")"

docs_dir="$script_dir/prover-docs"
for f in cvl.html prover.html user-guide.html; do
    if [[ ! -f "$docs_dir/$f" ]]; then
        echo "Error: $docs_dir/$f not found. Run ./gen_docs.sh first." >&2
        exit 1
    fi
done

(cd "$parent"; uv run --isolated --group ragbuild python -m composer.scripts.ragbuild \
    --corpus extended \
    "$docs_dir/cvl.html" \
    "$docs_dir/prover.html" \
    "$docs_dir/user-guide.html")
