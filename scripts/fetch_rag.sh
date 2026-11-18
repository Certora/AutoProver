#!/bin/bash
# fetch_rag.sh — copy the prebuilt RAG corpora out of the AutoProver Docker image.
#
# The image builds every built-in corpus (scripts/Dockerfile's rag-builder stage). Copying them out
# gives a dev host exactly the corpora the container searches, without the docs render and the
# embedding that scripts/refresh_rag.sh does. They land in $CERTORA_AI_COMPOSER_RAG_DIR (default
# ~/.local/share/autoprover/rag), where every entry point opens them by default.
set -euo pipefail

image="autoprover-local:latest"

usage() {
    cat <<'EOF'
Usage: scripts/fetch_rag.sh [--image IMAGE]

Copy the RAG corpora out of an AutoProver image into $CERTORA_AI_COMPOSER_RAG_DIR
(default ~/.local/share/autoprover/rag), replacing any already there.

Options:
  --image IMAGE   The image to copy from (default: autoprover-local:latest, the one
                  scripts/autoprove builds).
  -h, --help      Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --image) image="${2:?--image needs a value}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; echo >&2; usage >&2; exit 1 ;;
    esac
    shift
done

if ! docker image inspect "$image" >/dev/null 2>&1; then
    cat >&2 <<EOF
Error: no image $image. Build it with
    docker compose -f scripts/docker-compose.yml --profile autoprove build autoprove
(scripts/autoprove also builds it on first use), or build the corpora from the docs instead
with scripts/refresh_rag.sh.
EOF
    exit 1
fi

# Where the image keeps them is the image's own setting, not something to assume here.
src="$(docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$image" \
    | sed -n 's/^CERTORA_AI_COMPOSER_RAG_DIR=//p')"
if [[ -z "$src" ]]; then
    echo "Error: $image sets no CERTORA_AI_COMPOSER_RAG_DIR; it predates baked-in corpora. Rebuild it." >&2
    exit 1
fi

dest="${CERTORA_AI_COMPOSER_RAG_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/autoprover/rag}"
mkdir -p "$dest"

container="$(docker create "$image")"
# Staged beside the destination, so moving each file into place is a rename: a run that is
# searching a corpus sees either the old file or the new one, never a partial copy.
staging="$(mktemp -d "$dest/.fetch.XXXXXX")"
cleanup() {
    docker rm -f "$container" >/dev/null 2>&1 || true
    rm -rf -- "$staging"
}
trap cleanup EXIT

docker cp --quiet "$container:$src/." "$staging/"
shopt -s nullglob
corpora=("$staging"/*.sqlite)
if [[ ${#corpora[@]} -eq 0 ]]; then
    echo "Error: $image has no corpus files in $src." >&2
    exit 1
fi
for f in "${corpora[@]}"; do
    mv -f -- "$f" "$dest/"
    echo "  $(basename "$f")"
done
echo "Copied ${#corpora[@]} corpora from $image into $dest."
