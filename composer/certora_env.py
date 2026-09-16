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
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, cast

if TYPE_CHECKING:
    from certoraRun import CertoraRunResult


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
#: step — the Solana one compiles a Rust project where the EVM one compiles Solidity — so the app
#: is a choice of entry point, not an argument to one.
type ProverApp = Literal["evm", "solana", "soroban"]

#: What every Prover CLI is: the argument list in, a submitted run out, or ``None`` when the run
#: never reached submission. Shared across the apps, which is what lets everything downstream of
#: submission — the wrapper, cloud polling, result parsing — stay app-agnostic.
type ProverEntry = Callable[[list[str]], CertoraRunResult | None]


@dataclass(frozen=True)
class _EntryPoint:
    """Where an app's run function lives. The module is spelled once, relative to the package, and
    ``$CERTORA`` decides whether it is imported from the checkout or from the installed package."""

    module: str
    function: str


_PROVER_ENTRIES: dict[ProverApp, _EntryPoint] = {
    "evm": _EntryPoint("certoraRun", "run_certora"),
    "solana": _EntryPoint("certoraSolanaProver", "run_solana_prover"),
    "soroban": _EntryPoint("certoraSorobanProver", "run_soroban_prover"),
}


def prover_app(name: str) -> ProverApp:
    """Narrow an untrusted string to a :data:`ProverApp`.

    For the subprocess wrapper, which reads the app out of its own ``argv``. Keeping the parse
    here rather than inside :func:`import_prover_entry` leaves every in-process caller a checked
    literal.
    """
    for app in _PROVER_ENTRIES:
        if app == name:
            return app
    raise CertoraEnvironmentError(
        f"unknown prover app {name!r}; known: {sorted(_PROVER_ENTRIES)}"
    )


def import_prover_entry(app: ProverApp) -> ProverEntry:
    """The run function for ``app``, honoring ``$CERTORA``.

    When ``$CERTORA`` is set we run against a source checkout (added to ``sys.path``); otherwise we
    use the pip-installed ``certora_cli`` package. Used by the sandboxed subprocess wrappers.

    The cast is the unchecked step: the entry is resolved by name out of a dynamically imported
    module, so nothing but :data:`_PROVER_ENTRIES` says it is a :data:`ProverEntry`.
    """
    entry = _PROVER_ENTRIES[app]
    home = certora_home()
    if home is None:
        imported = importlib.import_module(f"certora_cli.{entry.module}")
    else:
        sys.path.append(str(home))
        imported = importlib.import_module(entry.module)
    return cast(ProverEntry, getattr(imported, entry.function))


def import_run_certora() -> ProverEntry:
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
