"""The Solana prover configuration: reading a project's, and layering a run onto it.

A prover conf is **JSON**, read with a JSON5-tolerant parser — real ones carry trailing commas and
comments, and both the recommended starting point's ``confs/run.conf`` and the public examples'
``Default.conf`` would fail a strict ``json.loads``. Integers come back as *strings*, which looks
wrong until you notice the confs in the wild say ``"loop_iter": "1"``: that is
``Shared/certoraUtils.read_conf_file``'s own ``parse_int=str``, and matching it is what keeps a
round-trip from silently rewriting a project's file.

The layering follows the CVL backend's :func:`~composer.spec.source.prover.prover_config_overlay`:
the project's conf is the base, the run owns a small set of keys, and
:data:`OVERLAY_OWNED_KEYS` says which ones so nothing downstream has to guess whether a base entry
survived. The Solana-specific part of that set is ``build_script`` and ``files``, and they are owned
for a reason spelled out in :mod:`composer.cargo.sbf`: the backend builds inside the sandbox and
hands the prover a script that reruns that same build, so a base conf naming its own build script —
which 15 of 16 surveyed projects do — must not win.

What the run *does not* own is as deliberate. ``solana_inlining`` and ``solana_summaries`` are left
unset, because ``cargo certora-sbf`` reads them out of the package's own
``[package.metadata.certora]`` and reports them through the build manifest, and
``certoraParseBuildScript`` applies them only when the context has none. Setting them here would
override the project's own declaration with our guess at it.
"""

import dataclasses
import io
import json
import logging
import re
import string
from pathlib import Path

import json5

_log = logging.getLogger(__name__)

#: Conf keys the run always decides, whatever the base says. ``files`` is in the set because it is
#: *removed*: ``certoraParseBuildScript.run_rust_build`` asserts the context has no files before a
#: build script may set them, so a base conf naming a prebuilt ``.so`` and a run building from
#: sources cannot both be honored.
#:
#: ``rule`` is deliberately **not** here — see :data:`RuleSelection`, where inheriting the base's
#: selection is one of three distinct intents rather than the absence of one.
OVERLAY_OWNED_KEYS: frozenset[str] = frozenset({"build_script", "files", "msg"})

#: The default base, from `Certora/solana-spec-template <https://github.com/Certora/solana-spec-template>`_
#: — the repository Certora recommends cloning to start a new Solana spec, and therefore the only
#: project-shaped source here that is *advice* rather than evidence of what somebody once did.
#:
#: One of its positions was followed here and then reversed by measurement. The template sets
#: ``optimistic_loop`` to ``false`` where twelve of sixteen surveyed projects set it true, and that
#: was originally copied on the reasoning that counting what projects do promotes the wrong answer.
#: With ``loop_iter: "1"``, ``false`` makes **any** loop inside a handler fail before the rule's own
#: property is reached: a rule calling an Anchor deposit handler came back VIOLATED on
#: *"Unwinding condition in a loop. We recommend to run with --optimistic_loop"*, against a loop in
#: the handler's own borsh path (``docs/cvlr-backend-plan.md`` §7.6.2). So it is true here — and the
#: soundness cost is real and stated: the prover then assumes loops finish within the bound instead
#: of proving it, which can hide a violation that needs more iterations to reach.
#:
#: The five ``-solanaOptimistic*`` memory-model flags remain absent, and unlike ``optimistic_loop``
#: this *is* a departure from the corpus: engagements carry them almost universally — 39 of 39 confs
#: in one project, plus another's ``base.conf`` — alongside ``-solanaAggressiveGlobalDetection``,
#: ``-solanaRemoveCFGDiamonds`` and ``-solanaSlicerIter``. They are absent here because the one thing
#: they were wanted for, they do not do: a matched pair of submissions differing in nothing but those
#: ten flags produced **byte-identical** [3308] errors (``docs/upstream-defects.md`` P4). They are
#: also unsound by name, so adopting a block of them to fix nothing would be the worst of both.
#: ``-solanaOptimisticJoinWithStackPtr`` was measured separately and does nothing for the error it
#: looks like it should address (P3).
TEMPLATE_BASE: dict[str, object] = {
    "msg": "Certora Verification Rules",
    "loop_iter": "1",
    "optimistic_loop": True,
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
    # Vacuity checking, which both public examples enable and this default had omitted. It is the
    # only thing that catches the rule a *blocked* author writes: when the properties in a batch
    # turn out to be unprovable — an un-inlined serialization path, a summarized helper — the way
    # forward that always works is to assume the conclusion. Such a rule VERIFIES, maps cleanly to
    # its property, and passes both halves of the publish gate, because "accounted for, not all
    # green" (§7.5) is designed to avoid *rewarding* weakened rules and cannot *detect* one.
    # Observed doing exactly that: a rule that assumed `vault.key == expected_pda` and then
    # asserted it. A sanity failure is not VERIFIED, so it reaches the author as unaccounted work.
    "rule_sanity": "basic",
}


class MalformedConf(ValueError):
    """A conf file could not be read as JSON5."""


def parse_conf(text: str) -> dict:
    """Parse conf text the way ``certoraRun`` does — JSON5, integers as strings.

    Duplicate keys are rejected, as they are there: a conf that sets ``loop_iter`` twice has two
    different intentions in it and neither the prover nor a reader can tell which one was meant.
    """
    try:
        parsed = json5.load(io.StringIO(text), allow_duplicate_keys=False, parse_int=str)
    except ValueError as exc:
        raise MalformedConf(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise MalformedConf(f"conf is a {type(parsed).__name__}, not an object")
    return parsed


def read_conf(path: Path) -> dict:
    return parse_conf(path.read_text())


def load_base(path: Path | None) -> dict:
    """The base conf for a run: the project's, or the recommended starting point's.

    The fallback is stated rather than empty because an empty conf is not a neutral one — it is a
    conf with no loop bound, no SMT timeout and no prover flags, which verifies differently. When
    the project has no opinion, the recommendation is the honest stand-in for one.
    """
    return dict(TEMPLATE_BASE) if path is None else read_conf(path)


def _flag(arg: str) -> str:
    """The flag a ``prover_args`` entry sets — its first token.

    Entries are shell-ish strings (``"-solanaTACOptimize 2"``), so the same flag at two different
    values is two entries that differ only after the space. Merging on the whole string keeps both
    and lets the prover pick; merging on the flag is what makes an overlay an override."""
    return arg.split(maxsplit=1)[0]


def merge_prover_args(base: list[str], overlay: list[str]) -> list[str]:
    """``base`` with ``overlay``'s flags overriding, base order preserved, new flags appended."""
    replacements = {_flag(a): a for a in overlay}
    merged = [replacements.pop(_flag(a), a) for a in base]
    return merged + [a for a in overlay if a in replacements.values()]


def _str_list(value: object) -> list[str]:
    """A conf field the CLI declares as a list, when a conf wrote one string instead.

    Both spellings are accepted by ``certoraRun``'s own validators, so both appear in real confs."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def tools_version(conf: dict) -> str | None:
    """The platform-tools version this conf asks for.

    Read rather than obeyed by the prover: ``cargo_tools_version`` only reaches ``cargo certora-sbf``
    on the CLI's *own* build path, and this backend owns the build. Honoring it here is what keeps
    the project's declaration meaningful instead of inert."""
    raw = conf.get("cargo_tools_version")
    return str(raw) if isinstance(raw, (str, int)) else None


def sbf_arch(conf: dict) -> str | None:
    raw = conf.get("solana_sbf_arch")
    return str(raw) if isinstance(raw, str) else None


#: The cargo feature that compiles the verification module into the program. Every surveyed project
#: and the recommended starting point agree on the name (``certora = ["no-entrypoint", "dep:cvlr",
#: …]``); it is a default rather than a constant because a project is free to call it something else
#: and ``cargo_features`` below is where a conf would say so.
#:
#: Here rather than beside the submission that uses it, because the scaffold that *creates* the
#: feature and the submission that *enables* it must agree on the name, and this module is the one
#: they both already read.
DEFAULT_FEATURE = "certora"


def cargo_features(conf: dict) -> tuple[str, ...]:
    return tuple(_str_list(conf.get("cargo_features")))


@dataclasses.dataclass(frozen=True)
class InheritRules:
    """Check whatever the base conf selects — its ``rule`` entry, or everything when it has none.

    The right default for a submission that did not come from an authoring loop: a project's conf
    names the rules its authors meant to run, and a run with no opinion has no business replacing
    that with a different set."""


@dataclasses.dataclass(frozen=True)
class SelectRules:
    """Check exactly these. Names are globs, which is how a parametric rule's instances are named."""

    names: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class AllRules:
    """Check every rule the artifact declares, overriding a narrower selection in the base.

    Distinct from :class:`InheritRules` precisely where it matters: against a base conf that names
    three of thirty rules, one of these runs three and the other runs thirty. Collapsing them into
    "no rules given" is how a run silently checks a different set than it reports."""


type RuleSelection = InheritRules | SelectRules | AllRules


@dataclasses.dataclass(frozen=True)
class RunOverlay:
    """What one submission adds to the base conf.

    ``build_script`` is a path as the prover will read it — relative to the directory
    ``certoraSolanaProver`` runs in, which is the session's workdir.
    """

    build_script: str
    rules: RuleSelection = dataclasses.field(default_factory=InheritRules)
    msg: str = ""
    #: Extra keys, applied last. For the run-shaped settings that are not a conf *policy* —
    #: ``multi_assert_check`` for a variant run, ``rule_sanity`` when a caller wants to force it.
    extra: dict[str, object] = dataclasses.field(default_factory=dict)


#: Characters ``certoraRun`` accepts in ``msg`` — a deliberate subset of what its own
#: ``certoraValidateFuncs.validate_msg`` permits, so that if the CLI ever narrows its set this stays
#: valid without an edit. The CLI *raises* on anything outside it, before a single rule is
#: processed, which is why this is a hard gate and not a nicety.
_MSG_SAFE = set(string.ascii_letters) | set(string.digits) | set(" ,.:_-()[]'/")


def safe_msg(msg: str) -> str:
    """``msg`` reduced to what the prover will accept.

    The ``msg`` a run sends is built from a component's display name, and a display name is prose
    written by a model — so it carries whatever prose carries. An ampersand is enough:
    ``"Deposit & Balance Tracking"`` made ``certoraRun`` raise
    ``{'&'} not allowed in 'msg'`` and every submission for that unit failed before any rule was
    read. The author cannot fix it, because the name is not in the harness; two units in one run
    spent 6 and 13+ submissions on it, one of them holding a finished ten-rule harness.

    Offending characters become spaces rather than being dropped, so words do not run together, and
    runs of whitespace collapse. Length is left to the CLI, which truncates with a warning rather
    than raising.
    """
    return re.sub(r"\s+", " ", "".join(c if c in _MSG_SAFE else " " for c in msg)).strip()


def solana_conf(base: dict, overlay: RunOverlay) -> dict:
    """The conf for one ``certoraSolanaProver`` submission.

    ``base`` is never mutated. Every key in :data:`OVERLAY_OWNED_KEYS` is decided here — including
    ``files``, which is *dropped*, since a from-sources run and a prebuilt artifact are mutually
    exclusive inputs and keeping both would fail inside the prover rather than here. ``rule`` is
    decided by :data:`RuleSelection`, which is the one key where "the base wins" is a real answer.
    """
    conf = {k: v for k, v in base.items() if k not in OVERLAY_OWNED_KEYS}
    conf["build_script"] = overlay.build_script
    conf["msg"] = safe_msg(overlay.msg)
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
    return conf


def dump_conf(conf: dict) -> str:
    """Serialize a conf for writing. Plain JSON: JSON5 is what we *accept*, not what we emit."""
    return json.dumps(conf, indent=4) + "\n"
