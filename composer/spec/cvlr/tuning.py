"""The two tuning files a CVLR build gives the Solana Prover, and how each is assembled.

The Prover verifies the compiled SBF program. Before it checks a rule, it decides which calls to
analyze through their bodies and what it may assume about the calls it does not analyze. Those
two answers are the inlining file and the summaries file. A package names them in
``[package.metadata.certora]`` (``solana_inlining``, ``solana_summaries``), and ``cargo
certora-sbf`` reports them to the Prover through the build manifest. Every directive in either
file is a regular expression over demangled symbol names.

Inlining (:data:`INLINING`)
    One directive per line, ``#[inline] <pattern>`` or ``#[inline(never)] <pattern>``. An inlined
    call is analyzed through its body. Any other call is opaque. The Prover does not look inside
    it, and it knows nothing about what the call wrote. The starting configuration leaves
    ``core``, ``std``, ``alloc``, ``solana_program`` and ``anchor_lang`` opaque by default. The
    exceptions are the functions whose effects a rule depends on: the ``AccountInfo`` accessors,
    ``invoke``, error conversions, and Anchor's account loaders.

Summaries (:data:`SUMMARIES`)
    Points-to summaries for opaque calls: one or more ``#[type(<location>:<kind>)]`` lines, then
    the pattern they describe. A location is a register (``r0``, the return value) or memory at an
    offset from one (``(*i64)(r1+8)``, the second word written through the first argument, which
    is where an out-pointer return lands). A kind is ``num``, ``ptr_heap`` or ``ptr_external``.
    The pointer analysis needs a type for memory an opaque call wrote, and without the body a
    summary is the only source of one.

A pattern that matches nothing is not an error. The directive does not apply, and the Prover runs
with a different configuration than the file appears to describe. Most of the care taken below
is about that failure.

Each file is split into layers. The two starting layers live only under :data:`ENV_DIR`. The
rest are in the target's ``envs/`` directory::

    <stem>_core.txt           starting configuration: the Rust runtime and the Solana platform
    <stem>_anchor.txt         starting configuration: the Anchor framework
    <stem>_<unit>_run.txt     one unit's own directives
    <stem>.txt                generated from the first two, named by the package
    <stem>_<unit>.txt         generated from all three, named by one unit's conf

When to change which layer:

- **The starting layers** are the configuration every target begins with, kept under
  :data:`ENV_DIR`. A change every target needs, such as a soundness fix, is made there. They are
  written with pre-split ``solana_program::`` paths, and :mod:`composer.spec.cvlr.env_paths`
  rewrites those into the target's platform generation when a composite is built. They are not
  copied into the target. A composite carries their content, spelled for that target.
- **The unit layer** holds the directives one unit's submission needs. A rule may need to see
  through a library function the starting layers leave opaque (``#[inline]``). A function may be
  too expensive or impossible to analyze, and a rule does better leaving it opaque
  (``#[inline(never)]``). An opaque call that the pointer analysis cannot type needs a summary.
  The layer is per unit because a pattern applies to the whole build, and no cargo feature can
  scope it. It is written against the project's own symbols, so paths in it are not rewritten.
- **The composites are not edited.** :func:`compose_env` builds them from the layers, and the
  scaffold rewrites the package composite whenever it differs from that. A directive written into
  a composite is not in any layer, and the next composition drops it.
"""

from dataclasses import dataclass
from pathlib import Path

from composer.spec.cvlr.env_paths import PathDialect

#: The starting layers, shipped in the wheel. Every composite is built from these.
ENV_DIR = Path(__file__).parent / "envs"


@dataclass(frozen=True)
class EnvFamily:
    """One tuning file, split into layers.

    ``core`` and ``anchor`` are the starting layers. The composite is generated from the two.
    """

    stem: str
    #: What the file's directives are, as prose.
    kind: str

    @property
    def core(self) -> str:
        return f"{self.stem}_core.txt"

    @property
    def anchor(self) -> str:
        return f"{self.stem}_anchor.txt"

    @property
    def composite(self) -> str:
        """The file the package declares. Generated from the starting layers."""
        return f"{self.stem}.txt"

    def unit_layer(self, unit: str) -> str:
        """The file name for one unit's own directives.

        A summary is a symbol pattern the prover applies to the whole build, not something a cargo
        feature can scope. Lines added to a file every unit's conf names would apply to every
        unit's submission.
        """
        return f"{self.stem}_{unit}_run.txt"

    def unit_composite(self, unit: str) -> str:
        """The file one unit's conf names. Generated from all three layers."""
        return f"{self.stem}_{unit}.txt"


#: Which calls the Prover analyzes through their bodies. Declared as ``solana_inlining``.
INLINING = EnvFamily("cvlr_inlining", kind="Inlining directives")
#: What the pointer analysis may assume about the memory an opaque call writes. Declared as
#: ``solana_summaries``.
SUMMARIES = EnvFamily("cvlr_summaries", kind="Points-to summaries")
ENV_FAMILIES = (INLINING, SUMMARIES)

#: The starting layers — one content for every target, as against the per-unit layer.
STARTING_ENVS = tuple(name for f in ENV_FAMILIES for name in (f.core, f.anchor))


_GENERATED_HEADER = """;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;; Generated — this is the file the build reports to the prover.
;;; Rewritten on every run. Composed, in order, from AutoProver's
;;; starting configuration:
;;;   {core}
;;;   {anchor}
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
"""


def starting_env(name: str, dialect: PathDialect = PathDialect()) -> str:
    """One starting layer, spelled for the target's platform generation.

    The default dialect changes nothing, so a caller comparing against the file on disk gets that
    file.
    """
    return dialect.render((ENV_DIR / name).read_text())


def compose_env(
    family: EnvFamily, *, unit_layer: str = "", dialect: PathDialect = PathDialect()
) -> str:
    """The generated composite: header, then the layers, in order.

    The scaffold writes the package-level composite, where ``unit_layer`` is empty. A non-empty
    layer is one unit's directives, so they are not applied to another unit's submission (see
    :meth:`EnvFamily.unit_layer`). Those are the project's own symbols, so the dialect leaves them
    alone.
    """
    header = _GENERATED_HEADER.format(core=family.core, anchor=family.anchor)
    parts = [header]
    if dialect.aliases:
        # Said in the file, so a reader comparing it with the starting layers can see that paths
        # were rewritten.
        parts.append(
            f";;; Platform paths rewritten for this target's generation "
            f"({len(dialect.aliases)} aliases) — see composer/spec/cvlr/env_paths.py\n"
        )
    parts += [
        starting_env(family.core, dialect),
        starting_env(family.anchor, dialect),
    ]
    if unit_layer.strip():
        parts.append(unit_layer)
    return "\n".join(p.rstrip("\n") for p in parts) + "\n"
