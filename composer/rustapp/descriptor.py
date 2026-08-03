"""Python mirror of the Rust ``AppDescriptor`` (see ``autoprover-sdk``).

These pydantic models are the Python side of the descriptor ABI. They are parsed
from the JSON a Rust wheel returns from ``descriptor()`` and consumed by the host
to synthesize the phase enum, argparse, frontend and artifact store. Keep the
field names in lockstep with ``rust/autoprover-sdk/src/lib.rs``.
"""

import enum
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from composer.spec.source.report.schema import ReportBackend

#: Ecosystem/chain tag. Mirrors ``composer.pipeline.ecosystem.ChainTag`` (kept local so this
#: ABI-mirror module stays decoupled from the pipeline); the host resolves it against the
#: ecosystem registry.
ChainTag = Literal["evm", "solana", "soroban"]


class CoreSlot(str, enum.Enum):
    """Which host-tagged step a declared phase groups.

    A phase with no slot is UI-only (cf. autoprove's harness/autosetup). The four the *driver* runs
    are :meth:`required` and every application must map them; the rest are optional steps the host
    runs around it, which fall back to a sensible phase when unclaimed."""

    ANALYSIS = "analysis"
    EXTRACTION = "extraction"
    FORMALIZATION = "formalization"
    REPORT = "report"
    #: Design-doc discovery, which the *entry point* runs before the pipeline (only when the doc
    #: wasn't passed on the command line). Optional: unclaimed, the task is grouped under the first
    #: declared phase. A wheel that wants it in a section of its own claims this slot rather than
    #: relying on a phase key the host would have to recognize by name.
    DISCOVERY = "discovery"

    @classmethod
    def required(cls) -> tuple["CoreSlot", ...]:
        """The slots every application must fill — the four steps the shared driver itself runs, and
        therefore tags every run with."""
        return (cls.ANALYSIS, cls.EXTRACTION, cls.FORMALIZATION, cls.REPORT)


class DeliverableMode(str, enum.Enum):
    """How the source deliverable is written (mirrors the Rust ``DeliverableMode``).

    ``per_component`` (default): the generic store writes one ``{prefix}_{slug}.{ext}`` file per
    component. ``callout``: the store writes no per-component source; the wheel's ``finalize``
    renders the whole deliverable (e.g. Crucible's one shared crate)."""

    PER_COMPONENT = "per_component"
    CALLOUT = "callout"


class StepSpec(BaseModel):
    """A declared step the host runs as its own visible task: which phase groups it (``phase_key``,
    a member of the synthesized enum) and what to call it (``label``).

    ``step`` is the step's kind, not wire data — it names the task id the host gives this step
    (``{app}-{step}``), so the id lives with the declaration rather than being spelled at each call
    site. Turn a spec into its task with :meth:`composer.rustapp.adapter.RustBackend.task_info`,
    which is what resolves ``phase_key`` against the enum."""

    step: ClassVar[str]

    phase_key: str
    label: str


class PreflightSpec(StepSpec):
    """An analysis-independent gate on the prepared workspace, run *concurrently with system
    analysis* — before a single property exists. The host follows the wheel's ``workspace_prep``
    with a ``kind="preflight"`` ``compile`` call under ``phase_key``, whose ``spec`` is empty: the
    wheel renders its own minimal skeleton, since nothing has been authored yet.

    It exists to fail on a *toolchain* problem — an unresolvable dependency graph, a harness that
    doesn't link, IDL codegen the generator rejects — while the run has spent almost no LLM budget.
    Such a failure is terminal (the host raises rather than re-authoring: the author does not own
    the manifest and cannot fix it), which lets the driver cancel the analysis and extraction
    running alongside. Mirrors the Rust ``PreflightSpec``."""

    step: ClassVar[str] = "preflight"


class SetupSpec(StepSpec):
    """A shared setup artifact authored once before per-component formalization (Crucible's
    shared fixture). The host runs the author→compile loop for a ``kind="setup"`` input under
    ``phase_key`` and threads the compiled spec into each component's context under
    ``context_key``. Mirrors the Rust ``SetupSpec``."""

    step: ClassVar[str] = "setup"

    context_key: str


class PhaseSpec(BaseModel):
    """One task-grouping phase; ``key`` becomes the synthesized enum member name."""

    key: str
    label: str
    order: int = 0
    core_slot: CoreSlot | None = None


class ArgDefault(BaseModel):
    """Tagged default value for a declared CLI argument."""

    kind: Literal["str", "int", "bool"]
    value: str | int | bool | None = None


class ArgSpec(BaseModel):
    """A CLI flag the generic entry point adds beyond the positional inputs."""

    flag: str
    help: str
    default: ArgDefault
    required: bool = False


class EventKind(BaseModel):
    """A domain event kind the frontend should render (see ``Command::Emit``).

    ``notice`` events are surfaced as a persistent, always-visible callout (plus a toast)
    rather than a line in the collapsible per-task events log — for one-shot important
    results such as a per-unit verdict. Defaults to ``False`` so wheels built before
    the field existed still load."""

    kind: str
    label: str
    notice: bool = False


class ArtifactLayout(BaseModel):
    """Project-root-relative deliverable layout."""

    deliverable_dir: str
    internal_dir: str
    report_dir: str
    artifact_dir: str
    artifact_prefix: str
    artifact_extension: str
    property_suffix: str
    #: Under ``callout`` deliverable mode, the project-relative primary deliverable path,
    #: ``{program}``-templated (Crucible: ``fuzz/{program}/src/main.rs``). Used only as each
    #: component's report link; ``None`` in ``per_component`` mode.
    deliverable_primary: str | None = None


class AppDescriptor(BaseModel):
    """The complete declaration a Rust wheel exports."""

    name: str
    header_text: str
    #: The ecosystem (chain) whose system model / prompts the shared front half uses. The
    #: host resolves it against ``composer.pipeline.ecosystem.ECOSYSTEMS``. Defaults to
    #: ``"evm"`` so wheels built before this field existed keep working.
    ecosystem: ChainTag = "evm"
    #: Which report vocabulary this backend's results are rendered with. Typed (rather than a free
    #: ``str`` validated later by ``as_report_backend``) so a wheel declaring a tag the report
    #: doesn't know fails in ``model_validate_json`` — at descriptor load, before the run starts —
    #: instead of at formalizer construction. The set is closed; see ``ReportBackend``.
    backend_tag: ReportBackend
    backend_guidance: str
    analysis_key: str
    phases: list[PhaseSpec]
    args: list[ArgSpec] = Field(default_factory=list)
    rag_db_default: str | None = None
    event_kinds: list[EventKind] = Field(default_factory=list)
    artifact_layout: ArtifactLayout
    #: Optional preflight gate on the prepared workspace, concurrent with system analysis (see
    #: :class:`PreflightSpec`).
    preflight: PreflightSpec | None = None
    #: Optional shared-setup step run before per-component formalization (see :class:`SetupSpec`).
    setup: SetupSpec | None = None
    #: How the source deliverable is written (see :class:`DeliverableMode`).
    deliverable_mode: DeliverableMode = DeliverableMode.PER_COMPONENT
    #: Serialize the blocking toolchain callouts on one semaphore — set when the app shares a
    #: single build dir / target across units.
    serialize_toolchain: bool = False
    #: Default to the fail-closed ``launcher`` sandbox provider (still overridable by
    #: ``COMPOSER_SANDBOX_PROVIDER``). Set by any wheel that runs untrusted native toolchains.
    confine_by_default: bool = False
    #: Human noun for one formalized unit in the console/TUI summary ("instruction" for
    #: Crucible). ``None`` → "component".
    component_noun: str | None = None

    def ordered_phases(self) -> list[PhaseSpec]:
        return sorted(self.phases, key=lambda p: (p.order, p.key))

    def core_slot_map(self) -> dict[CoreSlot, str]:
        """The declared phase ``key`` for each core slot it fills."""
        return {p.core_slot: p.key for p in self.phases if p.core_slot is not None}
