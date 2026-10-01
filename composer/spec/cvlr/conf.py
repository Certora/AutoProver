"""A Solana submission's prover conf: fixed settings, the ones the author may change, and one run's.

Every conf is built here, never read from the project. :data:`BASE_CONF` is fixed.
:class:`TunableConf` holds what the author may change, and :func:`tunable_conf` renders it onto
the fixed part. :func:`solana_conf` adds one submission's
keys: the build script, the message, the rule selection, and the summary files.

``solana_inlining`` is left unset. ``cargo certora-sbf`` reads it from the package's
``[package.metadata.certora]`` and reports it through the build manifest. The prover applies that
declaration only when the conf has none.

``solana_summaries`` is set when the run passes summary files. A summary is a regex over symbols
and applies to the whole build, so one package-level file would apply one unit's summaries to
every other unit. The run names a per-unit file, composed from the starting layers plus the unit's
own. Naming any value stops the prover from also applying the package's own declaration.
"""

from dataclasses import dataclass, field
from pathlib import Path

from composer.cargo.features import CargoFeature
from composer.prover.conf import Conf, InheritRules, RuleSelection, dump_conf, safe_msg
from composer.spec.util import string_hash

#: Conf keys no author or run changes.
#:
#: ``rule_sanity`` is ``basic``. With the check off, a [3308] inside the generated vacuity rule is
#: reported as verified.
#:
#: ``prover_args`` has none of the ``-solanaOptimistic*`` flags: they are unsound, and they do not
#: fix the [3308] they were meant to.
BASE_CONF: Conf = {
    "smt_timeout": "6000",
    "rule_sanity": "basic",
    "prover_args": [
        "-unsatCoresForAllAsserts true",
        "-solanaSkipCallRegInst true",
        "-solanaTACOptimize 2",
        "-solanaStackSize 8192",
        "-solanaTACMathInt true",
    ],
}


@dataclass(frozen=True)
class OptimisticLoop:
    """``optimistic_loop`` switched on, with the author's account of why.

    The account travels with the setting because the setting is unsound: every verdict in the unit
    is conditional on it, and a reviewer can weigh it only against the argument for it. The account
    is not part of the conf, so rewording it does not invalidate a stamp.
    """

    why: str


@dataclass(frozen=True)
class TunableConf:
    """The conf settings the author may change. The defaults are where every unit starts."""

    #: 2, not 1. With a bound of 1, a loop inside a handler fails before the rule's property is
    #: reached: an Anchor handler comes back violated on "Unwinding condition in a loop" against a
    #: loop in its borsh path.
    loop_iter: int = 2
    #: Off. The author treats it as a last resort, preferring to bound the inputs that set the trip
    #: count first, then summarize or munge the code that holds the loop, then raise ``loop_iter``,
    #: and finally turn it on only for a trip count that no bound discharges. Once it is on, every
    #: rule in the submission is verified under that assumption.
    optimistic_loop: OptimisticLoop | None = None


def tunable_conf(tunable: TunableConf) -> Conf:
    """The conf ``tunable`` describes, before any one submission's keys are added."""
    return {
        **BASE_CONF,
        "loop_iter": str(tunable.loop_iter),
        "optimistic_loop": tunable.optimistic_loop is not None,
    }


def conf_history(tunable: TunableConf) -> tuple[str, ...]:
    """The conf as a ``version_history`` token, so a stamp from before a change goes stale.

    A conf decides the loop bound, the solver flags, and whether vacuity is checked. A verdict
    under one conf does not apply to another. The token hashes the rendered conf rather than the
    ``tunable``, so a change to the fixed part invalidates a stamp too.
    """
    return (f"conf:{string_hash(dump_conf(tunable_conf(tunable)))}",)


#: The platform-tools release every build uses. The prover does not apply ``cargo_tools_version``
#: itself: it reaches ``cargo certora-sbf`` only on the CLI's own build path, and this backend owns
#: the build. One release serves every target because the reference set pins one platform
#: generation (:class:`~composer.spec.cvlr.reference.PlatformGeneration`).
PLATFORM_TOOLS_VERSION = "v1.43"


#: The cargo feature that compiles the verification module into the program. The scaffold writes
#: this name (``certora = ["no-entrypoint", "dep:cvlr", …]``), and preflight passes it to
#: ``cargo check``.
DEFAULT_FEATURE = CargoFeature("certora")


@dataclass(frozen=True)
class CheckVerdicts:
    """An ordinary submission: every rule's verdict, with vacuity checked."""


@dataclass(frozen=True)
class CollectUnsatCore:
    """A diagnostic submission for a rule reported vacuous: the vacuity check off, unsat cores on.

    With ``rule_sanity`` off, a vacuous rule verifies, and ``coverage_info`` makes the prover write
    the core that proof rests on (``Reports/UnsatCoreTAC-<rule>-<n>.txt``). ``basic`` rather than the
    ``advanced`` CVL's sanity reruns use: on a Solana rule ``basic`` took the same 25s as the
    ordinary run, and ``advanced`` had not finished after 25 minutes.
    """


type ConfPurpose = CheckVerdicts | CollectUnsatCore


@dataclass(frozen=True)
class RunOverlay:
    """What one submission adds to the conf its :class:`TunableConf` describes.

    ``build_script`` is absolute and outside the session's workdir: the prover
    runs it unconfined, so no build may be able to rewrite it.
    """

    build_script: Path
    rules: RuleSelection = field(default_factory=InheritRules)
    msg: str = ""
    #: Points-to summary files, relative to the workdir ``certoraSolanaProver`` runs in.
    #: Empty leaves the key unset, so the package's ``[package.metadata.certora]`` declaration
    #: still applies.
    summaries: tuple[Path, ...] = ()
    purpose: ConfPurpose = CheckVerdicts()


def solana_conf(tunable: TunableConf, run: RunOverlay) -> Conf:
    """The conf for one ``certoraSolanaProver`` submission."""
    conf = {
        **tunable_conf(tunable),
        "build_script": str(run.build_script),
        "msg": safe_msg(run.msg),
    }
    if run.summaries:
        conf["solana_summaries"] = [str(s) for s in run.summaries]
    match run.purpose:
        case CheckVerdicts():
            pass
        case CollectUnsatCore():
            conf["rule_sanity"] = "none"
            conf["coverage_info"] = "basic"
    return run.rules.apply_to(conf)
