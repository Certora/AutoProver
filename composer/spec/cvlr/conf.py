"""Read a Solana prover conf and layer one run's settings onto it.

Confs are JSON5. Real ones have trailing commas and comments, and both the recommended starting
point's ``confs/run.conf`` and the public examples' ``Default.conf`` fail ``json.loads``. Integers
stay strings. Real confs write ``"loop_iter": "1"``, and retyping them on a round-trip would
rewrite the project's file. ``certoraRun`` parses them the same way.

The project's conf is the base, as in
:func:`composer.spec.source.prover.prover_config_overlay`. The run owns the keys in
:data:`OVERLAY_OWNED_KEYS`. ``build_script`` comes from the overlay, so a base conf's own script
does not win. ``files`` is dropped: the prover refuses a build script that sets files when the
conf already names some, and a prebuilt ``.so`` cannot sit next to a from-sources build.
``server`` is owned because the deployment decides which cloud a run reaches.

:func:`project_conf` chooses the base. Everything else in the project's conf is kept: the loop
bound, the solver flags, ``prover_version``. :func:`with_sanity_floor` is the exception. It turns
vacuity checking on when the base never mentions ``rule_sanity``. A rule that assumes its
conclusion still verifies, and vacuity checking is what catches that.

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
import io
import json
import logging
import re
import string
from dataclasses import dataclass, field
from pathlib import Path

import json5

_log = logging.getLogger(__name__)

#: A parsed conf: the top-level JSON object, with integers kept as strings.
type Conf = dict[str, object]

#: Conf keys the run always decides, whatever the base says. ``files`` is in the set because it
#: is removed. The prover refuses a build script that sets files when the conf already names
#: some, so a base conf naming a prebuilt ``.so`` and a run building from sources cannot both
#: be kept.
#:
#: ``rule`` is not here. :data:`RuleSelection` has three answers, and inheriting the base's
#: selection is one of them.
#:
#: ``server`` is owned because the deployment decides which cloud a run reaches and passes it on
#: the CLI (:func:`composer.prover.core.make_prover_options`). Project confs that name a server
#: say ``"production"``. Dropping the key keeps a single answer.
OVERLAY_OWNED_KEYS: frozenset[str] = frozenset({"build_script", "files", "msg", "server"})

#: The default base, from Certora's solana-spec-template
#: (https://github.com/Certora/solana-spec-template), the repository Certora recommends cloning
#: to start a new Solana spec.
#:
#: ``optimistic_loop`` is false, which is the template's setting. Turning it on assumes loop halt
#: conditions instead of proving them, so a violation that needs more iterations is not found.
#: Bound the inputs that set the trip count, or edit the loop, before raising ``loop_iter``.
#:
#: ``loop_iter`` is 2, not the template's 1. A bound of 1 fails inside a handler's own
#: serialization before the rule's property is reached: an Anchor deposit handler comes back
#: violated on "Unwinding condition in a loop" against a loop in its borsh path.
#:
#: The ``-solanaOptimistic*`` flags are absent. They are unsound, and a pair of submissions that
#: differed only in those flags (plus ``-solanaAggressiveGlobalDetection``,
#: ``-solanaRemoveCFGDiamonds``, and ``-solanaSlicerIter``) produced the same [3308] errors.
#: ``-solanaOptimisticJoinWithStackPtr`` does not fix the error its name suggests either.
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
    # Vacuity checking. Both public examples enable it. It is what catches a rule that assumes
    # its conclusion: when a property cannot be proved (an un-inlined serialization path, a
    # summarized helper), assuming the conclusion makes the rule verify. A sanity failure is not
    # a verified result. With the check off, a [3308] raised inside the generated vacuity rule
    # is reported as VERIFIED.
    "rule_sanity": "basic",
}


class MalformedConf(ValueError):
    """A conf file could not be read as JSON5."""


def parse_conf(text: str) -> Conf:
    """Parse conf text the way ``certoraRun`` does: JSON5, integers as strings.

    Duplicate keys are rejected, as they are there. A conf that sets ``loop_iter`` twice has two
    values, and neither the prover nor a reader can tell which was meant.
    """
    try:
        parsed = json5.load(io.StringIO(text), allow_duplicate_keys=False, parse_int=str)
    except ValueError as exc:
        raise MalformedConf(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise MalformedConf(f"conf is a {type(parsed).__name__}, not an object")
    return parsed


def read_conf(path: Path) -> Conf:
    return parse_conf(path.read_text())


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
    """The base conf for a run: the project's, or the recommended starting point's.

    The fallback is :data:`TEMPLATE_BASE`, not an empty conf. An empty conf has no loop bound, no
    SMT timeout, and no prover flags, and that verifies differently. Logged either way, so a run
    that is not using the project's settings says so.
    """
    if path is None:
        _log.info("cvlr: no project conf found; using the recommended starting point's settings")
        return dict(TEMPLATE_BASE)
    _log.info("cvlr: prover conf from %s", path)
    return read_conf(path)


def _flag(arg: str) -> str:
    """The flag a ``prover_args`` entry sets: its first token.

    Entries are strings like ``"-solanaTACOptimize 2"``. The same flag at two values differs only
    after the space. Merging on the whole string keeps both and lets the prover pick. Merging on
    the flag is what makes an overlay replace the base value."""
    return arg.split(maxsplit=1)[0]


def merge_prover_args(base: list[str], overlay: list[str]) -> list[str]:
    """``base`` with ``overlay``'s flags overriding, base order preserved, new flags appended."""
    replacements = {_flag(a): a for a in overlay}
    merged = [replacements.pop(_flag(a), a) for a in base]
    return merged + [a for a in overlay if a in replacements.values()]


def _str_list(value: object) -> list[str]:
    """A conf field the CLI declares as a list, when a conf wrote one string.

    ``certoraRun`` accepts both spellings, and both appear in real confs."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


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
    return tuple(_str_list(conf.get("cargo_features")))


@dataclass(frozen=True)
class InheritRules:
    """Check whatever the base conf selects: its ``rule`` entry, or every rule when it has none.

    The default when a run has not named rules of its own. The project's conf already says which
    rules to run."""


@dataclass(frozen=True)
class SelectRules:
    """Check these rules. Names are globs, which is how a parametric rule's instances are named."""

    names: tuple[str, ...]


@dataclass(frozen=True)
class AllRules:
    """Check every rule the artifact declares, dropping a narrower ``rule`` list in the base.

    Against a base that names three of thirty rules, :class:`InheritRules` runs three and this
    runs thirty. Treating "no rules given" as either one would check a different set than the
    run reports."""


type RuleSelection = InheritRules | SelectRules | AllRules


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
    #: Extra keys, applied last. For settings that are not one of the fields above, such as
    #: ``multi_assert_check`` or a caller forcing ``rule_sanity``.
    extra: Conf = field(default_factory=dict)


#: Characters ``certoraRun`` accepts in ``msg``. A subset of what the CLI permits today, so a
#: narrower CLI set still accepts these. The CLI raises on anything outside its set before any
#: rule is processed.
_MSG_SAFE = set(string.ascii_letters) | set(string.digits) | set(" ,.:_-()[]'/")


def safe_msg(msg: str) -> str:
    """``msg`` reduced to what the prover accepts.

    The message is built from a component's display name, which is prose. ``certoraRun`` rejects
    characters outside :data:`_MSG_SAFE` before any rule is read. An ampersand is enough:
    ``"Deposit & Balance Tracking"`` raises ``{'&'} not allowed in 'msg'``. The name is not in
    the harness, so it cannot be fixed there.

    Characters outside the set become spaces, so words do not run together, and runs of whitespace
    collapse. Length is left to the CLI, which truncates with a warning.
    """
    return re.sub(r"\s+", " ", "".join(c if c in _MSG_SAFE else " " for c in msg)).strip()


#: Vacuity-check settings that count as on. ``"none"`` is the documented way to turn the check
#: off, and is treated here as absent.
_SANITY_ON = frozenset({"basic", "advanced"})


#: Solver settings for a nonlinear-arithmetic query, as one recipe.
#:
#: Taken from the reference project's conf. The extra seeds are there because more random seeds
#: help the nonlinear solver, on rules that split heavily and halt on the global timeout.
#: ``-smt_useLIA`` is in that conf because linear arithmetic is faster inside checked-arithmetic
#: helpers.
#:
#: One recipe, not a list a caller edits flag by flag. ``-solanaTACSoundSignedMath`` next to
#: ``-solanaTACMathInt`` (which :data:`TEMPLATE_BASE` already sets) turned a seven-minute,
#: eighteen-rule run into a two-hour timeout with thirteen rules unverified. The expansion is
#: fixed so that combination is not assembled by hand.
#:
#: Every flag here changes how long an answer takes, not what a verified result means. Solver
#: strategy, theory selection, and random seeds. The list does not include flags that change the
#: meaning of a result.
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
    present = {_flag(a) for a in _str_list(conf.get("prover_args"))}
    return all(_flag(a) in present for a in NONLINEAR_SOLVER_PORTFOLIO)


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
    args = _str_list(conf.get("prover_args"))
    if not enabled:
        drop = {_flag(a) for a in NONLINEAR_SOLVER_PORTFOLIO}
        return {**conf, "prover_args": [a for a in args if _flag(a) not in drop]}
    solvers = [a for a in NONLINEAR_SOLVER_PORTFOLIO if _flag(a) == _REPEATABLE_FLAG]
    rest = [a for a in NONLINEAR_SOLVER_PORTFOLIO if _flag(a) != _REPEATABLE_FLAG]
    kept = [a for a in args if _flag(a) != _REPEATABLE_FLAG]
    return {**conf, "prover_args": merge_prover_args(kept, rest) + solvers}


def with_loop_iter(conf: Conf, iterations: int) -> Conf:
    """``conf`` with a new loop bound, written as a string, which is how a conf spells an integer."""
    return {**conf, "loop_iter": str(iterations)}


def has_optimistic_loop(conf: Conf) -> bool:
    """Whether ``conf`` assumes loops finish.

    :data:`TEMPLATE_BASE` writes a JSON bool. A hand-written conf may spell it as a string, the
    way ``loop_iter`` is spelled. Absent, or any other value, is false. That is the template's
    setting, and what the prover does with a key it was not given.
    """
    match conf.get("optimistic_loop"):
        case bool(b):
            return b
        case str(s):
            return s.strip().lower() == "true"
        case _:
            return False


def with_optimistic_loop(conf: Conf, enabled: bool) -> Conf:
    """``conf`` with the loop-halt assumption on or off.

    This changes what a verified result means. Every loop is assumed to finish within
    ``loop_iter``, so a violation reachable only on a later iteration is not found and the rule
    still reports verified. Written as a JSON bool, matching the template.
    """
    return {**conf, "optimistic_loop": enabled}


def with_sanity_floor(conf: Conf) -> Conf:
    """``conf`` with vacuity checking on, at ``basic`` unless the conf already asks for more.

    A floor, not an owned key. ``advanced`` is kept. A conf that never mentions ``rule_sanity``,
    including the recommended starting point, gets ``basic``. ``"none"`` is treated as off: with
    the check off, a [3308] inside the generated vacuity rule is reported as verified, and a rule
    that assumes its conclusion verifies too.
    """
    if str(conf.get("rule_sanity", "")) in _SANITY_ON:
        return conf
    return {**conf, "rule_sanity": "basic"}


def solana_conf(base: Conf, overlay: RunOverlay) -> Conf:
    """The conf for one ``certoraSolanaProver`` submission.

    ``base`` is not mutated. Every key in :data:`OVERLAY_OWNED_KEYS` is decided here. ``files``
    is dropped: a from-sources run and a prebuilt artifact cannot both be set, and keeping both
    fails inside the prover. ``rule`` follows :data:`RuleSelection`, which is where keeping the
    base value is a real answer.
    """
    conf = {k: v for k, v in base.items() if k not in OVERLAY_OWNED_KEYS}
    conf["build_script"] = str(overlay.build_script)
    conf["msg"] = safe_msg(overlay.msg)
    if overlay.summaries:
        # Kept beside the base's entries. Naming any value stops the prover from also applying
        # the package's [package.metadata.certora] declaration, so dropping the base's entries
        # would narrow what it reads.
        conf["solana_summaries"] = list(
            dict.fromkeys(
                [*_str_list(base.get("solana_summaries")), *(str(s) for s in overlay.summaries)]
            )
        )
    match overlay.rules:
        case SelectRules(names):
            conf["rule"] = list(names)
        case AllRules():
            conf.pop("rule", None)
        case InheritRules():
            pass
    for key, value in overlay.extra.items():
        if key == "prover_args" and isinstance(value, list):
            conf[key] = merge_prover_args(_str_list(base.get("prover_args")), _str_list(value))
        else:
            conf[key] = value
    return with_sanity_floor(conf)


def dump_conf(conf: Conf) -> str:
    """Serialize a conf for writing. Plain JSON. JSON5 is accepted on read and not written."""
    return json.dumps(conf, indent=4) + "\n"
