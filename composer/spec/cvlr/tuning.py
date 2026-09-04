"""Points-to summaries the authoring loop adds, and what they cost.

``docs/cvlr-backend-plan.md`` §7.6. The Solana Prover's pointer analysis refuses some code outright
— most commonly [3308] on an Anchor program's own ``#[error_code]`` enum formatting its
``#[msg("...")]`` string, which every ``require!`` and every ``?`` in a handler can reach. A rule
that touches such a path gets no verdict at all, and the remedy the prover expects is a *summary*:
tell it to replace the offending function with an unconstrained stand-in instead of analyzing it.

Summaries live in ``cvlr_summaries_package.txt``, the one layer of the tuning files that belongs to
the project (:class:`composer.spec.cvlr.scaffold.EnvFamily`). Before this module they were written
once, by the scaffold, and nothing could add to them afterwards — so an authoring loop that hit
[3308] had no move that worked. An end-to-end run found that out the hard way and said so in a
harness header: *"the only writing tool available is put_harness (writes this module only)"*, having
first worked out the exact directive it needed and then written a reimplementation of the handler
instead. Two of three units gave up.

**A summary is unsound, and that is the whole design problem here.** It replaces a function with
"anything could have happened", so summarizing the wrong thing does not fail — it produces a green
rule that checked less than it appears to, with nothing in the harness source to show for it. That
is strictly worse than the reimplementation it replaces, which at least left a mirror function
somebody could see. So:

* every directive carries a ``why``, and both the judge and the report get the list;
* adding one **invalidates the prover stamp**, via the ``version_history`` channel
  :func:`composer.authoring.state.spec_digest` already has for exactly this ("a stamp earned before
  a source edit goes stale with it") — the previous run's verdicts were not about this build;
* the composite is rewritten from the canonical layers each time, so a directive can be added
  without any accumulated file becoming the source of truth.

What is deliberately *not* here is a check that a summary does not cover the code under test. It is
the failure that matters — summarizing the handler a rule drives makes the rule vacuous in a way no
verdict reveals — and a regex cannot be matched against symbols this side of a build. It is the
judge's, under its first checklist item, with the declared ``why`` as the evidence.
"""

import dataclasses
from pathlib import Path

from composer.spec.cvlr.env_paths import PathDialect
from composer.spec.cvlr.scaffold import ENV_FAMILIES, SUMMARIES, EnvFamily, compose_env

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


@dataclasses.dataclass(frozen=True)
class SummaryDirective:
    """One symbol pattern the prover should summarize rather than analyze."""

    #: A regex over demangled symbols, the way the canonical files spell them — anchored with
    #: ``^``/``$`` by convention, e.g. ``^<vault::VaultError as core::fmt::Display>::fmt$``.
    pattern: str
    #: Why analyzing it fails and why summarizing it is sound for the properties in this batch. Ends
    #: up in the project's tuning file and in the report; it is the only account anybody gets.
    why: str
    #: The ``#[type(...)]`` body, without the wrapper — ``None`` for an unconstrained return.
    returns: str | None = None

    def render(self) -> str:
        lines = [f"; {line}" for line in self.why.strip().splitlines() or [""]]
        if self.returns is not None:
            lines.append(_TYPE_ANNOTATION.format(body=self.returns))
        lines.append(self.pattern)
        return "\n".join(lines)


def package_layer(header: str, directives: tuple[SummaryDirective, ...]) -> str:
    """The project's own summary layer: the scaffold's header, then one block per directive."""
    return "\n\n".join([header.rstrip("\n"), *(d.render() for d in directives)]) + "\n"


@dataclasses.dataclass(frozen=True)
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

    def _package_layer_path(self, family: EnvFamily) -> Path:
        return self.envs_dir / family.package

    def unit_layer_path(self, family: EnvFamily = SUMMARIES) -> Path:
        """This unit's own layer — machine-owned, rewritten from state on every build."""
        return self.envs_dir / family.unit_layer(self.unit)

    def composite_path(self, family: EnvFamily = SUMMARIES) -> Path:
        """The composite this unit's conf names."""
        return self.envs_dir / family.unit_composite(self.unit)

    def read_header(self, family: EnvFamily = SUMMARIES) -> str:
        """The comment header the scaffold wrote, so rewriting the layer preserves it.

        Everything up to the first blank line. The scaffold's header is a two-line comment and the
        directives that follow are separated by blank lines, so this reads back what was written
        without the layer's format having to be parsed.
        """
        existing = self._package_layer_path(family)
        if not existing.is_file():
            return ""
        return existing.read_text().split("\n\n", 1)[0]

    def read_package_layer(self, family: EnvFamily = SUMMARIES) -> str:
        """The project's own layer, unchanged. Empty when the scaffold has not written one."""
        existing = self._package_layer_path(family)
        return existing.read_text() if existing.is_file() else ""

    def write(self, directives: tuple[SummaryDirective, ...]) -> None:
        """Rewrite this unit's summaries layer and recompose the file its conf names.

        Rewritten wholesale from ``directives`` rather than appended to, so the state the run
        recorded and the file on disk cannot drift — and a run that ends up with no directives
        leaves a layer with nothing in it but the header, which composes to the project's own
        content. That is what makes this a *derived* file in the sense
        ``docs/single-working-tree.md`` §4 needs: dropping a directive from state removes it from
        disk, where an appending writer would have kept it forever.

        The project's ``_package.txt`` is **not** touched. It is the project's file; this run's
        directives are the run's, and mixing them is how a unit's summary outlives the unit.

        Called from the build path rather than from the tool that records a directive. One writer,
        reading state the reducer has already merged: a tool writing its own view of the list would
        race a concurrent sibling and drop one of the two, which is the same bug as the unreduced
        state key and would not have raised.
        """
        header = _UNIT_LAYER_HEADER.format(unit=self.unit)
        layer = package_layer(header, directives)
        self.envs_dir.mkdir(parents=True, exist_ok=True)
        self.unit_layer_path(SUMMARIES).write_text(layer)
        self.composite_path(SUMMARIES).write_text(
            compose_env(
                SUMMARIES,
                package_layer=self.read_package_layer(SUMMARIES),
                unit_layer=layer,
                dialect=self.dialect,
            )
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
