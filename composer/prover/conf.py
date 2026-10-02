"""Prover confs as data: writing them, and scoping one to a run's rule selection.

Shared by every ecosystem. What a conf contains is each ecosystem's own policy
(:func:`composer.spec.source.prover.prover_config_overlay` for CVL,
:func:`composer.spec.cvlr.conf.solana_conf` for Solana).
"""

import json
import re
import string
from dataclasses import dataclass

#: A conf: the top-level JSON object.
type Conf = dict[str, object]


def dump_conf(conf: Conf) -> str:
    """Serialize a conf for writing."""
    return json.dumps(conf, indent=2) + "\n"


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

    def apply_to(self, conf: Conf) -> Conf:
        return conf


@dataclass(frozen=True)
class SelectRules:
    """Check these rules. Names are globs, which is how a parametric rule's instances are named."""

    names: tuple[str, ...]

    def __post_init__(self) -> None:
        # Sorted so that selections naming the same rules compare equal. Coerced to a tuple
        # because checkpointed graph state records selections, and the checkpoint serializer
        # restores tuples as lists.
        object.__setattr__(self, "names", tuple(sorted(self.names)))

    def apply_to(self, conf: Conf) -> Conf:
        return {**conf, "rule": list(self.names)}


@dataclass(frozen=True)
class ExcludeRules:
    """Check every rule the base conf selects except these."""

    names: tuple[str, ...]

    def __post_init__(self) -> None:
        # See SelectRules.__post_init__.
        object.__setattr__(self, "names", tuple(sorted(self.names)))

    def apply_to(self, conf: Conf) -> Conf:
        return {**conf, "exclude_rule": list(self.names)}


#: A run's rule scope. ``apply_to(conf)`` returns ``conf`` scoped to it, writing only the key the
#: selection names. Selections are hashable, and equal whenever they name the same rules.
type RuleSelection = InheritRules | SelectRules | ExcludeRules
