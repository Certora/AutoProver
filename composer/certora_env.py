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

import importlib
import os
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable, Literal, cast


class CertoraEnvironmentError(Exception):
    """The local Certora toolchain could not be resolved.

    Raised when a required Certora artifact (e.g. ``Typechecker.jar``) can't be
    located because ``$CERTORA`` points somewhere wrong or the pip-installed
    packages are missing. Callers should treat this as an environment fault to
    surface to the operator, NOT as a spec/input error to retry.
    """


def certora_home() -> Path | None:
    """The Certora source checkout pointed to by ``$CERTORA``.

    Returns ``None`` when ``$CERTORA`` is unset (i.e. we run against the
    pip-installed ``certora_cli`` / ``certora_jars`` packages).
    """
    path = os.environ.get("CERTORA")
    return Path(path) if path else None


#: Which Prover a run submits to. Each is a separate CLI in ``certora_cli`` with its own build
#: step — the Solana one compiles a Rust project where the EVM one compiles Solidity — so they are
#: distinct entry points rather than one function with an app argument, and this is the name that
#: selects between them.
type ProverApp = Literal["evm", "solana", "soroban"]

#: ``ProverApp`` -> (module, run function). The module is spelled once, relative to the package, and
#: ``$CERTORA`` decides whether it is imported from the checkout or from the installed package.
_PROVER_ENTRIES: dict[str, tuple[str, str]] = {
    "evm": ("certoraRun", "run_certora"),
    "solana": ("certoraSolanaProver", "run_solana_prover"),
    "soroban": ("certoraSorobanProver", "run_soroban_prover"),
}


def prover_app(name: str) -> ProverApp:
    """Narrow an untrusted string to a :data:`ProverApp`.

    One caller: the subprocess wrapper, reading the app out of its own ``argv``. The parse belongs
    at that boundary rather than inside :func:`import_prover_entry`, so that every in-process caller
    keeps a checked literal and only the process boundary pays for a runtime check."""
    if name not in _PROVER_ENTRIES:
        raise CertoraEnvironmentError(
            f"unknown prover app {name!r}; known: {sorted(_PROVER_ENTRIES)}"
        )
    return cast(ProverApp, name)


def import_prover_entry(app: ProverApp) -> Callable[[list[str]], Any]:
    """The run function for ``app``, honoring ``$CERTORA``.

    When ``$CERTORA`` is set we run against a source checkout (added to ``sys.path``); otherwise we
    use the pip-installed ``certora_cli`` package. Used by the sandboxed subprocess wrappers.

    Every entry takes the CLI argument list and returns that CLI's ``CertoraRunResult | None``, so
    the wrapper around them is app-agnostic — which is the only reason a Solana submission can reuse
    the EVM backend's cloud polling and result parsing unchanged.
    """
    module, function = _PROVER_ENTRIES[app]
    home = certora_home()
    if home is None:
        imported = importlib.import_module(f"certora_cli.{module}")
    else:
        sys.path.append(str(home))
        imported = importlib.import_module(module)
    return getattr(imported, function)


def import_run_certora():
    """Import and return the EVM ``run_certora``. See :func:`import_prover_entry`."""
    return import_prover_entry("evm")


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
