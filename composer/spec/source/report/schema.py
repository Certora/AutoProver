"""Datatypes for the autoprove report.

`AutoProverReport` is the top-level document written to ``certora/ap_report/report.json``.
The report is **property-keyed**: a high-level `PropertyGroup` (a "P-NN" heading) groups the
inferred `FormalizedProperty`s it covers, and a `RuleVerdict` may surface under several groups
(rules repeat; properties partition). Rule verdicts speak a backend-agnostic `Outcome` vocabulary
(GOOD/BAD/…); each backend's analysis status maps into it, and the human-facing label for an
outcome ("Verified" vs "Successful test") is a render-time concern, not stored here.

The report is a **per-run snapshot** — no guarantee that property/group slugs stay stable across
runs. Bump `schema_version` on a breaking change. A minor bump (3.1 -> 3.2) adds optional fields
only, so a reader of the new version still loads every older report.
"""
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from composer.spec.types import (
    ComponentName, PropertyFormulation, PropertyKey, PropertyTitle, PropertyType, RuleName,
)

type RuleRef = tuple[str, RuleName]
"""A rule's identity: ``(spec_file, name)``. A name is only unique within a spec, so the defining
spec file disambiguates a rule re-stated under the same name in another spec — two components that
each author a supporting invariant of the same name stay distinct — while collapsing a single
definition seen through several runs."""

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

    @classmethod
    def parse(cls, raw: str) -> "Outcome | None":
        """``raw`` as an outcome, or ``None`` when it names none.

        For values arriving from outside this repo — a backend wheel's JSON, a ``report.json`` read
        cold — where an unrecognized label is version skew rather than a bug, and the caller decides
        what to do about it (record UNKNOWN, drop a glyph). For a value we produced ourselves,
        ``Outcome(raw)`` and its ValueError remain the right thing."""
        try:
            return cls(raw)
        except ValueError:
            return None


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
    message: str | None = Field(
        default=None,
        description="Human-readable explanation of a non-GOOD outcome (e.g. the fuzzer's "
        "counterexample / failed-assertion message). Diagnostics only; may be absent.",
    )

    @property
    def ref(self) -> RuleRef:
        """This rule's identity ``(spec_file, name)`` — the key properties reference it by."""
        return (self.spec_file, self.name)


type CoverageStatus = Literal["covered", "partial", "uncovered"]

type FaithfulnessStatus = Literal["exact", "partial", "wrong"]


class PropertyFaithfulness(BaseModel):
    """A judgement of whether the rules mapped to a property SEMANTICALLY encode its claim,
    independent of how much they cover. Coverage asks whether the mapped set exercises the whole
    property; faithfulness asks whether each rule's CVL actually asserts what the property says —
    the exact quantity, the revert/no-revert direction, non-vacuously. A rule can be faithful but
    cover little, or cover everything yet assert the wrong thing. Written by the evaluate run when
    faithfulness judging is enabled; absent otherwise."""
    status: FaithfulnessStatus = Field(
        description="exact = the mapped rules jointly assert exactly the claim; partial = they "
        "assert only part of it, or a part is asserted incorrectly with no other rule covering "
        "it; wrong = they do not assert the claim at all.")
    note: str = Field(description="One to three sentences: what the rules faithfully encode and "
                      "where they diverge from the claim.")
    vacuity_risk: bool = Field(
        default=False,
        description="True when a mapped rule may pass vacuously (its assertion is unreachable or "
        "trivially true), so a verified verdict would not actually establish the claim.")


class PropertyCoverage(BaseModel):
    """A judgement of how well the rules a property maps to establish it. Written by a run that
    checks specs it did not author (``run_mode="evaluate"``); absent otherwise."""
    status: CoverageStatus
    reason: str = Field(description="Why the rules establish the property fully, partly, or not at all.")
    added_by_evaluator: bool = Field(
        default=False,
        description="True when the property was added during evaluation rather than inferred "
        "with the rest of the property list.",
    )
    faithfulness: PropertyFaithfulness | None = Field(
        default=None,
        description="The semantic-faithfulness judgement of the mapped rules, when faithfulness "
        "judging ran. A `wrong`/`partial` faithfulness lowers the coverage `status`.",
    )


type SceneConcern = Literal[
    "sound", "over_approximation", "under_approximation", "vacuity_risk",
    "unresolved_or_havoc", "wrong_resolution",
]
type SceneEffect = Literal["false_proof", "false_violation", "vacuity", "timeout_or_cost", "none"]


class SceneFinding(BaseModel):
    """One scene-modelling decision (a summary, link, dispatch choice, or ghost) and its
    soundness consequence. The scene is everything that stands in for code outside the verified
    contracts; an unsound choice can let a rule reach a verdict it does not deserve."""
    target: str = Field(description="The specific summary / link / dispatch / ghost at issue, named concretely.")
    concern: SceneConcern = Field(description=(
        "sound = meaning-preserving or a safe over-approx for the sorts that use it; "
        "over_approximation = adds behaviour; under_approximation = removes reachable behaviour; "
        "vacuity_risk = assumes away a rule's premise; unresolved_or_havoc = a depended-on call is "
        "havocked, a risk when that call is expected to have a real effect on the rule's correctness; "
        "wrong_resolution = linked/dispatched to the wrong implementation."))
    effect: SceneEffect = Field(description=(
        "false_proof = a rule could be VERIFIED that should not be (the dangerous case); "
        "false_violation = a spurious counterexample; vacuity = a rule passes proving nothing; "
        "timeout_or_cost = a tractability risk only; none = no soundness consequence."))
    properties_at_risk: list[str] = Field(
        default_factory=list,
        description="Titles of the properties this choice endangers, given their sort.")
    note: str = Field(description="What the scene construct actually does and why it has this effect.")


class SceneAssessment(BaseModel):
    """The soundness of the scene the specs were verified against. Written by the evaluate run
    when scene judging is enabled; absent otherwise."""
    overall: Literal["sound", "minor_concerns", "unsound"] = Field(
        default="sound",
        description="unsound if any finding is false_proof; minor_concerns if only "
        "false_violation / vacuity / timeout; else sound.")
    summary: str = Field(default="", description="A short plain-English verdict on the scene.")
    findings: list[SceneFinding] = Field(default_factory=list)
    false_proof_findings: int = Field(
        default=0, description="How many findings have effect=false_proof.")
    at_risk_properties: list[str] = Field(
        default_factory=list,
        description="Titles of the properties endangered by a false-proof finding.")
    tractability: list["TractabilityFinding"] = Field(
        default_factory=list,
        description="Predicted prover-friendliness of the scene's constructs, with proposed "
        "improvements. A relative, structural read, independent of soundness — useful even when "
        "the Prover already ran, except where every run was already fast.")
    tractability_note: str = Field(
        default="", description="Overall prover-friendliness of the scene and its main bottleneck.")


type CostRisk = Literal["cheap", "moderate", "expensive"]


class TractabilityFinding(BaseModel):
    """One scene construct and its predicted SMT cost, independent of soundness (a sound scene can
    be expensive and an unsound one cheap). `suggestion` proposes a scene/settings change that
    would make it cheaper without changing the verdicts — the structural half of improving a slow
    run; actually applying and re-running is not done here."""
    target: str = Field(description="The scene construct, named concretely (a summary, ghost, dispatch set, math).")
    cost_risk: CostRisk = Field(description=(
        "cheap = little added solver work (NONDET, PER_CALLEE_CONSTANT / CONSTANT, a simple "
        "symbolic constant, an uninterpreted ghost, an envfree view summary); moderate = keyed "
        "ghost state with simple reads, a small bounded dispatch set, light linear arithmetic; "
        "expensive = HAVOC of large/complex state, un-summarized nonlinear math (mulDiv, division, "
        "exponentiation), precise bitwise or hashing/keccak models, unbounded loops / deep "
        "recursion, large dispatch sets, heavy quantified ghost axioms."))
    note: str = Field(description="Why the construct has this cost.")
    suggestion: str = Field(
        default="", description="A concrete scene/settings change to make it cheaper WITHOUT "
        "changing the rules' verdicts. Empty when the construct is already cheap.")


type MutantClass = Literal["scene_assumption", "border_callsite", "syntactic"]
# killed = a rule mapped to the property that was VERIFIED on the original flipped to VIOLATED on the
# mutant; survived = all such rules still VERIFIED (a spec blind spot); no_signal = no rule mapped to
# the property was verified on the original, so the mutant is untestable; inconclusive = a rule
# errored/timed out.
type MutantStatus = Literal["not_run", "killed", "survived", "no_signal", "inconclusive"]


class Mutant(BaseModel):
    """A minimal, source-level bug injected into the MAIN contract to violate one property, with
    the author's scenario and the judge's stamp. The runner later applies it and records whether
    the property's rules caught it."""
    target_property: str = Field(description="Title of the property the mutant is meant to violate.")
    mutant_class: MutantClass = Field(description=(
        "scene_assumption = alters a value/behaviour a verifier might unsoundly treat as fixed; "
        "border_callsite = changes how the main contract handles a value crossing an external "
        "boundary; syntactic = a trivial operator/constant tweak."))
    old_str: str = Field(description="The exact snippet replaced in the main contract.")
    new_str: str = Field(description="The replacement that introduces the bug.")
    scenario: str = Field(description="The concrete, reachable execution under which the mutated "
                          "contract violates the property while the original satisfies it.")
    rationale: str = Field(description="One line: what assumption/behaviour this breaks.")
    accepted: bool = Field(default=False, description="Whether the judge accepted the mutant.")
    is_scene_assumption: bool = Field(
        default=False, description="The judge's read that this targets an unsound scene assumption.")
    review_reason: str = Field(default="", description="The judge's reason (esp. for a rejection).")
    #: --- filled by the runner (Tool 2) ---
    status: MutantStatus = Field(default="not_run", description="The kill-test outcome.")
    killed_by: list[str] = Field(
        default_factory=list,
        description="Rules mapped to the property that flipped to VIOLATED on the mutant.")
    run_note: str = Field(default="", description="Why inconclusive/no_signal, when so.")


class MutationReport(BaseModel):
    """Mutation testing of the specs: one or more source-level bugs per property, and whether the
    property's rules catch them. A SURVIVED mutant is a spec blind spot; the score is the rules'
    empirical catch-rate over the mutants that could be tested."""
    author_model: str = ""
    judge_model: str = ""
    n_properties: int = 0
    mutants: list[Mutant] = Field(default_factory=list)
    covered: int = Field(default=0, description="Properties with at least one accepted mutant.")
    #: --- filled by the runner (Tool 2) ---
    killed: int = 0
    survived: int = 0
    no_signal: int = 0
    inconclusive: int = 0
    mutation_score: float | None = Field(
        default=None, description="killed / (killed + survived); None when nothing was testable.")


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
    coverage: PropertyCoverage | None = None

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


class DraftedProperty(PropertyFormulation):
    """A curtailed component's property the author claims to have encoded, with the units it
    named at publish. Unverified: the publish gates were lifted, so the claim was never checked
    against a judge or a verification run."""
    units: list[RuleName] = Field(
        default_factory=list,
        description="The rule/test names the author declared for this property — an unchecked claim.",
    )


class CurtailedSkip(PropertyFormulation):
    """A curtailed component's property the author explicitly skipped (typically citing the
    budget) before publishing what remained."""
    reason: str


class CurtailedComponent(BaseModel):
    """A component whose formalization the run budget cut short. Whatever it published was
    accepted with the validation gates lifted, so neither the encoding nor any verification
    result is reliable: the component contributes nothing to ``properties``/``rules``/``groups``
    and is reported in the budget appendix instead. Its inferred properties are partitioned by
    disposition: ``drafted`` (claimed encoded, unverified), ``skipped`` (explicitly declined,
    with reason), ``unattempted`` (never reached)."""
    component: ComponentName
    #: Project-relative path of the quarantined partial encoding; None when the run was cut off
    #: before anything was published.
    artifact: str | None = None
    #: Last verification-run link the partial result carried, if any (context only — its
    #: outcome predates the final encoding and proves nothing about it).
    run_link: str | None = None
    #: Optional account of the termination (hard-stop message / the author's own words).
    detail: str | None = None
    drafted: list[DraftedProperty] = Field(default_factory=list)
    skipped: list[CurtailedSkip] = Field(default_factory=list)
    unattempted: list[PropertyFormulation] = Field(default_factory=list)


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
    #: Properties this run inferred and then chose not to pursue — non-zero only in a
    #: prioritized run, where formalization is deliberately narrowed to one focus. Read it
    #: alongside ``total_properties``, which counts only what reached the groupings.
    deprioritized_count: int = 0
    total_rules: int
    total_groups: int
    properties_per_group_min: int
    properties_per_group_max: int
    property_coverage_complete: bool
    """Every property that reached formalization landed in a group. It says nothing about
    whether every property the run *inferred* was pursued — a prioritized run drops most of
    them before this point, and reports that in ``deprioritized_count``."""
    properties_in_no_group: list[PropertyKey] = Field(default_factory=list)
    #: rules whose properties span >1 group — expected (rules repeat), reported as a stat not an error
    rules_spanning_multiple_groups: list[RuleName] = Field(default_factory=list)
    skipped_count: int = 0
    gave_up_component_count: int = 0
    curtailed_component_count: int = 0
    dropped_orphan_rules: int = 0
    warnings: list[str] = Field(default_factory=list)


class AppliedEditRecord(BaseModel):
    """Provenance of one applied source edit, as the editor reported it. Duplicates the
    workflow-internal ``AppliedEdit`` shape on purpose: report.json is a persisted contract and
    must not alias a model that is free to refactor."""
    edit_id: str
    executive_summary: str
    why_sound: str


class SourceEditRecord(BaseModel):
    """The source modifications one component's verification ran against: the editor's per-edit
    account in application order, plus the cumulative unified diff from the on-disk baseline to the
    proved source. Its presence means the component's outcomes are claims about the modified code,
    not the code as shipped."""
    component: ComponentName
    applied_edits: list[AppliedEditRecord]
    cumulative_diff: str


class VerificationArtifactRecord(BaseModel):
    """A verification-supporting file a plugin's tool produced during one component's
    formalization — a Lean proof discharging instrumented lemmas, an auxiliary
    certificate, etc. The content lives on disk at ``path`` (project-relative, written
    by the artifact store); the record carries provenance and a description for the
    deliverable. Deliberately its own model: report.json is a persisted contract."""
    component: ComponentName
    #: The contributing plugin's id (its entry-point name).
    plugin: str
    #: File basename as registered by the tool.
    name: str
    #: Open vocabulary, e.g. "lean-proof".
    kind: str
    description: str
    path: str


type ReportBackend = Literal["prover", "foundry", "none"]
"""Which pipeline produced this report. Provenance only — every backend fills the same fields;
this tag just lets the renderer pick the right outcome labels ("Verified" vs "Successful test"
vs "Unverified") for a report.json it reads cold. The producers are the CVL prover (``"prover"``),
Foundry (``"foundry"``), and ``"none"`` — a pipeline that records properties without verifying them
(the analysis-only null backend, ``composer.spec.solana.null_backend``), whose reports are all
UNKNOWN and say so. The set is closed: every backend lives in this repo, so a verification backend
adds its own literal here, plus its wording in ``report/render.py``."""


# ---------------------------------------------------------------------------
# Findings — violated rules surfaced as audit issues.
#
# A `Finding` records a violated rule (a `RuleVerdict` with ``outcome == Outcome.BAD``) as an audit
# issue: a ``title``, a ``severity``, and the ``content`` write-up, synthesized from the rule's
# counterexample analysis and the properties/groups it breaks (see ``report/findings.py``). The field
# set follows the "Submit Issue" body of the Sherlock Audit Engine API — schema at
# https://api-audit-engine.sherlock.xyz/v1/docs/public — so a finding maps cleanly onto a submission,
# but the source ``locations`` a submission needs
# (``{owner}/{repo}`` / file / line) are NOT produced here: a run knows only local paths and CVL-spec
# lines, so the submission layer reconstructs locations from the engagement scope + the counterexample.
# The report-time locator is on ``provenance`` (rule name, spec file, prover-run link).
# ---------------------------------------------------------------------------

type SeverityTier = Literal["critical", "high", "medium", "low", "informational"]
type ImpactLevel = Literal["high", "medium", "low", "none"]
"""Impact axis. ``none`` marks a property break with no real-world consequence (a specification or
code-quality observation); it resolves to ``informational`` severity."""
type LikelihoodLevel = Literal["high", "medium", "low"]


class AuthoredContent(BaseModel):
    """The written sections of a finding. These are what the findings LLM produces, so the field
    descriptions double as its instructions."""
    summary: str = Field(description="A one to three sentence summary of the finding.")
    description: str = Field(description="The full technical explanation of the vulnerability and how it manifests, grounded in the counterexample.")
    impact: str = Field(description="The concrete consequence if the issue is exploited — funds at risk, denial of service, data exposure, and so on.")
    attack_path: str | None = Field(default=None, description="The step-by-step path from the counterexample that triggers the issue, when one applies.")
    assumptions_and_uncertainties: str | None = Field(default=None, description="Assumptions the finding relies on, and anything you are uncertain about.")


class IssueContent(AuthoredContent):
    """A finding's ``content``: the authored sections plus the evidence the report attaches."""
    proof_of_concept: str | None = None
    references: list[str] | None = None


class FindingProvenance(BaseModel):
    """Report-only trace from a finding back to the verdict that produced it (not part of a
    submission payload)."""
    rule_name: RuleName
    spec_file: str
    outcome: Outcome
    group_slugs: list[str] = Field(default_factory=list)
    prover_link: str | None = None
    impact: ImpactLevel | None = None
    likelihood: LikelihoodLevel | None = None
    #: The findings LLM's justification for the assessed impact and likelihood.
    risk_reasoning: str | None = None


class Finding(BaseModel):
    """A violated rule rendered as an audit issue. ``severity`` is computed from the assessed impact
    and likelihood (both recorded on ``provenance``); ``content`` holds the write-up. Source
    ``locations`` are added by the submission layer, not stored here."""
    title: str
    severity: SeverityTier
    content: IssueContent
    provenance: FindingProvenance | None = None


type FeedbackSeverity = Literal["info", "warning", "error"]


class RuleFeedback(BaseModel):
    """A review remark on one rule as written: what is wrong or weak about it and how to fix it.

    About the rule's text or its conf, not its verdict, so a VERIFIED rule can carry one. A rule
    no property claims (an orphan, see ``AutoProverReport.rules``) can carry one as well."""
    rule: RuleName
    #: Basename of the spec defining the rule; with ``rule`` it is the rule's identity. ``None``
    #: when the remark is about a rule name across every spec that declares it.
    spec_file: str | None = None
    #: Project-relative path of the conf the remark is about, or ``None`` when it holds for every
    #: conf that checks the rule.
    conf: str | None = None
    severity: FeedbackSeverity
    #: Open vocabulary, e.g. ``"vacuous"`` or ``"tolerance-accepts-behaviour"``.
    kind: str
    explanation: str
    suggested_fix: str | None = None


class ConfCoverage(BaseModel):
    """Whether the confs that verify one spec, taken together, check every rule of it on every
    method. A gap is closed by a completing run, and this records what was missing before it."""
    spec: str = Field(description="Project-relative path of the spec.")
    confs: list[str] = Field(
        default_factory=list, description="Project-relative paths of the confs that verify the spec.",
    )
    rules_unchecked_before_completion: list[RuleName] = Field(
        default_factory=list,
        description="Rules no conf checked, before any completing run.",
    )
    methods_excluded: dict[RuleName, list[str]] = Field(
        default_factory=dict,
        description="Per parametric rule, the methods no conf instantiated it on, before any "
        "completing run.",
    )
    completion_conf: str | None = Field(
        default=None,
        description="Project-relative path of the conf written to close the gap, if one was needed.",
    )
    complete: bool = Field(
        description="Every rule was checked on every method once the completing run (if any) finished.",
    )


class SpecCoverageScore(BaseModel):
    """A named coverage score with its per-view breakdown, e.g. a static measure of how much of
    the code the specs constrain. The producer names the score and its views."""
    name: str
    score: float
    views: dict[str, float] = Field(default_factory=dict)


class DeprioritizedProperty(PropertyFormulation):
    """A property this run inferred and deliberately did not pursue, with the ranker's reason.

    Present only for a prioritized run. The point of recording them is that a prioritized
    report otherwise looks exactly like a contract with very few properties: this is the list
    that says what was set aside, and why."""
    component: ComponentName
    score: int
    rationale: str


class AutoProverReport(BaseModel):
    """Top-level report document — written to ``certora/ap_report/report.json``."""
    schema_version: Literal["3.0", "3.1", "3.2"] = "3.2"
    backend: ReportBackend = "prover"
    #: How much of the inferred property set the run pursued ("comprehensive", "prioritized", or
    #: "evaluate" for a run that checked the project's own specs). Absent on reports written
    #: before the mode existed, which are all comprehensive.
    run_mode: str | None = None
    #: What a prioritized run set aside. Empty for a comprehensive run.
    deprioritized: list[DeprioritizedProperty] = Field(default_factory=list)
    #: Plugins active for the run, as the manifest that feeds every per-component cache key.
    #: Without it a finished run leaves no record of which plugins participated, and whether an
    #: installed plugin was even loaded can only be inferred from the container image. Empty for
    #: a run with no plugins, and on reports written before the field existed.
    active_plugins: list[str] = Field(default_factory=list)
    contract_name: str
    run_timestamp_utc: str | None = None
    #: component name -> prover run link/path
    prover_links: dict[ComponentName, str] = Field(default_factory=dict)
    properties: list[FormalizedProperty]
    #: Every rule the report shows. A formalizing run keeps only rules some property maps to and
    #: counts the rest in ``coverage.dropped_orphan_rules``. An evaluate run keeps them all, so
    #: a rule here may have no property pointing at it.
    rules: list[RuleVerdict]
    groups: list[PropertyGroup]
    #: Formalization gaps — properties that exist but no rule formalizes (see the two gap types).
    skipped: list[SkippedClaim] = Field(default_factory=list)
    gave_up_components: list[GaveUpComponent] = Field(default_factory=list)
    #: Components the run budget cut short — excluded from every table above, rendered as an
    #: appendix.
    curtailed_components: list[CurtailedComponent] = Field(default_factory=list)
    #: Source modifications each component's verification ran against; empty when every
    #: component was verified against the on-disk source.
    source_edits: list[SourceEditRecord] = Field(default_factory=list)
    #: Verification-supporting artifacts registered by plugin tools during
    #: formalization (Lean proofs et al.), written to disk by the artifact store.
    verification_artifacts: list[VerificationArtifactRecord] = Field(default_factory=list)
    coverage: CoverageReport
    #: Violated rules surfaced as audit issues (one per BAD rule; empty
    #: when nothing is violated, when synthesis was unavailable, or for a non-prover backend). Prose
    #: is synthesized at report time — see ``report/findings.py``.
    findings: list[Finding] = Field(default_factory=list)
    #: Review remarks on individual rules. Empty for a run that wrote its own rules.
    rule_feedback: list[RuleFeedback] = Field(default_factory=list)
    #: Per spec, whether its confs together checked every rule on every method.
    conf_coverage: list[ConfCoverage] = Field(default_factory=list)
    #: Coverage scores computed over the specs, when the run produced any.
    coverage_scores: list[SpecCoverageScore] = Field(default_factory=list)
    #: Soundness of the scene the specs were verified against (evaluate run, when scene judging
    #: ran). None for a run that did not assess the scene.
    scene: SceneAssessment | None = None
    #: Mutation testing of the specs (evaluate run, when mutation ran). None otherwise.
    mutation: MutationReport | None = None
