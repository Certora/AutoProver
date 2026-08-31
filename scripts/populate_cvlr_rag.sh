#!/bin/bash
# Ingest the `cvlr_kb` corpus — published documentation + CVLR reference + verification practice.
#
# The corpus is fed by THREE manifests sharing one knowledge-base tag, and the importer numbers
# their sections apart (docs/cvlr-capture-plan.md §8.2):
#
#   cvlr-docs.rag.json      the published Solana/CVLR manual — the corpus's methodology half.
#   cvlr-crates.rag.json    the generated CVLR crate reference: every public item of the pinned
#                           reference set, with compile-gated examples.
#   cvlr-practice.rag.json  project-derived idioms.
#
# All three are PUBLIC content in two cases out of three, but all three are *produced* in the
# private `certora-cvlr-kb` repo and ship in its package, because that is where the machinery to
# rebuild them lives — an API key and cargo for the crate reference, a sphinx build for the docs.
# So this script does not build anything: it finds manifests and ingests them.
#
# Without access to that package there is no CVLR corpus. That is a supported state — the backend
# falls back to its static guidance — but it is not a state this script can do anything useful in,
# so finding nothing is an error rather than a warning.
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
    cat >&2 <<'MSG'
Error: no cvlr_kb manifest found, so there is nothing to ingest.

All three manifests ship in the certora-cvlr-kb package. To get them:

  - clone the private certora-cvlr-kb repo and point CVLR_KB_REPO at it, or
  - `pip install certora-cvlr-kb`.

Rebuilding them from source is done in that repo (tools/README.md maps each manifest to its
producer); nothing here builds a CVLR manifest any more.

Pass manifest paths explicitly to bypass discovery.
MSG
    exit 1
fi

echo "Ingesting ${#manifests[@]} manifest(s) into cvlr_kb ..." >&2
(cd "$parent"; uv run --isolated --group ragbuild \
    python -m composer.scripts.rag_import "${manifests[@]}" "${forward[@]+"${forward[@]}"}")

echo "Done." >&2
