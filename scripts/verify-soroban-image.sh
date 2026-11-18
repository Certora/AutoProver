#!/usr/bin/env bash
# verify-soroban-image.sh — re-verify the built Soroban AutoProver image, with no LLM and no Prover.
#
# Checks the images as they are; it does not build them. Build them first with the commands in
# the header of scripts/docker-compose.soroban.yml. Run after every rebuild, and before pinning a
# feature/soroban commit into AIAutoProver.
#
#   scripts/verify-soroban-image.sh [--unconfined]
#
#   1. The Soroban image carries its toolchain: the host Rust toolchain, the Soroban one with its
#      wasm32v1-none standard library, the Prover CLI, and the mounted run-confined launcher.
#   2. The kernel enforces Landlock (`run-confined --probe`). Without it every confined build fails.
#   3. Both entrypoint guards fire: console-soroban without a launcher, and console-soroban in the
#      base image, which has no cargo.
#   4. A confined scaffold, gate and wasm build of a one-function contract
#      (scripts/verify_soroban_build.py). It fetches crates from crates.io, and calls no model and
#      no Prover.
#
# --unconfined skips check 2 and runs check 4 without the sandbox. For an Apple Silicon host: the
# image is linux/amd64, and under emulation Landlock is unavailable whatever the kernel supports.
# The sandbox is then checked on the Batch pool's hosts instead (run-confined --probe there).
#
# Runs under a Compose project of its own, so it never recreates the containers of another one,
# such as a local postgres, and removes it on exit. Exits non-zero at the first check that fails.

set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

confined=true

while (($#)); do
  case "$1" in
    --unconfined) confined=false; shift ;;
    -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# The guards read it: an inherited `none` would let console-soroban start without a launcher.
unset COMPOSER_SANDBOX_PROVIDER
# Compose refuses to start without it, and the entrypoint checks it is set. Nothing here calls a
# model.
export ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-unused-by-image-verification}
# As the host user: the image's shared CARGO_HOME is root-owned, and a check run as root passes
# where a real run does not.
export HOST_UID=$(id -u) HOST_GID=$(id -g)

project=autoprove-soroban-verify
scratch=$(mktemp -d)
cleanup() {
  docker compose -p "$project" -f "$repo/scripts/docker-compose.yml" \
      -f "$repo/scripts/docker-compose.sandbox.yml" -f "$repo/scripts/docker-compose.soroban.yml" \
      --profile soroban --profile sandbox --profile autoprove down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$scratch"
}
trap cleanup EXIT
# /work is $PWD by default. In this checkout, its rust-toolchain.toml makes cargo try to install a
# toolchain into the image's read-only RUSTUP_HOME.
mkdir "$scratch/work"
export HOST_WORK_DIR=$scratch/work
# Nothing here reaches postgres from the host, and the host may already have one on 5432.
cat >"$scratch/no-postgres-port.yml" <<'EOF'
services:
  postgres:
    ports: !reset []
EOF

compose_files=(-p "$project" -f "$repo/scripts/docker-compose.yml")
soroban=(docker compose "${compose_files[@]}"
         -f "$repo/scripts/docker-compose.sandbox.yml"
         -f "$repo/scripts/docker-compose.soroban.yml"
         -f "$scratch/no-postgres-port.yml"
         --profile soroban --profile sandbox run --rm -T)
base=(docker compose "${compose_files[@]}"
      -f "$scratch/no-postgres-port.yml"
      --profile autoprove run --rm -T)

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# Runs "$@", expects exit status 2 and stderr containing $expected.
expect_refusal() {
  local expected=$1; shift
  local status=0
  "$@" >"$scratch/out" 2>&1 || status=$?
  if [[ $status -ne 2 ]] || ! grep -qF -- "$expected" "$scratch/out"; then
    cat "$scratch/out" >&2
    fail "expected exit 2 and '$expected', got exit $status"
  fi
  echo "ok: refused with '$expected'"
}

step "1. Toolchain in autoprover-soroban"
"${soroban[@]}" autoprove-soroban bash -euo pipefail -c '
  read -r host soroban target < <(python -c "
from composer.cargo.wasm import WASM_TARGET
from composer.spec.cvlr.conf import HOST_RUST_TOOLCHAIN, SOROBAN_RUST_TOOLCHAIN
print(HOST_RUST_TOOLCHAIN, SOROBAN_RUST_TOOLCHAIN, WASM_TARGET)")
  cargo "+$host" --version
  cargo "+$soroban" --version
  rustup target list --installed --toolchain "$soroban" | grep -qx "$target" \
    || { echo "$target is not installed for $soroban" >&2; exit 1; }
  echo "$target installed for $soroban"
  python -c "import certora_cli; print(\"certora_cli imports\")"
  test -x "$RUN_CONFINED_BIN" || { echo "$RUN_CONFINED_BIN is not executable" >&2; exit 1; }
  echo "run-confined at $RUN_CONFINED_BIN"
' || fail "the Soroban image is missing part of its toolchain"

if [[ $confined == true ]]; then
  step "2. The kernel enforces Landlock"
  "${soroban[@]}" autoprove-soroban bash -c '"$RUN_CONFINED_BIN" --probe' \
    || fail "run-confined --probe failed: every confined build would fail on this kernel (on Apple Silicon, rerun with --unconfined)"
else
  step "2. The kernel enforces Landlock: skipped (--unconfined)"
fi

step "3a. console-soroban refuses to start without a launcher"
expect_refusal "no run-confined launcher is mounted" \
  "${soroban[@]}" -e RUN_CONFINED_BIN=/nonexistent autoprove-soroban console-soroban

step "3b. console-soroban refuses to start in the base image"
# Unconfined, so the launcher guard lets it through to the cargo guard.
expect_refusal "needs a Rust toolchain" \
  "${base[@]}" -e COMPOSER_SANDBOX_PROVIDER=none autoprove console-soroban

if [[ $confined == true ]]; then
  step "4. Confined scaffold, gate and wasm build"
  unconfined_env=()
else
  step "4. Scaffold, gate and wasm build, unconfined (--unconfined)"
  # Unconfined, every cargo command uses $CARGO_HOME, and the image's is root-owned: a confined
  # build gets a private one per run, an unconfined one does not.
  unconfined_env=(-e COMPOSER_SANDBOX_PROVIDER=none -e CARGO_HOME=/tmp/verify-cargo-home)
fi
# Only the script is mounted, so it imports the image's composer and not this checkout's.
"${soroban[@]}" "${unconfined_env[@]}" \
  -v "$repo/scripts/verify_soroban_build.py:/verify/verify_soroban_build.py:ro" \
  autoprove-soroban bash -euo pipefail -c '
    # Importing composer writes .certora_internal into the working directory.
    cd "$(mktemp -d)"
    python /verify/verify_soroban_build.py /work/probe
  ' || fail "the Soroban build failed"

printf '\nAll checks passed.\n'
