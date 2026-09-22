#!/usr/bin/env bash
# Record the CVLR smoke tape (docs/cvlr-backend-plan.md §6, §7.8.5).
#
# One real, paid run of test_scenarios/solana_vault_idl through the shipped console-solana entry
# point, captured by composer.testing.record_tape into
# composer/testing/ui_harness_cvlr_vault.py. Re-run it whenever the tape has to be rebuilt; the
# scenario, the flags and the model settings all live in composer/testing/cvlr_tape.py, so this
# script chooses nothing about the run and cannot drift from what the replay test drives.
#
#   scripts/record_cvlr_tape.sh
#
# Then curate the draft (the `generate-tape` skill's step 3) and verify with:
#
#   uv run --no-sync pytest tests/test_cvlr_tape.py -m expensive -q -s
#
# What this script is really for is the prerequisites. Every one of them below has been observed
# failing *late* — after the run has been paid for — in a message that names neither itself nor the
# fix, so each is checked up front and named here instead.

set -euo pipefail

cd "$(dirname "$0")/.."

fail() { echo "record_cvlr_tape: $*" >&2; exit 1; }

# $CERTORA points certora_cli at a local Prover build, which reports itself as "no package
# installed" and refuses every cloud submission *before upload*, naming neither the variable nor
# itself. The loop reads that as a build failure and rewrites rules that were never wrong.
# An `if`, not `[[ … ]] && fail`: under `set -e` the latter exits the script with status 1 whenever
# the test is false, i.e. in exactly the case where everything is fine.
if [[ -n "${CERTORA:-}" ]]; then
  fail "\$CERTORA is set. Re-run under \`env -u CERTORA\`."
fi

# The recorder is installed from composer/bind.py by this variable, and the tape is named by it.
# Read from the scenario module so the name cannot disagree with the replay's.
TAPE_NAME=$(uv run --no-sync python -c 'from composer.testing.cvlr_tape import TAPE_NAME; print(TAPE_NAME)')
[[ -n "$TAPE_NAME" ]] || fail "could not read TAPE_NAME from composer.testing.cvlr_tape"

: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY is unset — \`source ~/.autoProverAnthropicApiKey.sh\` first}"
: "${CERTORAKEY:?CERTORAKEY is unset — cloud submission needs it}"

command -v cargo >/dev/null || fail "cargo is not on PATH"
cargo certora-sbf --version >/dev/null 2>&1 \
  || fail "\`cargo certora-sbf\` is not installed (cargo install cargo-certora-sbf)"

# The platform-tools release every build uses. A confined build cannot fetch it.
uv run --no-sync python - <<'PY' || exit 1
import sys
from composer.cargo.sbf import PLATFORM_TOOLS_ROOT, platform_tools_installed
from composer.spec.cvlr.conf import PLATFORM_TOOLS_VERSION

wanted = PLATFORM_TOOLS_VERSION
if not platform_tools_installed(wanted):
    sys.exit(
        f"record_cvlr_tape: Solana platform tools {wanted} are not installed under "
        f"{PLATFORM_TOOLS_ROOT}. Install once, unconfined: "
        f"cargo certora-sbf --no-build --tools-version {wanted}"
    )
PY

# Confinement is on by default and fail-closed, so a missing launcher stops the run several
# services in, in a message about a provider rather than about this machine.
uv run --no-sync python - <<'PY' || exit 1
import asyncio, sys
from composer.sandbox.policy import SandboxUnavailable, ensure_available
from composer.spec.cvlr.entry import build_confinement

sandbox = build_confinement()
if not sandbox.enabled:
    print("record_cvlr_tape: WARNING — COMPOSER_SANDBOX_PROVIDER=none, recording unconfined", file=sys.stderr)
else:
    try:
        asyncio.run(ensure_available(sandbox.resolve_provider()))
    except SandboxUnavailable as exc:
        sys.exit(f"record_cvlr_tape: {exc}")
PY

# Postgres backs the checkpointer and the store. Probed through the same config the run uses, per
# database, because the failure that matters is not "postgres is down" but "this cluster was
# provisioned before a schema existed" — which reaches a run as a role that cannot log in.
uv run --no-sync python - <<'PY' || exit 1
import sys
import psycopg
from composer.workflow.services import _DATABASE_CONFIGS, _get_composer_connection_string

for name, config in _DATABASE_CONFIGS.items():
    dsn = _get_composer_connection_string(**config)
    try:
        psycopg.connect(dsn).close()
    except Exception as exc:
        sys.exit(
            f"record_cvlr_tape: cannot reach the {name} database ({exc}). Bring the cluster up and "
            f"apply the schema:\n"
            f"  docker compose -f scripts/docker-compose.yml up -d postgres\n"
            f"  psql -h localhost -U postgres -f composer/scripts/init-db.sql"
        )
PY

# The Certora access token expires (~days) and only the *results* path notices: submission still
# works on CERTORAKEY, then fetch_sources_and_treeview_files falls back to an interactive browser
# PKCE flow and burns its 300s deadline per attempt in a headless run. Calling login() here renews
# it silently while the refresh token is valid; when it is not, the same PKCE fallback happens
# *here*, which is why this is under a timeout — a refresh that has to become a login is a person's
# job, not this script's.
if ! CERTORA_LOGIN_NO_BROWSER=true timeout 120 \
      uv run --no-sync python -c 'import certora_login; certora_login.login(force_file=True)'; then
  fail "Certora login could not be refreshed. Re-authenticate interactively — this opens a browser
and needs a click within 300s:
  uv run --no-sync python -c 'from certora_login.cli import main; main()' --force-file --force-refresh"
fi

echo "record_cvlr_tape: prerequisites ok; recording tape '$TAPE_NAME'" >&2
uv run --no-sync python - <<'PY'
from composer.pipeline.cli import parse_budget_file
from composer.testing.cvlr_tape import BUDGET
import sys
budget = parse_budget_file(BUDGET)
print(
    f"record_cvlr_tape: this is a real, paid run, bounded at ${budget.total:.0f} "
    f"({BUDGET.name}). The first one cost $166 and had to be interrupted.",
    file=sys.stderr,
)
PY

# Thinking is left ON, against the skill's suggestion. It is the configuration the expensive gate
# completes under, and the recorder already drops the content-less turns that disabling it is meant
# to avoid — so turning it off trades a smaller draft for the risk of a run that does not converge,
# which is the expensive half of this to get wrong.
COMPOSER_RECORD_TAPE="$TAPE_NAME" \
  exec uv run --no-sync python -m composer.testing.record_cvlr_tape "$@"
