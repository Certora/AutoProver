#!/usr/bin/env bash
# Entrypoint for the autoprove container.
#
# Four responsibilities:
#   1. Patch /etc/passwd for the host UID compose is running us as, so
#      libraries that call pwd.getpwuid() (torch via getpass.getuser(), etc.)
#      don't crash.
#   2. One-time `setup-db` subcommand — populates rag_db and the LangGraph
#      knowledge base against the compose-managed postgres.
#   3. For console-autoprove / tui-autoprove, transparently inject --rag-db
#      pointing at the in-network postgres service.
#   4. For console-solana / tui-solana, refuse to start when this container was
#      not given what a confined cargo build needs — both are things only the
#      operator can fix, and both otherwise surface much later.

set -euo pipefail

: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY must be set in the container env}"
: "${AUTOPROVE_HOME:?AUTOPROVE_HOME not set (image misconfigured)}"

# Synthetic passwd/group entry for the host UID compose runs us as.
_uid=$(id -u)
_gid=$(id -g)
if ! getent passwd "$_uid" >/dev/null 2>&1; then
  echo "autoprove:x:${_uid}:${_gid}:autoprove:${HOME}:/bin/bash" >> /etc/passwd
fi
if ! getent group "$_gid" >/dev/null 2>&1; then
  echo "autoprove:x:${_gid}:" >> /etc/group
fi
export USER=autoprove LOGNAME=autoprove

PGHOST="${CERTORA_AI_COMPOSER_PGHOST:-postgres}"
PGPORT="${CERTORA_AI_COMPOSER_PGPORT:-5432}"
RAG_CONN="postgresql://rag_user:rag_password@${PGHOST}:${PGPORT}/rag_db"

if [[ "${1:-}" == "setup-db" ]]; then
  shift
  export PGPASSWORD=postgres_admin_password
  # Always apply init-db.sql, even against an already-initialized cluster. Every statement in it
  # is guarded (IF NOT EXISTS / \gexec), so re-running is a no-op for what is already there and
  # creates what is not — which is the case that matters: a cluster provisioned before a new
  # corpus was added has the old roles and none of the new schema. Skipping on "rag_user exists"
  # would leave that cluster permanently missing the newer schemas, and the failure would surface
  # later as an ingest error rather than here.
  #
  # composer ships init-db.sql as package-data of composer.scripts, so it's at
  # site-packages/composer/scripts/init-db.sql in this image. It contains psql
  # \c meta-commands and must go through psql.
  init_sql=$(python -c "import importlib.resources; print(importlib.resources.files('composer.scripts').joinpath('init-db.sql'))")
  echo "[autoprove] applying schema from ${init_sql} ..."
  psql -h "$PGHOST" -p "$PGPORT" -U postgres -d postgres \
      -v ON_ERROR_STOP=1 -f "$init_sql"
  echo "[autoprove] populating rag_db at ${RAG_CONN} ..."
  python -m composer.scripts.ragbuild \
      --output "$RAG_CONN" \
      "$AUTOPROVE_HOME/prover-docs/cvl.html"
  echo "[autoprove] populating LangGraph knowledge base ..."
  python -m composer.scripts.kb_populate

  # The `cvlr_kb` corpus. Its methodology half is the Solana manual built into this image beside the
  # CVL one, ingested by the same producer; its surface and practice halves are two manifests that
  # share the tag, with the importer numbering their sections apart (docs/cvlr-capture-plan.md
  # §8.2). Those two ship in the separate certora-cvlr-kb package, so they are here only if it is
  # installed or CVLR_KB_REPO points at a checkout.
  #
  # An install with neither half is supported (the backend falls back to its static guidance), so
  # finding nothing reports a skip rather than failing setup-db.
  echo "[autoprove] populating cvlr_kb from the Solana manual ..."
  cvlr_conn="$(python -c 'from composer.rag.db import CVLR_DEFAULT_CONNECTION as c; print(c)')"
  python -m composer.scripts.ragbuild \
      --output "$cvlr_conn" \
      "$AUTOPROVE_HOME/prover-docs/solana.html"

  cvlr_manifests=()
  if [[ -n "${CVLR_KB_REPO:-}" && -d "${CVLR_KB_REPO%/}/src/certora_cvlr_kb/data" ]]; then
    shopt -s nullglob
    cvlr_manifests+=("${CVLR_KB_REPO%/}"/src/certora_cvlr_kb/data/*.rag.json)
    shopt -u nullglob
  fi
  # The installed package, consulted only when a checkout did not supply them, so a checkout you are
  # actively editing always wins over an older installed copy.
  if [[ ${#cvlr_manifests[@]} -eq 0 ]]; then
    pkg_probe='
try:
    from certora_cvlr_kb import manifests
    print("\n".join(str(p) for p in manifests()))
except Exception:
    pass
'
    while IFS= read -r line; do
      [[ -n "$line" ]] && cvlr_manifests+=("$line")
    done < <(python -c "$pkg_probe" 2>/dev/null || true)
  fi
  if [[ ${#cvlr_manifests[@]} -gt 0 ]]; then
    echo "[autoprove] populating cvlr_kb from ${#cvlr_manifests[@]} manifest(s) ..."
    python -m composer.scripts.rag_import "${cvlr_manifests[@]}"
  else
    echo "[autoprove] no cvlr_kb manifest found; the corpus has the manual but no crate reference"
  fi

  echo "[autoprove] setup-db done."
  exit 0
fi

# For the prove entry points, inject --rag-db if the user didn't supply one.
case "${1:-}" in
  console-autoprove|tui-autoprove)
    cmd="$1"; shift
    has_rag_db=0
    for arg in "$@"; do
      if [[ "$arg" == "--rag-db" || "$arg" == --rag-db=* ]]; then
        has_rag_db=1
        break
      fi
    done
    if (( has_rag_db == 0 )); then
      set -- "$@" --rag-db "$RAG_CONN"
    fi
    exec "$cmd" "$@"
    ;;
  console-solana|tui-solana)
    # Both front ends live in the Solana image (scripts/Dockerfile.solana), which carries the Rust
    # toolchain and is run with the sandbox overlay. Each check below is something only the operator
    # can fix, and each otherwise surfaces much later: the missing launcher as a fail-closed
    # provider error several services in, the missing toolchain as a compile failure mid-run.
    if [[ "${COMPOSER_SANDBOX_PROVIDER:-launcher}" != "none" && ! -x "${RUN_CONFINED_BIN:-}" ]]; then
      echo "[autoprove] $1 confines its builds, and no run-confined launcher is mounted." >&2
      echo "[autoprove] Add the sandbox overlay to the compose invocation:" >&2
      echo "[autoprove]   docker compose -f scripts/docker-compose.yml \\" >&2
      echo "[autoprove]       -f scripts/docker-compose.sandbox.yml \\" >&2
      echo "[autoprove]       -f scripts/docker-compose.solana.yml \\" >&2
      echo "[autoprove]       --profile solana --profile sandbox run --rm \\" >&2
      echo "[autoprove]       autoprove-solana $1 ..." >&2
      echo "[autoprove] Or set COMPOSER_SANDBOX_PROVIDER=none to build this project unconfined —" >&2
      echo "[autoprove] development only: the results are not production results." >&2
      exit 2
    fi
    # A confined build has no network, so a missing toolchain cannot be repaired at run time.
    if ! command -v cargo >/dev/null 2>&1; then
      echo "[autoprove] $1 needs a Rust toolchain, which this image does not carry." >&2
      echo "[autoprove] This is the base AutoProver image; $1 runs in the Solana one." >&2
      echo "[autoprove] Add -f scripts/docker-compose.solana.yml and run autoprove-solana." >&2
      exit 2
    fi
    exec "$@"
    ;;
  console-foundry|tui-foundry)
    # Foundry mode runs the project's own `forge test`, which can use the `ffi`
    # cheatcode and external cheatcodes. Enable the hardened fork's guards so
    # untrusted project tests can't shell out via FFI or reach external
    # cheatcodes. These guards only affect test/script execution; the autoprove
    # path calls forge only for `forge remappings` (a static config query that
    # runs no cheatcodes/FFI), so it needs no guard either way.
    export FOUNDRY_DISABLE_EXTERNAL_CHEATCODES=true FOUNDRY_FFI=false
    exec "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
