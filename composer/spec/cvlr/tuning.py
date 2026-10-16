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
    it: the call's return value is unconstrained, and it writes nothing, so everything the real
    function would have written keeps the value it had before the call. A summary (below) names
    the few locations it does write. The starting configuration leaves
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

Each file is split into layers. The starting layers live only under :data:`ENV_DIR`. The rest
are in the target's ``envs/`` directory::

    <stem>_core.txt           starting configuration: the Rust runtime and the Solana platform
    <stem>_anchor.txt         starting configuration: the Anchor framework (inlining only)
    <stem>_<unit>_run.txt     one unit's own directives
    <stem>.txt                generated from the starting layers, named by the package
    <stem>_<unit>.txt         generated from those plus the unit's, named by one unit's conf

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
    The pointer analysis refuses some code, most often [3308] on an Anchor
    ``#[error_code]`` enum formatting its ``#[msg("...")]`` string. Any
    ``require!`` or ``?`` in a handler can reach it, and a rule on that path
    gets no verdict. The remedy is a summary in the unit layer.

    A summary is unsound. It replaces a function with one that returns an
    unconstrained value and writes only the locations the summary names.
    Summarizing the wrong function produces a green rule that checked less
    than it appears to. Every directive carries a ``why``, which the judge and
    the report both get, and adding one invalidates the prover stamp
    (:func:`summary_history`). This module does not check that a summary avoids
    the code under test: a regex cannot be matched against symbols before a
    build. The judge does that, using ``why``.
"""

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from composer.spec.cvlr.env_paths import PathDialect

#: The starting layers, shipped in the wheel. Every composite is built from these.
ENV_DIR = files(__package__) / "envs"


@dataclass(frozen=True)
class StartingLayer:
    """One starting layer of a tuning file."""

    #: The ``<layer>`` in ``<stem>_<layer>.txt``.
    name: str
    #: What the layer's directives cover, as prose. The composite names its layers by this, since
    #: the layer files themselves are not in the target.
    covers: str


@dataclass(frozen=True)
class EnvFamily:
    """One tuning file, split into layers."""

    stem: str
    #: What the file's directives are, as prose.
    kind: str
    #: The starting layers, in composition order, each named ``<stem>_<layer>.txt``.
    layers: tuple[StartingLayer, ...]

    @property
    def starting(self) -> tuple[str, ...]:
        """The starting layers' file names, in the order the composite carries them."""
        return tuple(f"{self.stem}_{layer.name}.txt" for layer in self.layers)

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
        """The file one unit's conf names. Generated from the starting layers and the unit's."""
        return f"{self.stem}_{unit}.txt"


_CORE = StartingLayer("core", "the Rust runtime and the Solana platform")

#: Which calls the Prover analyzes through their bodies. Declared as ``solana_inlining``.
INLINING = EnvFamily(
    "cvlr_inlining",
    kind="Inlining directives",
    layers=(_CORE, StartingLayer("anchor", "the Anchor framework")),
)
#: What the pointer analysis may assume about the memory an opaque call writes. Declared as
#: ``solana_summaries``.
SUMMARIES = EnvFamily("cvlr_summaries", kind="Points-to summaries", layers=(_CORE,))
ENV_FAMILIES = (INLINING, SUMMARIES)

#: The starting layers — one content for every target, as against the per-unit layer.
STARTING_ENVS = tuple(name for f in ENV_FAMILIES for name in f.starting)


_GENERATED_HEADER = """;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;; Generated — this is the file the build reports to the prover.
;;; Rewritten on every run, so an edit made here is lost. Composed,
;;; in order, from AutoProver's starting configuration for:
{layers}
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
"""


def starting_env(name: str, dialect: PathDialect = PathDialect()) -> str:
    """One starting layer, spelled for the target's platform generation.

    The default dialect changes nothing, so a caller comparing against the file on disk gets that
    file.
    """
    return dialect.render((ENV_DIR / name).read_text())


def compose_env(
    family: EnvFamily, *, unit_layer: str | None = None, dialect: PathDialect = PathDialect()
) -> str:
    """The generated composite: header, then the layers, in order.

    The scaffold writes the package-level composite, which has no ``unit_layer``. A unit layer is
    one unit's directives, so they are not applied to another unit's submission (see
    :meth:`EnvFamily.unit_layer`). Those are the project's own symbols, so the dialect leaves them
    alone.
    """
    header = _GENERATED_HEADER.format(
        layers="\n".join(f";;;   {layer.covers}" for layer in family.layers)
    )
    parts = [header]
    if dialect.aliases:
        # Said in the file, so a reader who knows the upstream ``solana_program::`` paths is not
        # surprised to find other crates' paths in their place.
        parts.append(
            f";;; {len(dialect.aliases)} solana_program path prefixes respelled for the "
            f"crates this project resolves\n"
        )
    parts += [starting_env(name, dialect) for name in family.starting]
    if unit_layer is not None:
        parts.append(unit_layer)
    return "\n".join(p.rstrip("\n") for p in parts) + "\n"


#: A summary body. A ``#[type(...)]`` annotation is optional. Omitting it means
#: the return is unconstrained, which is the usual case for a formatting path
#: nothing asserts over.
_TYPE_ANNOTATION = "#[type({body})]"

#: Header of the per-unit layer. The next build rewrites the file from state.
_UNIT_LAYER_HEADER = (
    ";;; Summaries added by the CVLR authoring loop for unit `{unit}`.\n"
    ";;; DO NOT EDIT: rewritten from the run's state on every build."
)


@dataclass(frozen=True)
class SummaryDirective:
    """One symbol pattern the prover should summarize rather than analyze."""

    #: A regex over demangled symbols, spelled as the canonical files spell them.
    #: Anchored with ``^`` and ``$`` by convention, for example
    #: ``^<vault::VaultError as core::fmt::Display>::fmt$``.
    pattern: str
    #: Why analyzing it fails, and why summarizing it is sound for this batch.
    #: Written into the unit's tuning file and the report.
    why: str
    #: The ``#[type(...)]`` body, without the wrapper. ``None`` leaves the return
    #: unconstrained.
    returns: str | None = None

    def render(self) -> str:
        lines = [f"; {line}" for line in self.why.strip().splitlines() or [""]]
        if self.returns is not None:
            lines.append(_TYPE_ANNOTATION.format(body=self.returns))
        lines.append(self.pattern)
        return "\n".join(lines)


def render_layer(header: str, directives: tuple[SummaryDirective, ...]) -> str:
    return "\n\n".join([header.rstrip("\n"), *(d.render() for d in directives)]) + "\n"


@dataclass(frozen=True)
class TuningFiles:
    """Where one unit's tuning files are, and how paths in them are spelled.

    Held on its own, not derived from the harness path. The envs directory and
    the specs directory are siblings by convention, and reconstructing one from
    the other would encode that convention twice.

    ``unit`` names the file this unit's directives land in. Every unit shares
    one working tree. A summary is a regex over symbols that the prover applies
    to the whole build, and a cargo feature cannot scope it. A shared file
    would apply one unit's summaries to another unit's submission. The unit's
    conf names the unit's composite. The project's own declaration in
    ``[package.metadata.certora]`` is left alone.
    """

    envs_dir: Path
    dialect: PathDialect
    #: The harness module name. It is what makes these files this unit's.
    unit: str

    def unit_layer_path(self, family: EnvFamily = SUMMARIES) -> Path:
        """This unit's own layer. Rewritten from state on every build."""
        return self.envs_dir / family.unit_layer(self.unit)

    def composite_path(self, family: EnvFamily = SUMMARIES) -> Path:
        """The composite this unit's conf names."""
        return self.envs_dir / family.unit_composite(self.unit)

    def write(self, directives: tuple[SummaryDirective, ...]) -> None:
        """Rewrite this unit's summaries layer and recompose the file its conf names.

        Rewritten from ``directives``, not appended. The recorded state and the
        file then cannot drift. A run with no directives leaves a layer that is
        only the header, which composes to the starting configuration. Dropping
        a directive from state removes it from disk.

        Called from the build path. The tool that records a directive does not
        write the file. One writer reads state the reducer has already merged.
        A tool writing its own view of the list would race another call and
        drop one of the two.
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

        A summary written to a file the build does not read changes nothing.
        The tool would report success and the next run would hit the same
        [3308].

        The package composites are the scaffold's. The build manifest declares
        them, and inlining still reaches the prover that way. The unit
        summaries composite is what this unit's conf names. :meth:`write`
        creates it, so its absence means no build has staged yet.
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
    """Append summary directives, in order, dropping a repeated pattern.

    Two ``summarize_for_prover`` calls can land in one graph step. LangGraph
    refuses two writes to a field with no reducer. Each call contributes only
    its own directive, so neither has to have seen the other's. A repeated
    pattern keeps the earlier directive.
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
    """The directives as ``version_history`` tokens, so an earlier stamp goes stale.

    Keyed on the pattern and the return type, which is what the prover does
    differently. ``why`` is left out, so rewording a justification does not
    require a new submission.
    """
    return tuple(f"summary:{d.pattern}:{d.returns or ''}" for d in directives)
