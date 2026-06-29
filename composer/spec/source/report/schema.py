"""Datatypes for the autoprove report.

`AutoProverReport` is the top-level document written to ``certora/ap_report/report.json``.
The report is **property-keyed**: a high-level `PropertyGroup` (a "P-NN" heading) groups the
inferred `FormalizedProperty`s it covers, and a `RuleVerdict` may surface under several groups
(rules repeat; properties partition). Rule verdicts speak a backend-agnostic `Outcome` vocabulary
(GOOD/BAD/…); each backend's analysis status maps into it, and the human-facing label for an
outcome ("Verified" vs "Successful test") is a render-time concern, not stored here.

The report is a **per-run snapshot** — no guarantee that property/group slugs stay stable across
runs. Bump `schema_version` on a breaking change.
"""
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from composer.spec.types import PropertyFormulation

type RuleName = str
"""A CVL rule/invariant identifier as it appears in the prover report and in a component's
``property_rules`` mapping."""

type ComponentName = str
"""Human name of an AIComposer component (e.g. "Increment"), or "Structural Invariants"."""

type PropertyTitle = str
"""A property's unique snake_case title — the key in a component's ``property_rules`` mapping."""

type RuleRef = tuple[str, RuleName]
"""A rule's identity: ``(spec_file, name)``. A name is only unique within a spec, so the defining
spec file disambiguates a rule re-stated under the same name in another spec (and collapses a single
definition — e.g. an imported structural invariant — seen through several component runs)."""

type PropertyKey = tuple[ComponentName, PropertyTitle]
"""A property's identity: ``(component, title)`` — the cross-reference key groups use for members."""


class Outcome(str, Enum):
    """Backend-agnostic per-unit (rule / test) result. Each backend's native analysis status maps
    into this small vocabulary; the human-facing label ("Verified" for a prover rule, "Successful
    test" for a forge run) is supplied at render time, so the data model stays backend-neutral.

      - GOOD    — the property holds (prover: VERIFIED; foundry: a passing test)
      - BAD     — the property is violated (prover: VIOLATED; foundry: an expected-failure test)
      - ERROR   — the run errored out without a verdict
      - TIMEOUT — the run timed out without a verdict
      - UNKNOWN — no conclusive result

    A finalized report never carries RUNNING/PENDING — those fold into UNKNOWN at collection.
    """
    GOOD = "GOOD"
    BAD = "BAD"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


class GroupStatus(str, Enum):
    """Aggregated outcome for a `PropertyGroup`, rolled up from the `Outcome` of the rules its
    member properties are formalized by (see :func:`grouping.aggregate_status`):

      - GOOD    — every contributing rule is GOOD
      - BAD     — any contributing rule is BAD
      - PARTIAL — some GOOD, some not-yet-GOOD (but none BAD)
      - UNKNOWN — none GOOD, none BAD (all ERROR/TIMEOUT/UNKNOWN)
    """
    GOOD = "GOOD"
    BAD = "BAD"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class RuleVerdict(BaseModel):
    """One CVL rule/invariant (or foundry test) and its outcome — the verdict table the report
    references by `RuleRef`. Stored once even when properties across several groups are formalized
    by it, so a shared rule carries a single consistent outcome/link. ``outcome``/``line``/
    ``duration_seconds`` come from the backend's per-unit result."""
    name: RuleName
    spec_file: str = Field(
        description="Basename of the spec defining this rule; with `name` it is the rule's identity.",
    )
    outcome: Outcome = Outcome.UNKNOWN
    line: int | None = None
    duration_seconds: float | None = None
    prover_link: str | None = None

    @property
    def ref(self) -> RuleRef:
        """This rule's identity ``(spec_file, name)`` — the key properties reference it by."""
        return (self.spec_file, self.name)


class FormalizedProperty(PropertyFormulation):
    """An inferred property (title, methods, sort, description) that at least one CVL rule
    formalizes, tagged with its owning component. ``rule_refs`` are the property→rule edges; the
    render layer labels each edge with this property's ``description``. Distinct from a
    `PropertyGroup`, which is the audit-level grouping of several such properties."""
    component: ComponentName = Field(description="The AIComposer component that owns this property.")
    rule_refs: list[RuleRef] = Field(
        default_factory=list,
        description="Identities of the rules that (jointly) formalize this property.",
    )

    @property
    def key(self) -> PropertyKey:
        """This property's identity ``(component, title)`` — how groups reference it."""
        return (self.component, self.title)


class SkippedClaim(PropertyFormulation):
    """A formalization gap: an inferred property the author deliberately declined to formalize, with
    the recorded reason. The component's generation otherwise succeeded."""
    component: ComponentName
    reason: str = Field(description="Why the author skipped formalizing this property.")


class GaveUpComponent(BaseModel):
    """A formalization gap at component granularity: the component's CVL generation gave up (or
    crashed), so none of its inferred properties were formalized. No per-property reason."""
    component: ComponentName
    properties: list[PropertyFormulation]


class PropertyGroup(BaseModel):
    """An audit-level "P-NN" heading: a synthesized claim over a set of `FormalizedProperty`s (its
    ``members``, by identity). Members partition — each property belongs to exactly one group —
    while a rule may surface under several groups via those members' ``rule_refs``. Identified by
    its kebab-case ``slug``."""
    slug: str = Field(..., min_length=1, max_length=64)
    title: str
    description: str
    status: GroupStatus
    members: list[PropertyKey]


class CoverageReport(BaseModel):
    """Validation outcomes after grouping (see :func:`coverage.validate`)."""
    total_properties: int
    total_rules: int
    total_groups: int
    properties_per_group_min: int
    properties_per_group_max: int
    property_coverage_complete: bool
    properties_in_no_group: list[PropertyKey] = Field(default_factory=list)
    #: rules whose properties span >1 group — expected (rules repeat), reported as a stat not an error
    rules_spanning_multiple_groups: list[RuleName] = Field(default_factory=list)
    skipped_count: int = 0
    gave_up_component_count: int = 0
    dropped_orphan_rules: int = 0
    warnings: list[str] = Field(default_factory=list)


type ReportBackend = Literal["prover", "foundry"]
"""Which pipeline produced this report. Provenance only — every backend fills the same fields;
this tag just lets the renderer pick the right outcome labels ("Verified" vs "Successful test")
for a report.json it reads cold."""


class AutoProverReport(BaseModel):
    """Top-level report document — written to ``certora/ap_report/report.json``."""
    schema_version: Literal["3.0"] = "3.0"
    backend: ReportBackend = "prover"
    contract_name: str
    run_timestamp_utc: str | None = None
    #: component name (or "Structural Invariants") -> prover run link/path
    prover_links: dict[ComponentName, str] = Field(default_factory=dict)
    properties: list[FormalizedProperty]
    rules: list[RuleVerdict]
    groups: list[PropertyGroup]
    #: Formalization gaps — properties that exist but no rule formalizes (see the two gap types).
    skipped: list[SkippedClaim] = Field(default_factory=list)
    gave_up_components: list[GaveUpComponent] = Field(default_factory=list)
    coverage: CoverageReport
