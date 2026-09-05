"""Centralized resolution of the local Certora installation.

Three call sites locate the Certora toolchain through this module: the two
sandboxed subprocess wrappers (``certoraRunWrapper.py``, ``certoraTypeCheck.py``)
that import ``run_certora``, and the in-process CVL syntax checker
(``composer.cvl.tools``) that needs the ``Typechecker.jar``. Routing them all
through one policy keeps resolution consistent and ensures a missing jar or
misconfigured ``$CERTORA`` surfaces as a clear error rather than an opaque
failure downstream.

Policy: if ``$CERTORA`` is set, run against that source checkout; otherwise fall
back to the pip-installed ``certora_cli`` / ``certora_jars`` packages.
"""

import os
import sys
from importlib.resources import files
from pathlib import Path


class CertoraEnvironmentError(Exception):
    """The local Certora toolchain could not be resolved.

    Raised when a required Certora artifact (e.g. ``Typechecker.jar``) can't be
    located because ``$CERTORA`` points somewhere wrong or the pip-installed
    packages are missing. Callers should treat this as an environment fault to
    surface to the operator, NOT as a spec/input error to retry.
    """


# Overrides ``$CERTORA`` for AutoProver's own Certora resolution, set by
# console-autoprove's ``--certora-run-command``. The sentinel ``"pip"`` forces
# the pip-installed CLI. This lets a session keep ``$CERTORA`` pointed at a local
# build (for direct local-prover work) while AutoProver dispatches its cloud jobs
# through the pip ``certora_cli``: a source checkout is not a pip distribution, so
# ``get_package_and_version`` reports "not installed" and a cloud submission
# aborts demanding ``prover_version``/``commit_sha1`` — whereas the pip package
# carries the version that drives the submission.
CERTORA_HOME_OVERRIDE_ENV = "AUTOPROVER_CERTORA_HOME"
_FORCE_PIP = "pip"


def certora_home() -> Path | None:
    """The Certora source checkout to resolve against, or ``None`` to use the
    pip-installed ``certora_cli`` / ``certora_jars`` packages.

    ``$AUTOPROVER_CERTORA_HOME`` wins over ``$CERTORA`` when set: a filesystem
    path selects that source checkout; the sentinel ``"pip"`` forces the pip
    packages (returns ``None``). With the override unset, falls back to
    ``$CERTORA`` (a checkout), else the pip packages.
    """
    override = os.environ.get(CERTORA_HOME_OVERRIDE_ENV)
    if override is not None:
        if override.strip().lower() == _FORCE_PIP or not override.strip():
            return None
        return Path(override)
    path = os.environ.get("CERTORA")
    return Path(path) if path else None


def import_run_certora():
    """Import and return ``run_certora``, honoring ``$CERTORA``.

    When ``$CERTORA`` is set we run against a source checkout (added to
    ``sys.path``); otherwise we use the pip-installed ``certora_cli`` package.
    Used by the sandboxed subprocess wrappers.
    """
    home = certora_home()
    if home is None:
        from certora_cli.certoraRun import run_certora
    else:
        sys.path.append(str(home))
        from certoraRun import run_certora
    return run_certora


def typechecker_jar() -> Path:
    """Locate the CVL ``Typechecker.jar``, honoring ``$CERTORA``.

    Raises ``CertoraEnvironmentError`` with an actionable message if the jar
    cannot be located, so callers can surface an environment problem instead of
    mistaking it for a spec error.
    """
    home = certora_home()
    if home is not None:
        jar = home / "certora_jars" / "Typechecker.jar"
        if not jar.is_file():
            raise CertoraEnvironmentError(
                f"$CERTORA is set to {home} but {jar} does not exist"
            )
        return jar

    # No $CERTORA: use the jar shipped with the certora_jars package.
    try:
        base = files("certora_jars")
    except ModuleNotFoundError as exc:
        raise CertoraEnvironmentError(
            "certora_jars package is not importable and $CERTORA is unset; "
            "cannot locate Typechecker.jar"
        ) from exc
    jar = Path(str(base / "Typechecker.jar"))
    if not jar.is_file():
        raise CertoraEnvironmentError(
            f"certora_jars resolved to {jar} but the jar is missing"
        )
    return jar
