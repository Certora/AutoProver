"""Prover confs as data: reading and writing them, and layering one run's settings onto a base.

Shared by every ecosystem. What a run forces, and how it escalates, is each ecosystem's own
policy (:func:`composer.spec.source.prover.prover_config_overlay` for CVL,
:func:`composer.spec.cvlr.conf.solana_conf` for Solana), built on :func:`overlay`.

Confs are JSON5. Real ones have trailing commas and comments, and fail ``json.loads``. Integers
stay strings. Real confs write ``"loop_iter": "1"``, and retyping them on a round-trip would
rewrite the project's file. ``certoraRun`` parses them the same way.
"""

import io
import json
import re
import string
from dataclasses import dataclass
from pathlib import Path

import json5

#: A parsed conf: the top-level JSON object, with integers kept as strings.
type Conf = dict[str, object]


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


def dump_conf(conf: Conf) -> str:
    """Serialize a conf for writing. Plain JSON. JSON5 is accepted on read and not written."""
    return json.dumps(conf, indent=2) + "\n"


def str_list(value: object) -> list[str]:
    """A conf field the CLI declares as a list, when a conf wrote one string.

    ``certoraRun`` accepts both spellings, and both appear in real confs."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def flag_name(arg: str) -> str:
    """The flag a ``prover_args`` entry sets: its first token.

    Entries are strings like ``"-solanaTACOptimize 2"``. The same flag at two values differs only
    after the space. Merging on the whole string keeps both and lets the prover pick. Merging on
    the flag is what makes an overlay replace the base value."""
    return arg.split(maxsplit=1)[0]


def merge_prover_args(base: list[str], overlay: list[str]) -> list[str]:
    """``base`` with ``overlay``'s flags overriding, base order preserved, new flags appended."""
    replacements = {flag_name(a): a for a in overlay}
    merged = [replacements.pop(flag_name(a), a) for a in base]
    return merged + [a for a in overlay if a in replacements.values()]


def with_loop_iter(conf: Conf, iterations: int) -> Conf:
    """``conf`` with a new loop bound, written as a string, which is how a conf spells an integer."""
    return {**conf, "loop_iter": str(iterations)}


def has_optimistic_loop(conf: Conf) -> bool:
    """Whether ``conf`` assumes loops finish.

    A hand-written conf may spell the setting as a string, the way ``loop_iter`` is spelled.
    Absent, or any other value, is false, the prover's default.
    """
    match conf.get("optimistic_loop"):
        case bool(b):
            return b
        case str(s):
            return s.strip().lower() == "true"
        case _:
            return False


def with_optimistic_loop(conf: Conf, enabled: bool) -> Conf:
    """``conf`` with the loop-halt assumption on or off, written as a JSON bool."""
    return {**conf, "optimistic_loop": enabled}


#: Characters ``certoraRun`` accepts in ``msg``. A subset of what the CLI permits today, so a
#: narrower CLI set still accepts these. The CLI raises on anything outside its set before any
#: rule is processed.
_MSG_SAFE = set(string.ascii_letters) | set(string.digits) | set(" ,.:_-()[]'/")


def safe_msg(msg: str) -> str:
    """``msg`` reduced to what the prover accepts.

    A message built from prose fails on one stray character: ``"Deposit & Balance Tracking"``
    raises ``{'&'} not allowed in 'msg'`` before any rule is read.

    Characters outside the set become spaces, so words do not run together, and runs of whitespace
    collapse. Length is left to the CLI, which truncates with a warning.
    """
    return re.sub(r"\s+", " ", "".join(c if c in _MSG_SAFE else " " for c in msg)).strip()


@dataclass(frozen=True)
class InheritRules:
    """Check whatever the base conf selects: its ``rule`` and ``exclude_rule`` entries, or every
    rule when it has neither."""


@dataclass(frozen=True)
class SelectRules:
    """Check these rules. Names are globs, which is how a parametric rule's instances are named."""

    names: tuple[str, ...]


@dataclass(frozen=True)
class ExcludeRules:
    """Check every rule the base conf selects except these."""

    names: tuple[str, ...]


@dataclass(frozen=True)
class AllRules:
    """Check every rule the artifact declares, dropping a narrower ``rule`` list in the base.

    Against a base that names three of thirty rules, :class:`InheritRules` runs three and this
    runs thirty. Treating "no rules given" as either one would check a different set than the
    run reports."""


type RuleSelection = InheritRules | SelectRules | ExcludeRules | AllRules


def with_rules(conf: Conf, rules: RuleSelection) -> Conf:
    """``conf`` scoped to ``rules``. Each selection writes only the key it names."""
    match rules:
        case InheritRules():
            return conf
        case SelectRules(names):
            return {**conf, "rule": list(names)}
        case ExcludeRules(names):
            return {**conf, "exclude_rule": list(names)}
        case AllRules():
            return {k: v for k, v in conf.items() if k != "rule"}


def overlay(
    base: Conf,
    *,
    forced: Conf,
    drop: frozenset[str] = frozenset(),
    extra: Conf | None = None,
    rules: RuleSelection = InheritRules(),
) -> Conf:
    """``base`` with one run's settings layered on. ``base`` is not mutated.

    In order: the ``drop`` keys are removed, ``forced`` is written over what is left, ``extra``
    over that, and ``rules`` scopes the result. ``extra`` replaces whole values. A caller that
    wants its ``prover_args`` merged with the base's passes them through
    :func:`merge_prover_args` first.
    """
    conf = {k: v for k, v in base.items() if k not in drop}
    conf.update(forced)
    conf.update(extra or {})
    return with_rules(conf, rules)
