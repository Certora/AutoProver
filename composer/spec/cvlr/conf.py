"""Choose a Solana project's prover conf and layer one run's settings onto it.

Reading, writing, and layering are :mod:`composer.prover.conf`. This module is the Solana policy
on top: which conf is the base, what a run owns, and the settings a run may escalate to.

The project's conf is the base, as in
:func:`composer.spec.source.prover.prover_config_overlay`. The run owns the keys in
:data:`OVERLAY_OWNED_KEYS`. ``build_script`` comes from the overlay, so a base conf's own script
does not win. ``files`` is dropped: the prover refuses a build script that sets files when the
conf already names some, and a prebuilt ``.so`` cannot sit next to a from-sources build.
``server`` is owned because the deployment decides which cloud a run reaches.

:func:`project_conf` chooses the base. Everything else in the project's conf is kept: the loop
bound, the solver flags, ``prover_version``. :func:`with_sanity_floor` is the exception. It turns
vacuity checking on when the base never mentions ``rule_sanity``.

``solana_inlining`` is left unset. ``cargo certora-sbf`` reads it from the package's
``[package.metadata.certora]`` and reports it through the build manifest. The prover applies that
declaration only when the conf has none. Setting the key here would replace the project's file.

``solana_summaries`` is set when the run passes summary files. A summary is a regex over symbols
and applies to the whole build, so one package-level file would apply one unit's summaries to
every other unit. The run names a per-unit file, composed from the canonical layers plus the
project's ``_package`` layer. Anything the base conf already named is kept beside it. Naming any
value stops the prover from also applying the package's own declaration.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from composer.prover.conf import (
    Conf, InheritRules, RuleSelection, dump_conf, flag_name, merge_prover_args, overlay,
    read_conf, safe_msg, str_list,
)

_log = logging.getLogger(__name__)

#: Conf keys the run always decides, whatever the base says. ``files`` is in the set because it
#: is removed. The prover refuses a build script that sets files when the conf already names
#: some, so a base conf naming a prebuilt ``.so`` and a run building from sources cannot both
#: be kept.
#:
#: ``rule`` is not here. :data:`RuleSelection` has several answers, and inheriting the base's
#: selection is one of them.
#:
#: ``server`` is owned because the deployment decides which cloud a run reaches and passes it on
#: the CLI (:func:`composer.prover.core.make_prover_options`). Project confs that name a server
#: say ``"production"``. Dropping the key keeps a single answer.
OVERLAY_OWNED_KEYS: frozenset[str] = frozenset({"build_script", "files", "msg", "server"})

#: The base conf for a project that has none of :data:`PROJECT_CONF_NAMES`.
#:
#: ``optimistic_loop`` is false.  The author agent treats optimistic_loop as a last resort, preferring 
#: to bound the inputs that set the trip count first, then summarize or munge the code that holds the 
#: loop, then raise ``loop_iter``, and finally turn it on only for a trip count that no bound discharges. 
#: Once it is on, every rule in the submission is verified under that assumption.
TEMPLATE_BASE: Conf = {
    "msg": "Certora Verification Rules",
    "loop_iter": "2",
    "optimistic_loop": False,
    "java_args": ["-Dlevel.sbf=info"],
    "prover_args": [
        "-unsatCoresForAllAsserts true",
        "-solanaSkipCallRegInst true",
        "-solanaTACOptimize 2",
        "-solanaStackSize 8192",
        "-solanaTACMathInt true",
    ],
    "smt_timeout": "6000",
    "cargo_tools_version": "v1.43",
    "rule_sanity": "basic",
}


#: The project's base conf, in the order a run prefers them, inside the harness's ``confs/``
#: directory.
#:
#: ``base.conf`` is first. In a project that has one, the other confs reach it through
#: ``override_base_config``, so it carries the project's settings and nothing else. ``run.conf``
#: is the recommended starting point's single conf.
#:
#: A closed list. The other files in that directory are per-rule-set confs, and each carries a
#: ``rule`` list. :class:`InheritRules` would adopt it and check somebody else's selection.
#: A project whose base has another name gets :data:`TEMPLATE_BASE` and a log line.
PROJECT_CONF_NAMES: tuple[str, ...] = ("base.conf", "run.conf")


def project_conf(confs_dir: Path) -> Path | None:
    """The project's base conf, or ``None`` when neither known name is present."""
    return next(
        (c for name in PROJECT_CONF_NAMES if (c := confs_dir / name).is_file()),
        None,
    )


def conf_history(conf: Conf) -> tuple[str, ...]:
    """The conf as a ``version_history`` token, so a stamp from before a change goes stale.

    A conf decides the loop bound, the solver flags, and whether vacuity is checked. A verdict
    under one conf does not apply to another. The token is a hash of the whole conf. A list of
    known keys would miss a key this function had not heard of.
    """
    digest = hashlib.sha256(dump_conf(conf).encode()).hexdigest()[:16]
    return (f"conf:{digest}",)


def load_base(path: Path | None) -> Conf:
    """The base conf for a run: the project's, or :data:`TEMPLATE_BASE`.

    The fallback is :data:`TEMPLATE_BASE`, not an empty conf. An empty conf has no loop bound, no
    SMT timeout, and no prover flags, and that verifies differently. Logged either way, so a run
    that is not using the project's settings says so.
    """
    if path is None:
        _log.info("cvlr: no project conf found; using the default settings")
        return dict(TEMPLATE_BASE)
    _log.info("cvlr: prover conf from %s", path)
    return read_conf(path)


def tools_version(conf: Conf) -> str | None:
    """The platform-tools version this conf asks for.

    The prover does not apply ``cargo_tools_version`` itself. It reaches ``cargo certora-sbf``
    only on the CLI's own build path, and this backend owns the build. Reading it here is what
    keeps the project's declaration in effect."""
    raw = conf.get("cargo_tools_version")
    return str(raw) if isinstance(raw, (str, int)) else None


def sbf_arch(conf: Conf) -> str | None:
    raw = conf.get("solana_sbf_arch")
    return str(raw) if isinstance(raw, str) else None


#: The cargo feature that compiles the verification module into the program. The scaffold writes
#: this name (``certora = ["no-entrypoint", "dep:cvlr", …]``), and preflight passes it to
#: ``cargo check``. A project can call the feature something else; :func:`cargo_features` is
#: where a conf would say so.
DEFAULT_FEATURE = "certora"


def cargo_features(conf: Conf) -> tuple[str, ...]:
    return tuple(str_list(conf.get("cargo_features")))


@dataclass(frozen=True)
class RunOverlay:
    """What one submission adds to the base conf.

    ``build_script`` is a path as the prover reads it: relative to the directory
    ``certoraSolanaProver`` runs in, which is the session's workdir.
    """

    build_script: Path
    rules: RuleSelection = field(default_factory=InheritRules)
    msg: str = ""
    #: Points-to summary files, in the same relative-to-the-workdir spelling as ``build_script``.
    #: Added to whatever the base conf already names. Empty leaves the key unset, so the package's
    #: ``[package.metadata.certora]`` declaration still applies.
    summaries: tuple[Path, ...] = ()
    #: Extra keys, written over the fields above. For settings that are not one of them, such as
    #: ``multi_assert_check`` or a caller forcing ``rule_sanity``. ``prover_args`` is merged with
    #: the base's by flag, not replaced.
    extra: Conf = field(default_factory=dict)


#: Vacuity-check settings that count as on. ``"none"`` is the documented way to turn the check
#: off, and is treated here as absent.
_SANITY_ON = frozenset({"basic", "advanced"})


#: Solver settings for a nonlinear-arithmetic query, as one recipe.
#:
#: Taken from the reference project's conf, where it serves rules that split heavily and halt on
#: the global timeout.
#:
#: One recipe, not a list a caller edits flag by flag. ``-solanaTACSoundSignedMath`` next to
#: ``-solanaTACMathInt`` (which :data:`TEMPLATE_BASE` already sets) turned a seven-minute,
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


def has_solver_portfolio(conf: Conf) -> bool:
    """Whether ``conf`` already carries the portfolio, by the flags it sets.

    A project that wrote these settings by hand, as the reference project does, is recognized as
    already having them. Appending the recipe again would duplicate the set.
    """
    present = {flag_name(a) for a in str_list(conf.get("prover_args"))}
    return all(flag_name(a) in present for a in NONLINEAR_SOLVER_PORTFOLIO)


#: The one portfolio flag that repeats. Each ``-solvers`` entry adds a solver configuration
#: instead of replacing the last. :func:`merge_prover_args` dedupes on the flag, so
#: ``-solanaTACOptimize 2`` overrides ``-solanaTACOptimize 0``. Merging the four ``-solvers``
#: lines that way would collapse twelve solver instances into three.
_REPEATABLE_FLAG = "-solvers"


def with_solver_portfolio(conf: Conf, enabled: bool) -> Conf:
    """``conf`` with the nonlinear portfolio added or removed.

    The other flags go through :func:`merge_prover_args`, so a project that already sets
    ``-backendStrategy`` has that value replaced. A conf with one flag at two values leaves the
    prover to pick. ``-solvers`` is separate, as :data:`_REPEATABLE_FLAG` says: the project's
    lines are dropped and the portfolio's four replace them. A portfolio is one set, not a flag
    whose last value wins.
    """
    args = str_list(conf.get("prover_args"))
    if not enabled:
        drop = {flag_name(a) for a in NONLINEAR_SOLVER_PORTFOLIO}
        return {**conf, "prover_args": [a for a in args if flag_name(a) not in drop]}
    solvers = [a for a in NONLINEAR_SOLVER_PORTFOLIO if flag_name(a) == _REPEATABLE_FLAG]
    rest = [a for a in NONLINEAR_SOLVER_PORTFOLIO if flag_name(a) != _REPEATABLE_FLAG]
    kept = [a for a in args if flag_name(a) != _REPEATABLE_FLAG]
    return {**conf, "prover_args": merge_prover_args(kept, rest) + solvers}


def with_sanity_floor(conf: Conf) -> Conf:
    """``conf`` with vacuity checking on, at ``basic`` unless the conf already asks for more.

    A floor, not an owned key. ``advanced`` is kept. A conf that never mentions ``rule_sanity``
    gets ``basic``, and so does one that says ``"none"``: with the check off, a [3308] inside the
    generated vacuity rule is reported as verified.
    """
    if str(conf.get("rule_sanity", "")) in _SANITY_ON:
        return conf
    return {**conf, "rule_sanity": "basic"}


def solana_conf(base: Conf, run: RunOverlay) -> Conf:
    """The conf for one ``certoraSolanaProver`` submission.

    ``base`` is not mutated. Every key in :data:`OVERLAY_OWNED_KEYS` is decided here. ``files``
    is dropped: a from-sources run and a prebuilt artifact cannot both be set, and keeping both
    fails inside the prover. ``rule`` follows :data:`RuleSelection`, which is where keeping the
    base value is a real answer.
    """
    forced: Conf = {"build_script": str(run.build_script), "msg": safe_msg(run.msg)}
    if run.summaries:
        # Kept beside the base's entries. Naming any value stops the prover from also applying
        # the package's [package.metadata.certora] declaration, so dropping the base's entries
        # would narrow what it reads.
        forced["solana_summaries"] = list(
            dict.fromkeys(
                [*str_list(base.get("solana_summaries")), *(str(s) for s in run.summaries)]
            )
        )
    extra = dict(run.extra)
    if isinstance(extra_args := extra.get("prover_args"), list):
        extra["prover_args"] = merge_prover_args(
            str_list(base.get("prover_args")), str_list(extra_args)
        )
    conf = overlay(base, drop=OVERLAY_OWNED_KEYS, forced=forced, extra=extra, rules=run.rules)
    return with_sanity_floor(conf)

