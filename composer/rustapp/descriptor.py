"""Python mirror of the Rust ``AppDescriptor`` (see ``autoprover-sdk``).

These pydantic models are the Python side of the descriptor ABI. They are parsed
from the JSON a Rust wheel returns from ``descriptor()`` and consumed by the host
to synthesize the phase enum, argparse, frontend and artifact store. Keep the
field names in lockstep with ``rust/autoprover-sdk/src/lib.rs``.
"""

import enum
from typing import Annotated, Literal

from pydantic import Field

from composer.rustapp.wire import WireModel

from composer.spec.source.report.schema import ReportBackend

#: Ecosystem/chain tag. Mirrors ``composer.pipeline.ecosystem.ChainTag`` (kept local so this
#: ABI-mirror module stays decoupled from the pipeline); the host resolves it against the
#: ecosystem registry.
ChainTag = Literal["evm", "solana", "soroban"]


class PhaseRole(str, enum.Enum):
    """Which step of the run a declared phase groups — and, for the steps the host runs as their own
    visible task (:data:`STEP_ROLES`), the declaration *of* that step.

    The four the *driver* runs are :meth:`required` and every application must claim them; the rest
    are optional steps the host runs around it. A role no phase claims is a step this application
    does not have; :attr:`GROUPING` is a phase that declares no step at all."""

    #: Grouping only — the host runs no step of its own here (cf. autoprove's harness/autosetup).
    GROUPING = "grouping"
    ANALYSIS = "analysis"
    EXTRACTION = "extraction"
    FORMALIZATION = "formalization"
    REPORT = "report"
    #: Design-doc discovery, which the *entry point* runs before the pipeline (only when the doc
    #: wasn't passed on the command line). Optional: unclaimed, the task is grouped under the first
    #: declared phase. A wheel that wants it in a section of its own claims this role rather than
    #: relying on a phase key the host would have to recognize by name.
    DISCOVERY = "discovery"
    #: The analysis-independent gate on the prepared workspace, run concurrently with system
    #: analysis. The host follows the wheel's ``workspace_prep`` with a ``preflight`` ``compile``
    #: that carries no ``spec`` at all: the wheel renders its own minimal skeleton, since nothing
    #: has been authored yet.
    #:
    #: It exists to fail on a *toolchain* problem — an unresolvable dependency graph, a harness that
    #: doesn't link, IDL codegen the generator rejects — while the run has spent almost no LLM
    #: budget. Such a failure is terminal (the host raises rather than re-authoring: the author does
    #: not own the manifest and cannot fix it), which lets the driver cancel the analysis and
    #: extraction running alongside.
    PREFLIGHT = "preflight"
    #: A shared setup spec authored once before per-component formalization (Crucible's shared
    #: fixture). The host runs the author→compile loop for a :class:`SetupInput
    #: <composer.rustapp.wire.SetupInput>` and hands the compiled spec to every component as
    #: ``AuthorInput.setup``.
    SETUP = "setup"

    @classmethod
    def required(cls) -> tuple["PhaseRole", ...]:
        """The roles every application must claim — the four steps the shared driver itself runs,
        and therefore tags every run with."""
        return (cls.ANALYSIS, cls.EXTRACTION, cls.FORMALIZATION, cls.REPORT)


#: The roles whose step the host runs as its own visible task, ``{app}-{role}``. Looked up through
#: :meth:`AppDescriptor.step` — an unclaimed one means the application has no such step.
STEP_ROLES: tuple[PhaseRole, ...] = (PhaseRole.PREFLIGHT, PhaseRole.SETUP)


class PerComponent(WireModel):
    """The generic store writes one ``{prefix}_{slug}.{ext}`` file per component."""

    mode: Literal["per_component"] = "per_component"


class Callout(WireModel):
    """The store writes no per-component source; the wheel's ``finalize`` renders the whole
    deliverable (e.g. Crucible's one shared crate)."""

    mode: Literal["callout"] = "callout"
    #: Does the deliverable have a representative primary file? ``finalize`` renders a whole tree,
    #: but every delivered component still records one path — its basename becomes the component's
    #: ``unit_file``, the report's rule-identity fallback, echoed to ``finalize`` — and the store
    #: can't guess where in that tree the components' checks land. A path (project-relative,
    #: ``{program}``-templated — Crucible: ``fuzz/{program}/src/main.rs``) names that file;
    #: ``None`` declares that no one file represents the deliverable, and components anchor to the
    #: layout's ``deliverable_dir`` instead. On the variant because it means nothing per-component.
    deliverable_path: str | None


#: How the source deliverable is written — tagged on ``mode`` (Rust ``DeliverableMode``).
DeliverableMode = Annotated[PerComponent | Callout, Field(discriminator="mode")]


class PhaseSpec(WireModel):
    """One task-grouping phase; ``key`` becomes the synthesized enum member name.

    For a :data:`STEP_ROLES` role this is also the declaration of that step: the task the host runs
    is this ``label``, under this phase, with id ``{app}-{role}``. Turn one into its task with
    :meth:`composer.rustapp.adapter.RustBackend.task_info`, which resolves the phase member."""

    key: str
    label: str
    order: int
    role: PhaseRole


class StrDefault(WireModel):
    """A text flag's default; ``None`` when it has none."""

    kind: Literal["str"] = "str"
    value: str | None


class IntDefault(WireModel):
    """A numeric flag's default; ``None`` when it has none."""

    kind: Literal["int"] = "int"
    value: int | None


class BoolDefault(WireModel):
    """A ``store_true`` flag's initial state. Not optional the way the other two are: a boolean
    flag is either set or unset, so there is no third "no default" case to spell."""

    kind: Literal["bool"] = "bool"
    value: bool


#: A declared CLI argument's default — tagged on ``kind`` (Rust ``ArgDefault``). A variant per type
#: rather than one model with a ``str | int | bool | None`` beside the tag: only the matching
#: variant's ``value`` is meaningful, and this way the tag is what narrows it.
ArgDefault = Annotated[StrDefault | IntDefault | BoolDefault, Field(discriminator="kind")]


class ArgSpec(WireModel):
    """A CLI flag the generic entry point adds beyond the positional inputs."""

    flag: str
    help: str
    default: ArgDefault
    required: bool


class EventKind(WireModel):
    """A domain event kind the frontend should render.

    ``notice`` events are surfaced as a persistent, always-visible callout (plus a toast)
    rather than a line in the collapsible per-task events log — for one-shot important
    results such as one check's verdict.

    The events themselves are emitted by the *host*, around the callouts it drives (a build
    failure, a review verdict, a check's outcome) — a wheel has no emit channel, since its blocking
    callouts run to completion with the GIL released. A declared kind nothing emits renders
    nothing."""

    kind: str
    label: str
    notice: bool


class ArtifactLayout(WireModel):
    """Project-root-relative deliverable layout."""

    deliverable_dir: str
    internal_dir: str
    report_dir: str
    artifact_dir: str
    artifact_prefix: str
    artifact_extension: str
    property_suffix: str


class AppDescriptor(WireModel):
    """The complete declaration a Rust wheel exports."""

    name: str
    header_text: str
    #: The ecosystem (chain) whose system model / prompts the shared front half uses. The
    #: host resolves it against ``composer.pipeline.ecosystem.ECOSYSTEMS``; a tag outside the set
    #: fails here, at descriptor load, rather than against the wrong system model mid-run.
    ecosystem: ChainTag
    #: Which report vocabulary this backend's results are rendered with. Typed (rather than a free
    #: ``str`` validated later by ``as_report_backend``) so a wheel declaring a tag the report
    #: doesn't know fails in ``model_validate_json`` — at descriptor load, before the run starts —
    #: instead of at formalizer construction. The set is closed; see ``ReportBackend``.
    backend_tag: ReportBackend
    backend_guidance: str
    analysis_key: str
    phases: list[PhaseSpec]
    args: list[ArgSpec]
    rag_db_default: str | None
    event_kinds: list[EventKind]
    artifact_layout: ArtifactLayout
    #: How the source deliverable is written (see :data:`DeliverableMode`).
    deliverable_mode: DeliverableMode
    #: Serialize the blocking toolchain callouts on one semaphore — set when the app shares a
    #: single build dir / target across components.
    serialize_toolchain: bool
    #: Default to the fail-closed ``launcher`` sandbox provider (still overridable by
    #: ``COMPOSER_SANDBOX_PROVIDER``). Set by any wheel that runs untrusted native toolchains.
    confine_by_default: bool
    #: Human noun for one formalized component in the console/TUI summary ("instruction" for
    #: Crucible). ``None`` → "component"; read it through :meth:`unit_noun`.
    component_noun: str | None
    #: What this backend calls one check *to the model* ("rule", "harness function", "invariant") —
    #: the word the authoring prompts use throughout. ``None`` → "check"; read it through
    #: :meth:`check_label`. Declared rather than fixed because an author writes better when the
    #: prompt speaks its domain's language.
    check_noun: str | None
    #: What an author may cite when rebutting the judge's prior-round feedback — the closed set the
    #: rebuttal tool's ``evidence_type`` is built from. Declared per wheel because the evidence a
    #: backend can produce is a property of that backend.
    evidence_kinds: list[str]

    def unit_noun(self, *, plural: bool = False) -> str:
        """The noun for a formalized unit, with the generic default applied — so no frontend
        spells the ``or "component"`` fallback (nor the pluralization) itself."""
        noun = self.component_noun or "component"
        return f"{noun}s" if plural else noun

    def check_label(self, *, plural: bool = False) -> str:
        """The noun for one check, with the generic default applied — so no prompt spells the
        ``or "check"`` fallback (nor the pluralization) itself."""
        noun = self.check_noun or "check"
        return f"{noun}s" if plural else noun

    def ordered_phases(self) -> list[PhaseSpec]:
        return sorted(self.phases, key=lambda p: (p.order, p.key))

    def role_map(self) -> dict[PhaseRole, str]:
        """The declared phase ``key`` for each role a phase claims."""
        return {p.role: p.key for p in self.phases if p.role is not PhaseRole.GROUPING}

    def step(self, role: PhaseRole) -> PhaseSpec | None:
        """The phase declaring ``role``'s step, or ``None`` when no phase claims it — which is how
        an application says it has no such step. The lookup is by role rather than by a key one
        side spells and the other has to match."""
        return next((p for p in self.phases if p.role is role), None)
