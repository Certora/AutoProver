#!/bin/bash
# refresh_rag.sh — rebuild the documentation RAG corpora from the published docs, end to end.
#
#   1. (re)generate the docs HTML   -> gen_docs.sh
#   2. rebuild the corpus files     -> populate_rag.sh / populate_extended_rag.sh
#
# Each corpus is a single file under $CERTORA_AI_COMPOSER_RAG_DIR (default
# ~/.local/share/autoprover/rag). A rebuild writes a scratch file and swaps it in only when it
# finishes, so this is safe to run while something is searching the old corpus.
set -euo pipefail

script_dir="$(realpath "$(dirname "$0")")"

do_gen=1        # 0 with --skip-gen-docs to reuse existing scripts/prover-docs/*.html
do_default=1    # cvl.sqlite      (CVL only; used by AI Composer + cex-analyzer)
do_extended=0   # extended.sqlite (CVL + prover + user-guide; used by sanity-analyzer)

usage() {
    cat <<'EOF'
Usage: scripts/refresh_rag.sh [options]

Regenerate the documentation and rebuild the CVL-manual RAG corpora.

Options:
  --skip-gen-docs   Skip the gen_docs.sh step and rebuild from the HTML already
                    present in scripts/prover-docs/. Use when the docs are fresh.
  --extended        ALSO rebuild the extended corpus (CVL + prover + user-guide).
  --all             Rebuild both corpora.
  --only-extended   Rebuild ONLY the extended corpus.
  -h, --help        Show this help.

Default (no options): regenerate docs, then rebuild the CVL corpus only.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-gen-docs) do_gen=0 ;;
        --extended)      do_extended=1 ;;
        --all)           do_default=1; do_extended=1 ;;
        --only-extended) do_default=0; do_extended=1 ;;
        -h|--help)       usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; echo >&2; usage >&2; exit 1 ;;
    esac
    shift
done

if [[ $do_gen -eq 1 ]]; then
    echo "==> Regenerating documentation HTML (gen_docs.sh) ..."
    bash "$script_dir/gen_docs.sh"
fi

if [[ $do_default -eq 1 ]]; then
    echo "==> Rebuilding the CVL corpus (populate_rag.sh) ..."
    bash "$script_dir/populate_rag.sh"
fi

if [[ $do_extended -eq 1 ]]; then
    echo "==> Rebuilding the extended corpus (populate_extended_rag.sh) ..."
    bash "$script_dir/populate_extended_rag.sh"
fi

echo
echo "✅ RAG refresh complete."
