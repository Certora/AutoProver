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

Summaries the authoring loop adds (:class:`SummaryDirective`, written by :class:`TuningFiles`)
    The Solana Prover's pointer analysis refuses some code outright, most commonly [3308] on an
    Anchor program's ``#[error_code]`` enum formatting its ``#[msg("...")]`` string, which every
    ``require!`` and every ``?`` in a handler can reach. A rule that reaches such a path gets no
    verdict. The remedy is a summary in the unit layer.

    A summary is unsound. It replaces a function with "anything could have happened", so
    summarizing the wrong thing does not fail. It produces a green rule that checked less than it
    appears to. So every directive carries a ``why``, which the judge and the report both get, and
    adding one invalidates the prover stamp (:func:`summary_history`). Nothing here checks that a
    summary does not cover the code under test, because a regex cannot be matched against symbols
    before a build. That check is the judge's, with the declared ``why`` as the evidence.
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


#: What the prover accepts as a summary body: a ``#[type(...)]`` annotation is optional, and its
#: absence means "return anything", which is the common case for a formatting path nobody asserts
#: over.
_TYPE_ANNOTATION = "#[type({body})]"

#: Header of the per-unit layer, so a reader of the file knows who owns it and that editing it is
#: pointless — the next build rewrites it from the run's state.
_UNIT_LAYER_HEADER = (
    ";;; Summaries added by the CVLR authoring loop for unit `{unit}`.\n"
    ";;; DO NOT EDIT: rewritten from the run's state on every build."
)


@dataclass(frozen=True)
class SummaryDirective:
    """One symbol pattern the prover should summarize rather than analyze."""

    #: A regex over demangled symbols, the way the canonical files spell them — anchored with
    #: ``^``/``$`` by convention, e.g. ``^<vault::VaultError as core::fmt::Display>::fmt$``.
    pattern: str
    #: Why analyzing it fails and why summarizing it is sound for the properties in this batch. Ends
    #: up in the unit's tuning file and in the report; it is the only account anybody gets.
    why: str
    #: The ``#[type(...)]`` body, without the wrapper — ``None`` for an unconstrained return.
    returns: str | None = None

    def render(self) -> str:
        lines = [f"; {line}" for line in self.why.strip().splitlines() or [""]]
        if self.returns is not None:
            lines.append(_TYPE_ANNOTATION.format(body=self.returns))
        lines.append(self.pattern)
        return "\n".join(lines)


def render_layer(header: str, directives: tuple[SummaryDirective, ...]) -> str:
    """A unit's summaries layer: its header, then one block per directive."""
    return "\n\n".join([header.rstrip("\n"), *(d.render() for d in directives)]) + "\n"


@dataclass(frozen=True)
class TuningFiles:
    """Where one unit's tuning files are, and how paths in them must be spelled.

    Held rather than derived from the harness module's path: the envs directory and the specs
    directory are siblings by convention, and reconstructing one from the other would encode that
    convention in a second place.

    ``unit`` names the file this unit's directives land in. Every unit now shares one working tree
    (``docs/single-working-tree.md``), and a summary is the one thing a cargo feature cannot scope:
    it is a regex over symbols that the prover applies to the whole build, so a shared file would
    apply one unit's summaries to another unit's submission. Since
    :mod:`composer.spec.cvlr.tuning`'s own warning is that a wrong summary produces "a green rule
    that checked less than it appears to", that leak is exactly the failure that must not be
    possible. The unit's conf names the unit's composite; the project's own declaration in
    ``[package.metadata.certora]`` is left alone and still answers for anything this run does not
    submit.
    """

    envs_dir: Path
    dialect: PathDialect
    #: The harness module name, which is what makes these files this unit's.
    unit: str

    def unit_layer_path(self, family: EnvFamily = SUMMARIES) -> Path:
        """This unit's own layer — machine-owned, rewritten from state on every build."""
        return self.envs_dir / family.unit_layer(self.unit)

    def composite_path(self, family: EnvFamily = SUMMARIES) -> Path:
        """The composite this unit's conf names."""
        return self.envs_dir / family.unit_composite(self.unit)

    def write(self, directives: tuple[SummaryDirective, ...]) -> None:
        """Rewrite this unit's summaries layer and recompose the file its conf names.

        Rewritten wholesale from ``directives`` rather than appended to, so the state the run
        recorded and the file on disk cannot drift — and a run that ends up with no directives
        leaves a layer with nothing in it but the header, which composes to the starting
        configuration alone. That is what makes this a *derived* file in the sense
        ``docs/single-working-tree.md`` §4 needs: dropping a directive from state removes it from
        disk, where an appending writer would have kept it forever.

        Called from the build path rather than from the tool that records a directive. One writer,
        reading state the reducer has already merged: a tool writing its own view of the list would
        race a concurrent sibling and drop one of the two, which is the same bug as the unreduced
        state key and would not have raised.
        """
        header = _UNIT_LAYER_HEADER.format(unit=self.unit)
        layer = render_layer(header, directives)
        self.envs_dir.mkdir(parents=True, exist_ok=True)
        self.unit_layer_path(SUMMARIES).write_text(layer)
        self.composite_path(SUMMARIES).write_text(
            compose_env(SUMMARIES, unit_layer=layer, dialect=self.dialect)
        )

    def missing(self) -> tuple[str, ...]:
        """Tuning files that should be on disk and are not.

        Checked because the failure is otherwise mute: a summary written to a file the build does not
        read changes nothing, and the author would read a successful tool result and a second
        identical [3308].

        Both kinds are checked. The package composites are the scaffold's and are what the build
        manifest declares — inlining still reaches the prover that way, since nothing in the loop
        writes an inlining directive. The unit summaries composite is this run's and is what the
        unit's conf names; :meth:`write` creates it, so its absence means no build has staged yet.
        """
        package = tuple(
            family.composite
            for family in ENV_FAMILIES
            if not (self.envs_dir / family.composite).is_file()
        )
        unit = () if self.composite_path().is_file() else (SUMMARIES.unit_composite(self.unit),)
        return package + unit


def merge_summaries(
    left: list[SummaryDirective], right: list[SummaryDirective]
) -> list[SummaryDirective]:
    """State reducer for the summary list: append, in order, deduplicating by pattern.

    A reducer rather than a plain field because two ``summarize_for_prover`` calls can land in one
    graph step — the model routinely emits several tool calls per turn — and LangGraph refuses two
    writes to an unreduced key. That is not a hypothetical: an end-to-end run made five calls while
    hunting for a pattern that matched, two of them concurrent, and the step raised
    ``InvalidUpdateError`` and took the whole unit down with it.

    Deduplicating rather than replacing is what makes concurrent calls correct: each tool contributes
    only its own directive, so neither has to have seen the other's.
    """
    merged = list(left)
    seen = {d.pattern for d in merged}
    for directive in right:
        if directive.pattern in seen:
            continue
        merged.append(directive)
        seen.add(directive.pattern)
    return merged


def summary_history(directives: tuple[SummaryDirective, ...]) -> tuple[str, ...]:
    """The directives as ``version_history`` tokens, so a stamp predating one goes stale.

    Keyed on the pattern and the return type — what the prover actually does differently — and not
    on ``why``, so correcting the wording of a justification does not cost a submission.
    """
    return tuple(f"summary:{d.pattern}:{d.returns or ''}" for d in directives)
