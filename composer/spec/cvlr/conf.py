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

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from composer.cargo.features import CargoFeature
from composer.prover.conf import Conf, InheritRules, RuleSelection, dump_conf, safe_msg

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
    optimistic_loop: bool = False


def tunable_conf(tunable: TunableConf) -> Conf:
    """The conf ``tunable`` describes, before any one submission's keys are added."""
    return {
        **BASE_CONF,
        "loop_iter": str(tunable.loop_iter),
        "optimistic_loop": tunable.optimistic_loop,
    }


def conf_history(tunable: TunableConf) -> tuple[str, ...]:
    """The conf as a ``version_history`` token, so a stamp from before a change goes stale.

    A conf decides the loop bound, the solver flags, and whether vacuity is checked. A verdict
    under one conf does not apply to another. The token hashes the rendered conf rather than the
    ``tunable``, so a change to the fixed part invalidates a stamp too.
    """
    digest = hashlib.sha256(dump_conf(tunable_conf(tunable)).encode()).hexdigest()[:16]
    return (f"conf:{digest}",)


#: The platform-tools release every build uses. The prover does not apply ``cargo_tools_version``
#: itself: it reaches ``cargo certora-sbf`` only on the CLI's own build path, and this backend owns
#: the build. One release serves every target because the reference set pins one platform
#: generation (:class:`~composer.spec.cvlr_reference.PlatformGeneration`).
PLATFORM_TOOLS_VERSION = "v1.43"


#: The cargo feature that compiles the verification module into the program. The scaffold writes
#: this name (``certora = ["no-entrypoint", "dep:cvlr", …]``), and preflight passes it to
#: ``cargo check``.
DEFAULT_FEATURE = CargoFeature("certora")


@dataclass(frozen=True)
class RunOverlay:
    """What one submission adds to the conf its :class:`TunableConf` describes.

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


def solana_conf(tunable: TunableConf, run: RunOverlay) -> Conf:
    """The conf for one ``certoraSolanaProver`` submission."""
    conf = {
        **tunable_conf(tunable),
        "build_script": str(run.build_script),
        "msg": safe_msg(run.msg),
    }
    if run.summaries:
        conf["solana_summaries"] = [str(s) for s in run.summaries]
    return run.rules.apply_to(conf)
