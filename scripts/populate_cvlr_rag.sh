#!/bin/bash
# Ingest the `cvlr_kb` corpus — CVLR reference + verification practice.
#
# The corpus is fed by SEVERAL manifests sharing one knowledge-base tag
# (docs/cvlr-capture-plan.md §8.2). This script ingests whichever of them it can find, because
# the halves have deliberately different availability:
#
#   cvlr-docs.rag.json      the published Solana/CVLR documentation. Built HERE, offline, from a
#                           docs checkout — so a plain AutoProver install always has this half.
#   cvlr-crates.rag.json    the generated CVLR crate reference: every public item of the pinned
#                           reference set, with compile-gated examples. Content is public (it is
#                           derived from published crates), but REBUILDING it costs an API key and
#                           a cargo toolchain, so it is generated in the `certora-cvlr-kb` repo and
#                           ships with that package rather than being built here.
#   cvlr-practice.rag.json  project-derived idioms, from the same private repo.
# The last two are absent for anyone without access to that package — a SUPPORTED state, not a
# failure: you get the documentation without the generated reference or the practice entries.
#
# So a missing practice manifest is a warning, and a missing docs manifest is a warning, but
# finding nothing at all is an error — that means the corpus would be empty and the caller almost
# certainly did not mean to do that.
#
# Manifest resolution, in order:
#   1. any paths given on the command line (before `--`), which override discovery entirely
#   2. $CVLR_KB_REPO/src/certora_cvlr_kb/data/*.rag.json   (a checkout of the private repo)
#   3. the installed `certora_cvlr_kb` package, if importable
#   4. scripts/cvlr-docs/*.rag.json                        (locally built documentation manifest)
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

# Always succeeds: a miss is the normal case (each manifest half is independently optional), and
# under `set -e` a non-zero return from the last command in a loop body would kill the script.
add_if_file() {
    if [[ -f "$1" ]]; then
        manifests+=("$1")
        echo "  found $1" >&2
    fi
    return 0
}

# Unmatched globs must vanish rather than being passed through as literal patterns.
shopt -s nullglob

if [[ ${#manifests[@]} -eq 0 ]]; then
    echo "Discovering cvlr_kb manifests ..." >&2

    if [[ -n "${CVLR_KB_REPO:-}" ]]; then
        kb_data="${CVLR_KB_REPO%/}/src/certora_cvlr_kb/data"
        if [[ -d "$kb_data" ]]; then
            for f in "$kb_data"/*.rag.json; do add_if_file "$f"; done
        else
            echo "  CVLR_KB_REPO is set but $kb_data does not exist" >&2
        fi
    fi

    # The installed package, if present. Only consulted when a checkout did not supply it, so a
    # checkout you are actively editing always wins over an older installed copy.
    if [[ ${#manifests[@]} -eq 0 ]]; then
        probe='
try:
    from certora_cvlr_kb import practice_manifest
    p = practice_manifest()
    print(p if p.is_file() else "")
except Exception:
    print("")
'
        pkg_manifest="$(cd "$parent" && uv run --no-sync python -c "$probe" 2>/dev/null || true)"
        if [[ -n "$pkg_manifest" ]]; then
            add_if_file "$pkg_manifest"
        fi
    fi

    for f in "$script_dir"/cvlr-docs/*.rag.json; do add_if_file "$f"; done
fi

if [[ ${#manifests[@]} -eq 0 ]]; then
    cat >&2 <<'MSG'
Error: no cvlr_kb manifest found, so there is nothing to ingest.

  - For the project-derived half: clone the private certora-cvlr-kb repo and point
    CVLR_KB_REPO at it, or `pip install certora-cvlr-kb`.
  - For the public half, which needs no private access:
        ./scripts/gen_docs.sh                       # builds scripts/prover-docs/solana.html
        uv run python -m composer.scripts.cvlr_docs_manifest scripts/prover-docs/solana.html
    That writes scripts/cvlr-docs/cvlr-docs.rag.json, which this script then finds. The generated
    crate reference and the practice entries both ship in the certora-cvlr-kb package.

Pass manifest paths explicitly to bypass discovery.
MSG
    exit 1
fi

echo "Ingesting ${#manifests[@]} manifest(s) into cvlr_kb ..." >&2
(cd "$parent"; uv run --isolated --group ragbuild \
    python -m composer.scripts.rag_import "${manifests[@]}" "${forward[@]+"${forward[@]}"}")

echo "Done." >&2
