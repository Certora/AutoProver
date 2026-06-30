#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." &>/dev/null && pwd)

cd "$REPO_ROOT"

compose_files=(
    -f "$REPO_ROOT/scripts/docker-compose.yml"
)

DEST_DIR=$(mktemp -p "$REPO_ROOT/tests" -d --suffix '.autoprover-test')

for i in 1 2 3 4 5; do
    cp -r "$REPO_ROOT/tests/smoketest" "$DEST_DIR/$i"
    echo "launching on $DEST_DIR/$i"
    (
        export HOST_WORK_DIR="$DEST_DIR/$i"
        export HOST_UID=$(id -u) HOST_GID=$(id -g)

        docker compose \
            "${compose_files[@]}" \
            --project-directory "$REPO_ROOT" \
            --profile autoprove run --rm autoprove \
            console-autoprove --cloud /work/ /work/src/Answer.sol:Answer /work/design.md \
            |& tee "$DEST_DIR/$i/$(basename "$DEST_DIR/$i").log"
    ) &
    sleep 2
done
wait
