#!/bin/bash
set -euo pipefail

host_dir="$(realpath "$(dirname "$0")")/prover-docs"
mkdir -p "$host_dir"
doc_dir="$(mktemp -d)"
venv_dir="$(mktemp -d)"

cleanup() {
    [[ $(type -t deactivate) == function ]] && deactivate
    rm -rf "$doc_dir" "$venv_dir"
}
trap cleanup EXIT

git clone --depth 1 git@github.com:Certora/Documentation.git "$doc_dir"

# Record which revision these manuals were built from. The clone is a temp dir that this script
# deletes, so without this the built HTML has no traceable origin — and a corpus derived from it
# could not say which docs it is reporting. The CVLR corpus's docs producer lives in the
# certora-cvlr-kb repo and reuses both this HTML and this stamp when it finds them.
printf 'Certora/Documentation %s (%s)\n' \
    "$(git -C "$doc_dir" rev-parse HEAD)" \
    "$(git -C "$doc_dir" log -1 --format=%cI)" \
    > "$host_dir/PROVENANCE"

python3 -m venv "$venv_dir"
source "$venv_dir/bin/activate"
pip install -r "$doc_dir/requirements.txt"

for target in "$doc_dir"/docs/{cvl,solana,prover,user-guide}/ ; do
    cp "$doc_dir/conf.py" "$target"
    cd "$target"
    sphinx-build -M singlehtml . tmp
    cp tmp/singlehtml/index.html "$host_dir/$(basename "$target").html"
done
