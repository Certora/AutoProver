#!/bin/bash
# Ingest the two CVLR corpora.
#
#   cvlr_kb      the Solana manual (solana.html, from gen_docs.sh) plus any practice manifests
#                found. Prose, hand-written, allowed to lag the crates.
#   cvlr_api_kb  the CVLR API, generated here by composer.scripts.cvlr_api_docs from rustdoc over
#                the releases composer/spec/cvlr_reference.py pins.
#
# Each manifest declares its own knowledge_base, and rag_import routes by that tag, so both land
# from one call. See docs/cvlr-api-docs-plan.md. Any source can be missing and the others still
# land; all missing is an error.
#
# Manifest resolution, in order:
#   1. paths given before `--`, which skip both generation and discovery
#   2. the generated cvlr_api_kb manifest
#   3. $CVLR_KB_REPO/src/certora_cvlr_kb/data/*.rag.json
#   4. the installed certora_cvlr_kb package
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

# The API corpus is built here rather than found: it needs cargo and a nightly toolchain and
# nothing else, which is the whole reason it moved into this repo.
build_api_docs() {
    local out="$1"
    if ! (cd "$parent"; uv run --no-sync python -m composer.scripts.cvlr_api_docs --output "$out"); then
        echo "Could not generate the CVLR API corpus; continuing with whatever else is available." >&2
        echo "  It needs a nightly toolchain (rustup toolchain install nightly) and a warm cargo registry." >&2
        return 1
    fi
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

# Generated first so it joins the same rag_import call as everything else; an explicit manifest
# list on the command line means the caller is driving, so nothing is generated.
generated=""
if [[ ${#manifests[@]} -eq 0 ]]; then
    generated="$(mktemp -t cvlr_api_kb.XXXXXX.rag.json)"
    trap 'rm -f "$generated"' EXIT
    if build_api_docs "$generated"; then
        manifests+=("$generated")
    else
        generated=""
    fi
fi

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
Error: nothing to ingest -- the API corpus could not be generated, no practice manifest was found,
and there is no local manual.

  cvlr_api_kb  needs a nightly toolchain and a warm cargo registry; see the error above.
  cvlr_kb      needs ./gen_docs.sh for the manual. Practice manifests, where an install still has
               them, ship in the certora-cvlr-kb package (set CVLR_KB_REPO, or pip install it) --
               that repo is being wound down as a corpus source, so an install without them is
               expected rather than broken.

Pass manifest paths explicitly to skip generation and discovery.
MSG
    exit 1
fi

ingest_manual || true

# No --output: each manifest carries its own knowledge_base and rag_import groups by the
# connection that resolves to, which is what lets one call fill two corpora.
echo "Ingesting ${#manifests[@]} manifest(s) ..." >&2
(cd "$parent"; uv run --isolated --group ragbuild \
    python -m composer.scripts.rag_import "${manifests[@]}" "${forward[@]+"${forward[@]}"}")
