#!/bin/bash
# Ingest the `cvlr_kb` corpus — published documentation + CVLR reference + verification practice.
#
# The corpus has two sources, and they are fed differently.
#
# 1. The published Solana/CVLR manual — the corpus's methodology half — is built HERE, by
#    ./gen_docs.sh, which has always produced solana.html beside the CVL manual it publishes. It is
#    ingested straight into the knowledge base by ragbuild, the same producer the CVL corpus uses;
#    there is no manifest for it, because rebuilding it needs nothing this repo does not have.
#
# 2. TWO manifests sharing the knowledge-base tag, with the importer numbering their sections apart
#    (docs/cvlr-capture-plan.md §8.2):
#
#      cvlr-crates.rag.json    the generated CVLR crate reference: every public item of the pinned
#                              reference set, with compile-gated examples.
#      cvlr-practice.rag.json  project-derived idioms.
#
#    Both are *produced* in the private `certora-cvlr-kb` repo and ship in its package, because
#    that is where the machinery to rebuild them lives — an API key, cargo, and a model. This
#    script does not build them: it finds them and ingests them.
#
# Either source can be missing and the other still lands: the manual without the surface, or the
# surface without the methodology. Both absent is the supported no-corpus state (the backend falls
# back to its static guidance) but not one this script can do anything in, so it is an error.
#
# Manifest resolution, in order:
#   1. any paths given on the command line (before `--`), which override discovery entirely
#   2. $CVLR_KB_REPO/src/certora_cvlr_kb/data/*.rag.json   (a checkout of the private repo)
#   3. the installed `certora_cvlr_kb` package, if importable
#
# Args after `--` are forwarded to rag_import, e.g.:
#   ./populate_cvlr_rag.sh -- --print              # dry run, no DB writes
#   ./populate_cvlr_rag.sh -- --output <conn>      # ignore the registry, write to this DB
set -euo pipefail

script_dir="$(realpath "$(dirname "$0")")"
parent="$(realpath "$script_dir/..")"

# The manual, from this repo's own build. Its PROVENANCE stamp says which docs revision it is, so a
# corpus that carries it can be traced; a missing HTML is a prompt to run gen_docs.sh rather than a
# failure, since the manifests below can still land without it.
manual="$script_dir/prover-docs/solana.html"
ingest_manual() {
    if [[ ! -f "$manual" ]]; then
        echo "No $manual — run ./gen_docs.sh to build it. Skipping the documentation half." >&2
        return 1
    fi
    echo "Ingesting $(basename "$manual") into cvlr_kb ..." >&2
    [[ -f "$script_dir/prover-docs/PROVENANCE" ]] && cat "$script_dir/prover-docs/PROVENANCE" >&2
    (cd "$parent"; uv run --isolated --group ragbuild \
        python -m composer.scripts.ragbuild --knowledge-base cvlr_kb "$manual")
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
    echo "Discovering cvlr_kb manifests ..." >&2

    if [[ -n "${CVLR_KB_REPO:-}" ]]; then
        kb_data="${CVLR_KB_REPO%/}/src/certora_cvlr_kb/data"
        if [[ -d "$kb_data" ]]; then
            for f in "$kb_data"/*.rag.json; do
                manifests+=("$f")
                echo "  found $f" >&2
            done
        else
            echo "  CVLR_KB_REPO is set but $kb_data does not exist" >&2
        fi
    fi

    # The installed package, if present. Only consulted when a checkout did not supply them, so a
    # checkout you are actively editing always wins over an older installed copy.
    if [[ ${#manifests[@]} -eq 0 ]]; then
        probe='
try:
    from certora_cvlr_kb import manifests
    print("\n".join(str(p) for p in manifests()))
except Exception:
    pass
'
        while IFS= read -r line; do
            [[ -n "$line" ]] || continue
            manifests+=("$line")
            echo "  found $line" >&2
        done < <(cd "$parent" && uv run --no-sync python -c "$probe" 2>/dev/null || true)
    fi
fi

if [[ ${#manifests[@]} -eq 0 ]]; then
    # The manual alone is a usable corpus, so try it before giving up.
    if ingest_manual; then
        echo "No manifest found; ingested the manual alone." >&2
        exit 0
    fi
    cat >&2 <<'MSG'
Error: no cvlr_kb manifest found and no local manual, so there is nothing to ingest.

Both manifests ship in the certora-cvlr-kb package. To get them:

  - clone the private certora-cvlr-kb repo and point CVLR_KB_REPO at it, or
  - `pip install certora-cvlr-kb`.

Rebuilding them from source is done in that repo (tools/README.md maps each manifest to its
producer). The documentation half is not among them: run ./gen_docs.sh and this script ingests it
from scripts/prover-docs/solana.html.

Pass manifest paths explicitly to bypass discovery.
MSG
    exit 1
fi

ingest_manual || true

echo "Ingesting ${#manifests[@]} manifest(s) into cvlr_kb ..." >&2
(cd "$parent"; uv run --isolated --group ragbuild \
    python -m composer.scripts.rag_import "${manifests[@]}" "${forward[@]+"${forward[@]}"}")

echo "Done." >&2
