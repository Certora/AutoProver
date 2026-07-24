#!/usr/bin/env bash
# Entrypoint for the autoprove container.
#
# Three responsibilities:
#   1. Patch /etc/passwd for the host UID compose is running us as, so
#      libraries that call pwd.getpwuid() (torch via getpass.getuser(), etc.)
#      don't crash.
#   2. One-time `setup-db` subcommand — populates rag_db and the LangGraph
#      knowledge base against the compose-managed postgres.
#   3. For console-autoprove / tui-autoprove, transparently inject --rag-db
#      pointing at the in-network postgres service.

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
CRUCIBLE_RAG_CONN="postgresql://crucible_rag_user:rag_password@${PGHOST}:${PGPORT}/rag_db"

if [[ "${1:-}" == "setup-db" ]]; then
  shift
  export PGPASSWORD=postgres_admin_password
  # Skip schema init if rag_user already exists. The compose postgres service
  # applies init-db.sql on first boot via /docker-entrypoint-initdb.d, so the
  # schema is usually already present; this also guards re-runs (init-db.sql is
  # plain CREATE USER/DATABASE, not idempotent).
  if psql -h "$PGHOST" -p "$PGPORT" -U postgres -d postgres -tAc \
      "SELECT 1 FROM pg_user WHERE usename='rag_user'" | grep -q 1; then
    echo "[autoprove] schema already initialized, skipping init-db.sql"
  else
    # composer ships init-db.sql as package-data of composer.scripts, so it's at
    # site-packages/composer/scripts/init-db.sql in this image. It contains psql
    # \c meta-commands and must go through psql.
    init_sql=$(python -c "import importlib.resources; print(importlib.resources.files('composer.scripts').joinpath('init-db.sql'))")
    echo "[autoprove] applying schema from ${init_sql} ..."
    psql -h "$PGHOST" -p "$PGPORT" -U postgres -d postgres \
        -v ON_ERROR_STOP=1 -f "$init_sql"
  fi
  echo "[autoprove] populating rag_db at ${RAG_CONN} ..."
  python -m composer.scripts.ragbuild \
      --output "$RAG_CONN" \
      "$AUTOPROVE_HOME/prover-docs/cvl.html"
  echo "[autoprove] populating crucible_kb RAG at ${CRUCIBLE_RAG_CONN} ..."
  python -m composer.scripts.rag_import \
      --output "$CRUCIBLE_RAG_CONN" \
      "$AUTOPROVE_HOME/crucible_kb.rag.json"
  echo "[autoprove] populating LangGraph knowledge base ..."
  python -m composer.scripts.kb_populate
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
  console-crucible|tui-crucible)
    # Crucible runs fully in-container, but ONLY on the crucible toolchain image
    # (scripts/Dockerfile.crucible) with the sandbox overlay mounted. Fail fast
    # with an actionable message instead of a deep build/sandbox error. The RAG
    # connection is derived from CERTORA_AI_COMPOSER_PGHOST/PGPORT (no --rag-db).
    missing=()
    for bin in crucible cargo-build-sbf anchor; do
      command -v "$bin" >/dev/null 2>&1 || missing+=("$bin")
    done
    if (( ${#missing[@]} > 0 )); then
      echo "[autoprove] Crucible toolchain missing: ${missing[*]}." >&2
      echo "[autoprove] Run the crucible image: add -f scripts/docker-compose.crucible.yml" >&2
      exit 1
    fi
    if [[ "${COMPOSER_SANDBOX_PROVIDER:-launcher}" == "launcher" ]] \
       && [[ -z "${RUN_CONFINED_BIN:-}" || ! -x "${RUN_CONFINED_BIN:-/nonexistent}" ]] \
       && ! command -v run-confined >/dev/null 2>&1; then
      echo "[autoprove] run-confined not found and the launcher provider is fail-closed." >&2
      echo "[autoprove] Add -f scripts/docker-compose.sandbox.yml, or set COMPOSER_SANDBOX_PROVIDER=none." >&2
      exit 1
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
