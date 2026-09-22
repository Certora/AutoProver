"""A Solana submission's prover conf: fixed settings, the ones the author may change, and one run's.

Every conf is built here, never read from the project. :data:`BASE_CONF` and
:data:`BASE_PROVER_ARGS` are fixed. :class:`ProverSettings` holds what the author may change, and
:func:`settings_conf` renders it onto the fixed part. :func:`solana_conf` adds one submission's
keys: the build script, the message, the rule selection, and the summary files.

``solana_inlining`` is left unset. ``cargo certora-sbf`` reads it from the package's
``[package.metadata.certora]`` and reports it through the build manifest. The prover applies that
declaration only when the conf has none.

``solana_summaries`` is set when the run passes summary files. A summary is a regex over symbols
and applies to the whole build, so one package-level file would apply one unit's summaries to
every other unit. The run names a per-unit file, composed from the starting layers plus the unit's
own. Naming any value stops the prover from also applying the package's own declaration.
"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from composer.prover.conf import Conf, InheritRules, RuleSelection, dump_conf, safe_msg, with_rules

#: Prover flags every conf carries. None of the ``-solanaOptimistic*`` flags: they are unsound, and
#: they do not fix the [3308] they were meant to.
BASE_PROVER_ARGS: tuple[str, ...] = (
    "-unsatCoresForAllAsserts true",
    "-solanaSkipCallRegInst true",
    "-solanaTACOptimize 2",
    "-solanaStackSize 8192",
    "-solanaTACMathInt true",
)

#: Conf keys no author or run changes.
#:
#: ``rule_sanity`` is ``basic``. With the check off, a [3308] inside the generated vacuity rule is
#: reported as verified.
BASE_CONF: Conf = {
    "java_args": ["-Dlevel.sbf=info"],
    "smt_timeout": "6000",
    "rule_sanity": "basic",
}


#: Solver settings for a nonlinear-arithmetic query, as one recipe.
#:
#: Taken from the reference project's conf, where it serves rules that split heavily and halt on
#: the global timeout.
#:
#: One recipe, not a list a caller edits flag by flag. ``-solanaTACSoundSignedMath`` next to
#: ``-solanaTACMathInt`` (which :data:`BASE_PROVER_ARGS` already sets) turned a seven-minute,
#: eighteen-rule run into a two-hour timeout with thirteen rules unverified. The expansion is
#: fixed so that combination is not assembled by hand.
#:
#: Every flag here must leave the meaning of a verified result unchanged. A flag that changes it
#: does not belong in the recipe.
NONLINEAR_SOLVER_PORTFOLIO: tuple[str, ...] = (
    "-backendStrategy adaptive",
    "-smt_useLIA true",
    "-smt_useNIA true",
    "-solvers [z3:def{randomSeed=21},z3:def{randomSeed=22},z3:def{randomSeed=23}]",
    "-solvers [z3:def{randomSeed=24},z3:def{randomSeed=25},z3:def{randomSeed=26}]",
    "-solvers [z3:def{randomSeed=27},z3:def{randomSeed=28},z3:def{randomSeed=29}]",
    "-solvers [z3:def{randomSeed=30},z3:def{randomSeed=31},z3:def{randomSeed=32}]",
)


@dataclass(frozen=True)
class ProverSettings:
    """The conf settings the author may change. The defaults are where every unit starts."""

    #: 2, not 1. With a bound of 1, a loop inside a handler fails before the rule's property is
    #: reached: an Anchor handler comes back violated on "Unwinding condition in a loop" against a
    #: loop in its borsh path.
    loop_iter: int = 2
    #: Off. The author treats it as a last resort, preferring to bound the inputs that set the trip
    #: count first, then summarize or munge the code that holds the loop, then raise ``loop_iter``,
    #: and finally turn it on only for a trip count that no bound discharges. Once it is on, every
    #: rule in the submission is verified under that assumption.
    optimistic_loop: bool = False
    #: Whether the conf carries :data:`NONLINEAR_SOLVER_PORTFOLIO`.
    solver_portfolio: bool = False


def settings_conf(settings: ProverSettings) -> Conf:
    """The conf ``settings`` describe, before any one submission's keys are added."""
    portfolio = NONLINEAR_SOLVER_PORTFOLIO if settings.solver_portfolio else ()
    return {
        **BASE_CONF,
        "prover_args": [*BASE_PROVER_ARGS, *portfolio],
        "loop_iter": str(settings.loop_iter),
        "optimistic_loop": settings.optimistic_loop,
    }


def conf_history(settings: ProverSettings) -> tuple[str, ...]:
    """The conf as a ``version_history`` token, so a stamp from before a change goes stale.

    A conf decides the loop bound, the solver flags, and whether vacuity is checked. A verdict
    under one conf does not apply to another. The token hashes the rendered conf rather than the
    settings, so a change to the fixed part invalidates a stamp too.
    """
    digest = hashlib.sha256(dump_conf(settings_conf(settings)).encode()).hexdigest()[:16]
    return (f"conf:{digest}",)


#: The platform-tools release every build uses. The prover does not apply ``cargo_tools_version``
#: itself: it reaches ``cargo certora-sbf`` only on the CLI's own build path, and this backend owns
#: the build. One release serves every target because the reference set pins one platform
#: generation (:class:`~composer.spec.cvlr_reference.PlatformGeneration`).
PLATFORM_TOOLS_VERSION = "v1.43"


#: The cargo feature that compiles the verification module into the program. The scaffold writes
#: this name (``certora = ["no-entrypoint", "dep:cvlr", …]``), and preflight passes it to
#: ``cargo check``.
DEFAULT_FEATURE = "certora"


@dataclass(frozen=True)
class RunOverlay:
    """What one submission adds to the conf its settings describe.

    ``build_script`` is a path as the prover reads it: relative to the directory
    ``certoraSolanaProver`` runs in, which is the session's workdir.
    """

    build_script: Path
    rules: RuleSelection = field(default_factory=InheritRules)
    msg: str = ""
    #: Points-to summary files, in the same relative-to-the-workdir spelling as ``build_script``.
    #: Empty leaves the key unset, so the package's ``[package.metadata.certora]`` declaration
    #: still applies.
    summaries: tuple[Path, ...] = ()


def solana_conf(settings: ProverSettings, run: RunOverlay) -> Conf:
    """The conf for one ``certoraSolanaProver`` submission."""
    conf = {
        **settings_conf(settings),
        "build_script": str(run.build_script),
        "msg": safe_msg(run.msg),
    }
    if run.summaries:
        conf["solana_summaries"] = [str(s) for s in run.summaries]
    return with_rules(conf, run.rules)
