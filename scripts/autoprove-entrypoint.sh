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
  *)
    exec "$@"
    ;;
esac
