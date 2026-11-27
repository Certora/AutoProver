"""The results file of one verification run of one main contract, schema
``certora-autoprover-results/1``: the repositories, the property suite the run verified, the
outcome of every property of the suite with its rules and prover results, and the findings with
their justification and evidence. The statements of the properties are in the suite. The field
descriptions are part of the format.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from composer.spec.source.report.schema import ImpactLevel, LikelihoodLevel, SeverityTier

SCHEMA_ID = "certora-autoprover-results/1"

type TrustClass = Literal["unprivileged", "trusted_party", "self_harm"]
type RuleOutcome = Literal["verified", "violated", "timeout", "error", "unknown", "sanity_failed"]
type PropertyStatus = Literal["verified", "violated", "partial", "not_proven", "skipped", "dropped"]

#: The statuses whose property needs a ``status_note``.
NOTED_STATUSES: frozenset[PropertyStatus] = frozenset({"partial", "not_proven", "skipped", "dropped"})

#: The statuses a prover verdict on at least one rule decides.
RULED_STATUSES: frozenset[PropertyStatus] = frozenset({"verified", "violated", "partial"})

#: The statuses of a property with no rule.
UNRULED_STATUSES: frozenset[PropertyStatus] = frozenset({"skipped", "dropped"})

_COMMIT = r"^[0-9a-f]{40}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRepository(_Strict):
    """The project as its owners published it: the code the findings are about."""

    repository: str = Field(description="Git URL of the project's repository.")
    commit: str = Field(pattern=_COMMIT, description="The commit the run verified.")


class ResultsRepository(_Strict):
    """The repository holding the run's specs, confs and harnesses: the source tree at
    ``source.commit`` plus those files and nothing else."""

    repository: str = Field(description="Git URL of the results repository.")
    commit: str = Field(pattern=_COMMIT)
    certora_dir: str = Field(
        description="Directory of this run's specs, confs and harnesses in the repository, "
        "e.g. certora/ or certora/<name>/. Every rule's spec and conf is under it."
    )


class Run(_Strict):
    source: SourceRepository
    results: ResultsRepository
    main_contract: str = Field(pattern=r"^[^:]+\.sol:\w+$", description="path/to/File.sol:Contract")
    property_suite: str = Field(
        description="Path in the results repository, under results.certora_dir, of the "
        "*.property-suite.json the run verified. Its properties are the statements of "
        "properties[]."
    )
    skill_version: str
    generated_at: datetime
    property_generator_run_id: str | None = Field(
        default=None,
        description="Runs API id of the hosted property generator run the properties came from; "
        "null when it did not run.",
    )


class RuleResult(_Strict):
    rule: str
    spec: str = Field(description="Spec path in the results repository.")
    conf: str = Field(description="Conf path in the results repository the job ran.")
    result: RuleOutcome
    job_url: str = Field(
        description="The final prover job for this rule's current text. "
        "Its sources and conf match the results repository at run.results.commit."
    )


class Justification(_Strict):
    """Why this is a real finding."""

    pre_state: str = Field(
        description="The state the scenario starts from, with concrete values from the "
        "counterexample."
    )
    steps: list[str] = Field(description="The calls, in order, and who makes them.")
    reachability: str = Field(
        description="How the pre-state is reached through real calls from deployment, not only "
        "from arbitrary harness state. Where the run did not establish it, say so."
    )
    not_a_model_artefact: str = Field(
        description="Why the behaviour does not come from a summary, mock, harness or model of "
        "an external contract; or which real behaviour the model reproduces."
    )
    configuration: str | None = Field(
        default=None,
        description="The parameters or external setup the scenario needs, and whether they are "
        "plausible or documented.",
    )


class SeverityJustification(_Strict):
    impact: ImpactLevel
    likelihood: LikelihoodLevel
    reasoning: str


class Evidence(_Strict):
    property: str = Field(description="Property id.")
    how: str = Field(description="How this property's result shows the finding.")
    rules: list[RuleResult] = Field(
        min_length=1, description="The property's rules whose results show the finding."
    )


class MergedFrom(_Strict):
    unit: str = Field(description="Full unit (property group) name.")
    finding: str = Field(description="The unit's own finding id.")


class Finding(_Strict):
    id: str = Field(description="Stable within the run, e.g. F01.")
    title: str
    severity: SeverityTier
    trust_class: TrustClass
    trusted_precondition: str | None = Field(
        default=None,
        description="Required when trust_class is trusted_party: which trusted role must act and "
        "how.",
    )
    description: str = Field(description="What is wrong, in the code's terms, with file:line references.")
    impact: str | None = None
    justification: Justification
    severity_justification: SeverityJustification
    recommendation: str | None = None
    evidence: list[Evidence] = Field(min_length=1)
    merged_from: list[MergedFrom] | None = Field(
        default=None,
        description="Provenance: the unit findings merged into this finding. Report-facing text "
        "never cites them.",
    )

    @model_validator(mode="after")
    def _trusted_precondition_is_given(self) -> "Finding":
        if self.trust_class == "trusted_party" and not self.trusted_precondition:
            raise ValueError("a trusted_party finding needs a trusted_precondition")
        return self


class Property(_Strict):
    """The outcome of one property of the suite."""

    id: str = Field(description="The property's id in the suite.")
    status: PropertyStatus
    status_note: str | None = Field(
        default=None,
        description="Required, at least 20 characters, unless status is verified or violated. "
        "partial: the part of the property left out and why. not_proven: what was tried and why "
        "it did not produce a verdict. skipped: why it was not formalised. dropped: why the "
        "property was removed from the input list (duplicate, out of scope, wrong for this "
        "contract, ...).",
    )
    rules: list[RuleResult] = Field(
        description="Every rule tagged with the property's id (@property), with its result. At "
        "least one when status is verified, violated or partial; none when skipped or dropped."
    )
    findings: list[str] = Field(description="Finding ids this property's results show.")

    @model_validator(mode="after")
    def _status_fits(self) -> "Property":
        if self.status in NOTED_STATUSES and len(self.status_note or "") < 20:
            raise ValueError(f"a {self.status} property needs a status_note of at least 20 characters")
        if self.status in RULED_STATUSES and not self.rules:
            raise ValueError(f"a {self.status} property needs at least one rule")
        if self.status in UNRULED_STATUSES and self.rules:
            raise ValueError(f"a {self.status} property has no rules")
        return self


class Assumption(_Strict):
    text: str
    justified: bool
    justification: str | None = None


class Model(_Strict):
    name: str
    source: str = Field(
        description="Where the modelled behaviour was taken from: a dependency path, a fetched "
        "repository at a commit, or 'interface and documentation only'."
    )
    assumptions: list[Assumption]
    properties: list[str] | None = Field(
        default=None, description="Property ids whose verdict rests on this model."
    )


class Gap(_Strict):
    description: str
    properties: list[str]


class VerificationResults(_Strict):
    model_config = ConfigDict(
        title="Results of one verification run of one main contract",
        json_schema_extra={"$id": SCHEMA_ID},
    )

    schema_: Literal["certora-autoprover-results/1"] = Field(alias="schema")
    run: Run
    findings: list[Finding]
    properties: list[Property] = Field(description="One entry per property of the suite.")
    models: list[Model] | None = Field(
        default=None, description="Models of external contracts the verdicts rest on."
    )
    gaps: list[Gap] | None = Field(
        default=None, description="Code paths or properties the prover could not check, and why."
    )
