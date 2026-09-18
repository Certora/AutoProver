#!/bin/bash
# Ingest the cvlr_kb corpus: solana.html (from gen_docs.sh), plus any manifests found.
#
# The manifests -- the CVLR crate reference and the project-derived practice entries -- are built
# in the private certora-cvlr-kb repo, because that is where the API key, cargo and model are.
# This script finds and ingests them; it does not build them. Either source can be missing and the
# other still lands; both missing is an error.
#
# Manifest resolution, in order:
#   1. paths given before `--`, which skip discovery
#   2. $CVLR_KB_REPO/src/certora_cvlr_kb/data/*.rag.json
#   3. the installed certora_cvlr_kb package
#
# Args after `--` go to rag_import:
#   ./populate_cvlr_rag.sh -- --print          # dry run
set -euo pipefail

script_dir="$(realpath "$(dirname "$0")")"
parent="$(realpath "$script_dir/..")"
docs_dir="$script_dir/prover-docs"

# Read CVLR_DEFAULT_CONNECTION so CERTORA_AI_COMPOSER_PGHOST/PGPORT apply. A copied
# DSN would send a container ingest to the wrong host.
conn="$(cd "$parent"; uv run --isolated --group ragbuild \
    python -c 'from composer.rag.db import CVLR_DEFAULT_CONNECTION as c; print(c)')"

# A missing manual is not fatal: the manifests can still land without it.
ingest_manual() {
    if [[ ! -f "$docs_dir/solana.html" ]]; then
        echo "No $docs_dir/solana.html -- run ./gen_docs.sh. Skipping the manual." >&2
        return 1
    fi
    # PROVENANCE names the docs revision, which is the only record of what this HTML is.
    [[ -f "$docs_dir/PROVENANCE" ]] && cat "$docs_dir/PROVENANCE" >&2
    (cd "$parent"; uv run --isolated --group ragbuild \
        python -m composer.scripts.ragbuild --output "$conn" "$docs_dir/solana.html")
}

manifests=()
forward=()
seen_ddash=0
for arg in "$@"; do
    if [[ $seen_ddash -eq 1 ]]; then
        forward+=("$arg")
    elif [[ "$arg" == "--" ]]; then
        seen_ddash=1
    else
        manifests+=("$arg")
    fi
done

# Unmatched globs must vanish rather than being passed through as literal patterns.
shopt -s nullglob

if [[ ${#manifests[@]} -eq 0 ]]; then
    if [[ -n "${CVLR_KB_REPO:-}" ]]; then
        kb_data="${CVLR_KB_REPO%/}/src/certora_cvlr_kb/data"
        for f in "$kb_data"/*.rag.json; do
            manifests+=("$f")
        done
        [[ -d "$kb_data" ]] || echo "  CVLR_KB_REPO is set but $kb_data does not exist" >&2
    fi

    # The installed package, only if a checkout did not supply them, so a checkout you are
    # editing wins over an older installed copy.
    if [[ ${#manifests[@]} -eq 0 ]]; then
        probe='
try:
    from certora_cvlr_kb import manifests
    print("\n".join(str(p) for p in manifests()))
except Exception:
    pass
'
        while IFS= read -r line; do
            [[ -n "$line" ]] && manifests+=("$line")
        done < <(cd "$parent" && uv run --no-sync python -c "$probe" 2>/dev/null || true)
    fi
fi

if [[ ${#manifests[@]} -eq 0 ]]; then
    if ingest_manual; then
        echo "No manifest found; ingested the manual alone." >&2
        exit 0
    fi
    cat >&2 <<'MSG'
Error: no cvlr_kb manifest found and no local manual, so there is nothing to ingest.

The manifests ship in the certora-cvlr-kb package: clone that repo and point CVLR_KB_REPO at it,
or `pip install certora-cvlr-kb`. Its tools/README.md maps each manifest to its producer. For the
manual, run ./gen_docs.sh.

Pass manifest paths explicitly to skip discovery.
MSG
    exit 1
fi

ingest_manual || true

echo "Ingesting ${#manifests[@]} manifest(s) into cvlr_kb ..." >&2
(cd "$parent"; uv run --isolated --group ragbuild \
    python -m composer.scripts.rag_import "${manifests[@]}" "${forward[@]+"${forward[@]}"}")
