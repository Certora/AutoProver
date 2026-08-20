"""The Rust backend's result type (``FormT``) and artifact identifier.

``RustFormalResult`` is a plain pydantic model — the design's rule is that the
result stays Python-native so the driver's type-keyed cache
(``cache_get(formalizer.formalized_type)`` / ``cache_put``) round-trips it
unchanged. It is assembled by the adapter's loop from what the wheel published
(the wheel has no result type of its own: it answers per target, and the host
accumulates), so the only wire types in here are the per-check
:class:`~composer.rustapp.wire.Verdict` and the :class:`~composer.rustapp.wire.Target` objects a
gate run covered. It satisfies both ``FormalResult``
(``artifact_text`` / ``commentary`` / ``property_checks()``) and
``ReportableResult`` (``skipped`` / ``property_checks()`` / ``output_link``)
structurally.
"""

from dataclasses import dataclass

from pydantic import BaseModel, Field

from composer.rustapp.wire import Target, Verdict
from composer.authoring.state import SkippedProperty
from composer.spec.source.report.schema import Outcome
from composer.spec.types import CheckName, PropertyTitle


class RustSetupSpec(BaseModel):
    """The compiled shared setup spec (Crucible's fixture), wrapped for the store.

    It is one string, but the typed cache round-trips pydantic models only — and it earns a cache
    entry: authoring + compiling it is a full LLM loop (on a large program, the longest single step
    of a run), it is authored once for the whole run, and every component builds on it."""

    source: str


def _declared_finding(ran: Verdict, reason: str) -> Verdict:
    """Mark ``ran`` as a declared finding, keeping the run's own ``detail``."""
    if ran.outcome is Outcome.BAD:
        lead = f"DECLARED EXPECTED TO FAIL — the violation below is the finding: {reason}"
    else:
        lead = (
            f"DECLARED EXPECTED TO FAIL — a violation here is the finding: {reason}\n"
            f"NOT REPRODUCED: this run reported {ran.outcome.value}, so the finding rests on the "
            f"author's reading rather than on a counterexample from this run."
        )
    body = f"{lead}\n\n{ran.detail}" if ran.detail else lead
    return ran.model_copy(update={"outcome": Outcome.BAD, "detail": body})


class RustFormalResult(BaseModel):
    """A successful Rust formalization. ``checks`` holds the property→check-names
    map as JSON-friendly lists; ``property_checks()`` re-tuples it for the
    protocols. The field is *not* named ``property_checks`` to avoid clashing with
    that required method."""

    commentary: str = ""
    artifact_text: str = ""
    checks: list[tuple[PropertyTitle, list[CheckName]]] = Field(default_factory=list)
    skipped: list[SkippedProperty] = Field(default_factory=list)
    output_link: str | None = None
    # Per-check verdicts from the wheel (what the run observed). Empty for run-service-backed
    # backends (they use fetch_verdicts). Use ``reported_verdicts`` for what to report.
    verdicts: dict[CheckName, Verdict] = Field(default_factory=dict)
    # Checks the author marked expected-to-fail, name -> why. The wheel never sees these.
    expected_failures: dict[CheckName, str] = Field(default_factory=dict)
    # What the stamping gate run covered: each validation *target* — one invocation of the checker —
    # with the checks it covered, in the order they ran. Several checks may share one target
    # (Crucible puts a component's whole property set in one fuzz target), so this is neither
    # ``checks``' keys nor its values.
    #
    # The names are mirrored into ``finalize``'s outcome set because a callout-mode wheel assembling
    # one deliverable needs the real target names: re-deriving them from a display name would put
    # the slug rule in two languages and smuggle a semantic value through a string. The checks are
    # here so this component's coverage — which checks, and so which properties, these results are
    # about — is answerable even where a whole target errored.
    targets: list[Target] = Field(default_factory=list)

    def property_checks(self) -> list[tuple[PropertyTitle, list[CheckName]]]:
        return [(title, list(names)) for title, names in self.checks]

    def reported_verdicts(self) -> dict[CheckName, Verdict]:
        """Verdicts as the report and console should show them.

        A declared check reports BAD even if this run did not reproduce it, so a
        documented finding cannot show as a pass. The detail says which case it is:
        a reproduced finding keeps the counterexample; an unreproduced one says
        ``NOT REPRODUCED``."""
        return {
            name: (
                _declared_finding(verdict, self.expected_failures[name])
                if name in self.expected_failures
                else verdict
            )
            for name, verdict in self.verdicts.items()
        }

    def display_name(self, check: CheckName) -> str:
        """Row name for one check: the property title when it verifies exactly one, else the check name."""
        titles = self.check_properties().get(check, [])
        return titles[0] if len(titles) == 1 else check

    def check_properties(self) -> dict[CheckName, list[PropertyTitle]]:
        """``checks`` inverted: check name -> the property titles it verifies. For display, where
        the property's own words ("Balance never overflows") read better than the backend's check
        name (``rule_balance_never_overflows``).

        A list, not one title: the mapping is many-to-many, and a check that discharges three
        properties has no single title to be named after. A check absent here has none at all."""
        titles: dict[CheckName, list[PropertyTitle]] = {}
        for title, names in self.checks:
            for name in names:
                titles.setdefault(name, []).append(title)
        return titles


@dataclass(frozen=True)
class RustArtifact:
    """Artifact identifier for a Rust backend — ``{prefix}_{slug}.{extension}``.
    Prefix/extension come from the descriptor's ``ArtifactLayout`` so naming lives
    in one place."""

    slug: str
    prefix: str
    extension: str

    @property
    def stem(self) -> str:
        return f"{self.prefix}_{self.slug}"

    @property
    def artifact_file(self) -> str:
        return f"{self.stem}.{self.extension}"
