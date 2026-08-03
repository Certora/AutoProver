"""The Rust backend's result type (``FormT``) and artifact identifier.

``RustFormalResult`` is a plain pydantic model — the design's rule is that the
result stays Python-native so the driver's type-keyed cache
(``cache_get(formalizer.formalized_type)`` / ``cache_put``) round-trips it
unchanged. Rust returns its fields as JSON (a ``Formalized``); the adapter
validates them into this model. It satisfies both ``FormalResult``
(``artifact_text`` / ``commentary`` / ``property_units()``) and
``ReportableResult`` (``skipped`` / ``property_units()`` / ``output_link``)
structurally.
"""

from dataclasses import dataclass

from pydantic import BaseModel, Field

from composer.spec.cvl_generation import SkippedProperty


class RustSetupArtifact(BaseModel):
    """The compiled shared setup artifact (Crucible's fixture), wrapped for the store.

    It is one string, but the typed cache round-trips pydantic models only — and it earns a cache
    entry: authoring + compiling it is a full LLM loop (on a large program, the longest single step
    of a run), it is authored once for the whole run, and every component builds on it."""

    source: str


class RustFormalResult(BaseModel):
    """A successful Rust formalization. ``units`` holds the property→unit-names
    map as JSON-friendly lists; ``property_units()`` re-tuples it for the
    protocols. The field is *not* named ``property_units`` to avoid clashing with
    that required method."""

    commentary: str = ""
    artifact_text: str = ""
    units: list[tuple[str, list[str]]] = Field(default_factory=list)
    skipped: list[SkippedProperty] = Field(default_factory=list)
    output_link: str | None = None
    # Per-unit verdicts baked in at formalize time by a self-contained backend
    # (unit name -> the Rust ``Verdict`` dict: {outcome, line?, duration_seconds?,
    # unit_file?}). Empty for run-service-backed backends (they use fetch_verdicts).
    verdicts: dict[str, dict] = Field(default_factory=dict)
    # The distinct validation *targets* this unit's report rows were checked by — the wheel's
    # ``Unit.target``s, in the order they ran. Several report rows may share one target (Crucible
    # puts a component's whole property set in one fuzz target), so this is not ``units``' keys.
    # Mirrored into ``finalize``'s outcome set because a callout-mode wheel assembling one
    # deliverable needs the real target names: re-deriving them from a display name would put the
    # slug rule in two languages and smuggle a semantic value through a string.
    targets: list[str] = Field(default_factory=list)

    def property_units(self) -> list[tuple[str, list[str]]]:
        return [(title, list(names)) for title, names in self.units]

    def unit_titles(self) -> dict[str, str]:
        """``units`` inverted: unit name -> the property title it checks. For display, where the
        property's own words ("Balance never overflows") read better than the backend's unit name
        (``rule_balance_never_overflows``). A unit absent here has no title to show."""
        return {unit: title for title, names in self.units for unit in names}

    @classmethod
    def from_formalized(cls, formalized: dict) -> "RustFormalResult":
        """Build from a Rust ``Formalized`` dict (the payload of ``Command::Publish``)."""
        return cls(
            commentary=formalized.get("commentary", ""),
            artifact_text=formalized.get("artifact_text", ""),
            units=[(t, list(u)) for t, u in formalized.get("property_units", [])],
            skipped=[SkippedProperty(**s) for s in formalized.get("skipped", [])],
            output_link=formalized.get("output_link"),
            verdicts=dict(formalized.get("verdicts", {})),
        )


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
