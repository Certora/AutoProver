#!/usr/bin/env bash
# Entrypoint for the autoprove container.
#
# Responsibilities:
#   1. Patch /etc/passwd for the host UID compose is running us as, so
#      libraries that call pwd.getpwuid() (torch via getpass.getuser(), etc.)
#      don't crash.
#   2. `setup-db` subcommand — applies the schema (checkpoints, store, memory)
#      to a postgres that was not initialized from init-db.sql. The compose
#      postgres applies it on first boot, so it needs no setup-db. The RAG
#      corpora are not in postgres: they are built into the image, under
#      $CERTORA_AI_COMPOSER_RAG_DIR, and every entry point opens them by default.
#   3. For console-foundry / tui-foundry, enable the hardened forge's guards.

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

if [[ "${1:-}" == "setup-db" ]]; then
  shift
  PGHOST="${CERTORA_AI_COMPOSER_PGHOST:-postgres}"
  PGPORT="${CERTORA_AI_COMPOSER_PGPORT:-5432}"
  export PGPASSWORD=postgres_admin_password
  # Always applied: every statement in init-db.sql is guarded (IF NOT EXISTS / \gexec), so a
  # re-run changes nothing that is there and creates what a cluster provisioned by an older
  # init-db.sql is missing.
  #
  # composer ships init-db.sql as package-data of composer.scripts, so it's at
  # site-packages/composer/scripts/init-db.sql in this image. It contains psql
  # \c meta-commands and must go through psql.
  init_sql=$(python -c "import importlib.resources; print(importlib.resources.files('composer.scripts').joinpath('init-db.sql'))")
  echo "[autoprove] applying schema from ${init_sql} ..."
  psql -h "$PGHOST" -p "$PGPORT" -U postgres -d postgres \
      -v ON_ERROR_STOP=1 -f "$init_sql"
  echo "[autoprove] setup-db done."
  exit 0
fi

case "${1:-}" in
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
