#!/usr/bin/env bash
# verify-solana-image.sh — re-verify built Solana AutoProver images, with no LLM.
#
# Checks the images as they are; it does not build them. Build them first with the commands in
# the header of scripts/docker-compose.solana.yml. Run after every rebuild, and before pinning a
# feature/solana commit into AIAutoProver.
#
#   scripts/verify-solana-image.sh [--examples DIR] [--no-prover]
#
#   1. The Solana image carries its toolchain: cargo, cargo certora-sbf, the platform tools the
#      installed composer builds with, the Prover CLI, and the mounted run-confined launcher.
#   2. Both entrypoint guards fire: console-solana without a launcher, and console-solana in the
#      base image, which has no cargo.
#   3. A Prover round trip: tests/test_cvlr_end_to_end.py inside the Solana container, against a
#      Certora/SolanaExamples checkout. It submits one cloud job, so it needs CERTORAKEY, or a
#      `certora-cloud login` in ~/.certora. --no-prover skips it.
#
# Exits non-zero at the first check that fails. A skipped end-to-end test is a failure: the test
# skips, rather than fails, when the image lacks what it needs.

set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
examples=${SOLANA_EXAMPLES_REPO:-$HOME/src/SolanaExamples}
prover=true

while (($#)); do
  case "$1" in
    --examples) examples=$2; shift 2 ;;
    --no-prover) prover=false; shift ;;
    -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Points certora_cli at a Prover source checkout, which refuses every submission.
unset CERTORA
# The guards read it: an inherited `none` would let console-solana start without a launcher.
unset COMPOSER_SANDBOX_PROVIDER
# Compose refuses to start without it, and the entrypoint checks it is set. Nothing here calls a
# model.
export ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-unused-by-image-verification}
# As the host user: the image's shared CARGO_HOME is root-owned, and a check run as root passes
# where a real run does not.
export HOST_UID=$(id -u) HOST_GID=$(id -g)

scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
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

compose_files=(-f "$repo/scripts/docker-compose.yml")
solana=(docker compose "${compose_files[@]}"
        -f "$repo/scripts/docker-compose.sandbox.yml"
        -f "$repo/scripts/docker-compose.solana.yml"
        -f "$scratch/no-postgres-port.yml"
        --profile solana --profile sandbox run --rm -T)
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

step "1. Toolchain in autoprover-solana"
"${solana[@]}" autoprove-solana bash -euo pipefail -c '
  cargo --version
  cargo certora-sbf --version
  tools=$(python -c "from composer.spec.cvlr.conf import PLATFORM_TOOLS_VERSION; print(PLATFORM_TOOLS_VERSION)")
  test -d "$CERTORA_PLATFORM_TOOLS_ROOT/$tools" \
    || { echo "platform tools $tools missing under $CERTORA_PLATFORM_TOOLS_ROOT" >&2; exit 1; }
  echo "platform tools $tools at $CERTORA_PLATFORM_TOOLS_ROOT/$tools"
  python -c "import certora_cli; print(\"certora_cli imports\")"
  test -x "$RUN_CONFINED_BIN" || { echo "$RUN_CONFINED_BIN is not executable" >&2; exit 1; }
  echo "run-confined at $RUN_CONFINED_BIN"
' || fail "the Solana image is missing part of its toolchain"

step "2a. console-solana refuses to start without a launcher"
expect_refusal "no run-confined launcher is mounted" \
  "${solana[@]}" -e RUN_CONFINED_BIN=/nonexistent autoprove-solana console-solana

step "2b. console-solana refuses to start in the base image"
# Unconfined, so the launcher guard lets it through to the cargo guard.
expect_refusal "needs a Rust toolchain" \
  "${base[@]}" -e COMPOSER_SANDBOX_PROVIDER=none autoprove console-solana

if [[ $prover == false ]]; then
  step "3. Prover round trip: skipped (--no-prover)"
  exit 0
fi

step "3. Prover round trip: tests/test_cvlr_end_to_end.py"
[[ -f "$examples/cvlr_by_example/first_example/Cargo.toml" ]] \
  || fail "no SolanaExamples checkout at $examples; clone https://github.com/Certora/SolanaExamples and pass --examples"
if [[ -z "${CERTORAKEY:-}" && ! -d "$HOME/.certora" ]]; then
  fail "set CERTORAKEY or run \`certora-cloud login\`: the test submits a cloud job"
fi
# Only tests/ and the pytest config are mounted, so the test imports the image's composer and not
# this checkout's. The image has no pytest; it is installed into the throwaway container.
status=0
"${solana[@]}" \
  -v "$repo/tests:/verify/tests:ro" \
  -v "$repo/pyproject.toml:/verify/pyproject.toml:ro" \
  -v "$(cd "$examples" && pwd):/examples:ro" \
  -e SOLANA_EXAMPLES_REPO=/examples \
  autoprove-solana bash -euo pipefail -c '
    pip install --quiet --disable-pip-version-check --target /tmp/pytest-deps \
      pytest pytest-asyncio json5
    # Importing composer writes .certora_internal into the working directory, and the mount
    # points are root-owned.
    cd "$(mktemp -d)"
    PYTHONPATH=/tmp/pytest-deps python -m pytest -c /verify/pyproject.toml --rootdir /verify \
      /verify/tests/test_cvlr_end_to_end.py -m expensive -rs -p no:cacheprovider
  ' 2>&1 | tee "$scratch/e2e" || status=$?
[[ $status -eq 0 ]] || fail "the end-to-end test failed (exit $status)"
if grep -qE '[0-9]+ skipped' "$scratch/e2e"; then
  fail "the end-to-end test skipped; the reason is above"
fi
grep -qE '[0-9]+ passed' "$scratch/e2e" || fail "the end-to-end test ran nothing"

printf '\nAll checks passed.\n'
