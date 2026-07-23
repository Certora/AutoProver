#!/usr/bin/env bash
#
# Runs INSIDE the QEMU Amazon Linux 2023 (kernel 6.1) guest — see
# .github/workflows/sandbox-escape-6.1.yml. Provisions a minimal toolchain and
# runs the sandbox escape suite against the *production* kernel version.
#
# Why the guest at all: Landlock + seccomp are kernel-mediated, and a container
# shares the host kernel — so the only faithful way to test run-confined on the
# prod 6.1 kernel is to boot that kernel (docs/command-sandbox.md §8). On 6.1 the
# suite exercises the 6.1 contract specifically: Landlock FS enforced, network is
# seccomp-only (no Landlock net rules < 6.7), and scopes are absent (< 6.12, so
# the signal / abstract-UDS asserts self-skip). The x32 deny-mirror is asserted
# regardless (§11 item 2).
#
# Kept dependency-light on purpose: the escape suite imports only stdlib +
# composer.sandbox.* (all stdlib) + pytest, so we install just pytest via uv and
# put the repo on PYTHONPATH — no project build, no numpy/psycopg/langchain. We
# pass --noconftest so tests/conftest.py (which imports those heavy deps) is not
# collected; the suite's fixtures are all in-module.
set -euo pipefail

REPO="${1:?usage: sandbox_vm_provision.sh <repo-dir>}"
JUNIT="${REPO}/sandbox-escape-junit.xml"

section() { printf '\n=== %s ===\n' "$*"; }

section "Guest kernel — this is what the sandbox is actually tested against"
uname -srm
# The behavior of run-confined is version-gated; record it so a CI artifact shows
# exactly what protected the run (not just pass/skip).
KREL="$(uname -r)"
if [ -r "/boot/config-${KREL}" ]; then
  X32="$(grep -E '^CONFIG_X86_X32_ABI' "/boot/config-${KREL}" || echo 'CONFIG_X86_X32_ABI not set')"
elif [ -r /proc/config.gz ]; then
  X32="$(zcat /proc/config.gz | grep -E '^CONFIG_X86_X32_ABI' || echo 'CONFIG_X86_X32_ABI not set')"
else
  X32="kernel config not available in guest"
fi
echo "x32 ABI: ${X32}"
echo "(The x32 deny-mirror is asserted by the suite regardless — this only records"
echo " whether the bypass was ever live on this kernel build.)"

section "Toolchain: gcc (linker + rustc's cc), git"
sudo dnf -y -q install gcc git tar >/dev/null

section "Rust (rustup, minimal profile) — builds run-confined + compiles the malicious probe"
if ! command -v rustc >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain stable
fi
# shellcheck disable=SC1091
source "${HOME}/.cargo/env"
rustc --version

section "uv (isolated pytest env on Python 3.12 — AL2023 ships 3.11)"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="${HOME}/.local/bin:${PATH}"
uv --version

section "Build run-confined (release)"
cargo build -p run-confined --release --manifest-path "${REPO}/rust/Cargo.toml"
export RUN_CONFINED_BIN="${REPO}/rust/target/release/run-confined"

section "Landlock probe on the 6.1 kernel (drives fail-closed available())"
# Non-fatal: the suite's own skip-guard depends on this, but we want the output.
"${RUN_CONFINED_BIN}" --probe || {
  echo "!! run-confined --probe reported Landlock NOT enforcing on this kernel."
  echo "!! On a real 6.1 kernel this should succeed (Landlock floor is 5.13)."
  exit 3
}

section "Escape suite against kernel ${KREL}"
export PYTHONPATH="${REPO}"
# --no-project: don't build AutoProver; --with: ephemeral pytest env.
# --noconftest: skip tests/conftest.py's heavy imports (fixtures here are in-module).
uv run --no-project --python 3.12 \
  --with 'pytest>=9.0' --with 'pytest-asyncio>=1.3' \
  pytest --noconftest -v \
  --junitxml="${JUNIT}" \
  "${REPO}/tests/test_sandbox_escape.py"
