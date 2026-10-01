#!/bin/bash
# Ingest the two CVLR corpora.
#
#   cvlr_kb      the Solana manual (solana.html, from gen_docs.sh). Prose and methodology,
#                hand-written, allowed to lag the crates.
#   cvlr_api_kb  the CVLR API, generated here by composer.scripts.cvlr_api_docs from rustdoc over
#                the releases composer/spec/cvlr_reference.py pins.
#
# Each manifest declares its own knowledge_base, and rag_import routes by that tag, so both land
# from one call. See docs/cvlr-api-docs-plan.md. Any source can be missing and the others still
# land; all missing is an error.
#
# Manifest resolution, in order:
#   1. paths given before `--`, which skip generation
#   2. the generated cvlr_api_kb manifest
#
# There is no third source. `cvlr_kb` was once filled from manifests built in certora-cvlr-kb -- a
# crate reference that cvlr_api_kb now replaces and does better, and practice entries that moved to
# the always-in-context bundle and the recipes (cvlr-knowledge-plan.md §4). Discovering them here
# would find nothing, or find a stale crate reference to contradict the generated one.
#
# --crate-source PATH (repeatable) generates cvlr_api_kb from a local CVLR checkout instead of the
# published crates, which is how documentation gets read before it is released. The pin still says
# which crates the corpus holds; the checkout only says what is in them. The entries are identical
# to the ones the same crates produce once published -- a corpus like this is built to be tried,
# so only the manifest's source line records that a checkout was read.
#
# Args after `--` go to rag_import:
#   ./populate_cvlr_rag.sh -- --print                          # dry run
#   ./populate_cvlr_rag.sh --crate-source ~/src/cvlr --crate-source ~/src/cvlr-solana
set -euo pipefail

script_dir="$(realpath "$(dirname "$0")")"
parent="$(realpath "$script_dir/..")"
docs_dir="$script_dir/prover-docs"

# A missing manual is not fatal: the manifests can still land without it.
ingest_manual() {
    if [[ ! -f "$docs_dir/solana.html" ]]; then
        echo "No $docs_dir/solana.html -- run ./gen_docs.sh. Skipping the manual." >&2
        return 1
    fi
    # PROVENANCE names the docs revision, which is the only record of what this HTML is.
    [[ -f "$docs_dir/PROVENANCE" ]] && cat "$docs_dir/PROVENANCE" >&2
    (cd "$parent"; uv run --isolated --group ragbuild \
        python -m composer.scripts.ragbuild --corpus cvlr "$docs_dir/solana.html")
}

# The API corpus is built here rather than found: it needs cargo and a nightly toolchain and
# nothing else, which is the whole reason it moved into this repo.
build_api_docs() {
    local out="$1"
    if ! (cd "$parent"; uv run --no-sync python -m composer.scripts.cvlr_api_docs \
            --output "$out" "${crate_sources[@]+"${crate_sources[@]}"}"); then
        echo "Could not generate the CVLR API corpus; continuing with whatever else is available." >&2
        echo "  It needs a nightly toolchain (rustup toolchain install nightly) and a warm cargo registry." >&2
        return 1
    fi
}

manifests=()
forward=()
crate_sources=()
seen_ddash=0
want_source=0
for arg in "$@"; do
    if [[ $seen_ddash -eq 1 ]]; then
        forward+=("$arg")
    elif [[ $want_source -eq 1 ]]; then
        crate_sources+=(--crate-source "$arg")
        want_source=0
    elif [[ "$arg" == "--" ]]; then
        seen_ddash=1
    elif [[ "$arg" == "--crate-source" ]]; then
        want_source=1
    elif [[ "$arg" == --crate-source=* ]]; then
        crate_sources+=(--crate-source "${arg#*=}")
    else
        manifests+=("$arg")
    fi
done
if [[ $want_source -eq 1 ]]; then
    echo "Error: --crate-source needs a path." >&2
    exit 2
fi

# An explicit manifest list skips generation entirely, so a checkout passed with one would be
# silently ignored -- and "it ingested the published API" is indistinguishable from success.
if [[ ${#crate_sources[@]} -gt 0 && ${#manifests[@]} -gt 0 ]]; then
    echo "Error: --crate-source generates a manifest, so it cannot be combined with manifest" >&2
    echo "paths, which skip generation. Pass one or the other." >&2
    exit 2
fi

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
    if ingest_manual; then
        echo "The API corpus could not be generated; ingested the manual alone." >&2
        exit 0
    fi
    cat >&2 <<'MSG'
Error: nothing to ingest -- the API corpus could not be generated and there is no local manual.

  cvlr_api_kb  needs a nightly toolchain and a warm cargo registry; see the error above.
  cvlr_kb      needs ./gen_docs.sh for the manual.

Pass manifest paths explicitly to skip generation.
MSG
    exit 1
fi

ingest_manual || true

# No --output: each manifest carries its own knowledge_base and rag_import groups by the
# connection that resolves to, which is what lets one call fill two corpora.
echo "Ingesting ${#manifests[@]} manifest(s) ..." >&2
(cd "$parent"; uv run --isolated --group ragbuild \
    python -m composer.scripts.rag_import "${manifests[@]}" "${forward[@]+"${forward[@]}"}")
