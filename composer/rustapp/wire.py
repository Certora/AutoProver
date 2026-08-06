"""Python mirror of the Rust SDK's **runtime** ABI — the payloads that cross the FFI on every call.

Peer of :mod:`composer.rustapp.descriptor`, which mirrors the *declarative* half (the
``AppDescriptor`` a wheel exports once). Together they are the whole seam: every string that goes
into or comes out of a wheel is one of these models, so a field renamed in
``rust/autoprover-sdk/src/lib.rs`` fails here — at the boundary, naming the field — instead of
silently reading as ``""`` three call frames later.

Keep the field names, defaults and tags in lockstep with ``rust/autoprover-sdk/src/lib.rs``.

The two results are **tagged unions** on the Rust side (``#[serde(tag = "status")]`` /
``#[serde(tag = "kind")]``), so they are discriminated unions here: a
:class:`ValidateBuildFailed` carries ``errors`` and no verdicts, a :class:`ValidateVerdicts` the
reverse, and ``isinstance`` is what tells them apart. Neither can be asked for a field the other
owns.

Both halves of this seam ship together, so a payload missing a field is never version skew — it is a
mirror that drifted. Nothing here is tolerant of that: **the side that deserializes requires
everything**. Python deserializes the *inbound* payloads, so those models default nothing and reject
unknown fields — a field a wheel omits, or one it sends that the host doesn't declare, fails in
``model_validate`` naming the field, at the callout that returned it.

Defaults on the *outbound* models are a different thing wearing the same clothes. Python only
serializes those, and ``model_dump_json`` writes every field whether or not it was set, so a default
there costs the wire nothing and buys a constructor: an empty ``source_unit`` is how the host says it
resolved nothing. Requiring those payloads in full is the Rust side's job, and it does it the same
way — no ``#[serde(default)]``, ``deny_unknown_fields``, and ``crate::required::present`` on the
``Option`` fields serde would otherwise fill in silently.

Three payloads on this seam are **chain-shaped**: ``source_unit``, ``prep_facts`` and
``WorkspacePrep.toolchain_request`` are typed here as bare ``dict[str, Any]`` (Rust:
``chain::ChainData``) because their fields belong to the *analyzed project's* build system, which this
framework deliberately holds no schema for — see :mod:`composer.rustapp.toolchain`. They are the same
treatment ``model`` and ``unit`` already get, and for the same reason.
"""

import enum
from typing import Annotated, Any, Callable, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from composer.spec.source.report.schema import Outcome
from composer.spec.types import PropertyType


class WireModel(BaseModel):
    """Base for every payload on this seam, and for :mod:`composer.rustapp.descriptor`'s.

    Rejects unknown fields, the counterpart of the Rust side's ``#[serde(deny_unknown_fields)]``:
    a key one half sends and the other doesn't declare is drift between two mirrors that ship
    together, so it should stop the run naming the key rather than be dropped on the floor."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Outbound — what the host sends into a callout.
# ---------------------------------------------------------------------------

class Property(WireModel):
    """One property to formalize, plus the host-assigned unique ``slug`` that names its unit."""

    title: str
    #: The shared vocabulary (:data:`~composer.spec.types.PropertyType`), mirrored by the Rust
    #: ``PropertyKind`` — a closed set on both sides rather than a free string.
    sort: PropertyType
    description: str
    slug: str = ""


class AppArgs(WireModel):
    """The run's resolved inputs, as the two argument-shaped callouts (``validate_preconditions``,
    ``sandbox_grants``) receive them. Mirrors the Rust ``AppArgs``.

    Every part the host already knows is its own field: ``program`` and ``source_path`` are the two
    halves of the entry point's ``path:Name`` argument, split *here* so no wheel re-splits it."""

    #: The project root, absolute.
    project_root: str
    #: The analysis identifier — never the name of a build-system unit (that is ``source_unit``).
    program: str
    #: The main source file, project-root-relative.
    source_path: str = ""
    #: The design doc, when one was named on the command line.
    system_doc: str | None = None
    #: Where the analyzed code lives as a unit of its own build system, chain-shaped — see
    #: :attr:`_AuthorInputBase.source_unit`. These two callouts run before workspace prep, so there
    #: are no prep facts to accompany it.
    source_unit: dict[str, Any] = Field(default_factory=dict)
    #: The wheel's own declared flags, keyed by argparse dest (``--fuzz-timeout`` →
    #: ``fuzz_timeout``). Untyped: the *wheel* declares these, so the host has no schema for them.
    declared: dict[str, Any] = Field(default_factory=dict)


class FailureKind(str, enum.Enum):
    """Which gate rejected a draft — a judge rejection is *not* a build failure (it compiled)."""

    COMPILE = "compile"
    JUDGE = "judge"


class Failure(WireModel):
    """Why a draft was rejected, fed into the next ``author_prompt`` as revise context. ``draft`` is
    carried because each authoring turn is fresh — the model has no memory of its prior attempt."""

    draft: str
    errors: str
    kind: FailureKind = FailureKind.COMPILE


class _AuthorInputBase(WireModel):
    """What every authoring/gating callout is told, whatever is being authored."""

    #: The *analysis* identifier of the program under test — a label and a namespace, never the name
    #: of a build-system unit (that is :attr:`source_unit`, and the two are independent).
    program: str
    #: Where the analyzed code lives as a unit of *its own* build system, resolved once per run by
    #: the chain's registered :class:`~composer.rustapp.toolchain.ProjectToolchain` and carried
    #: unchanged from here on. Chain-shaped and opaque to the host: a Cargo crate is a directory, a
    #: package, a lib target and an Anchor requirement; a Move package is not. Empty when nothing was
    #: resolved, which is when the wheel applies its own convention.
    source_unit: dict[str, Any] = Field(default_factory=dict)
    #: The properties this artifact must make checkable.
    props: list[Property] = Field(default_factory=list)
    #: The compiled shared setup artifact, for a wheel that declared a
    #: :class:`~composer.rustapp.descriptor.SetupSpec`.
    setup: str | None = None
    #: What workspace prep established from the wheel's own
    #: :attr:`WorkspacePrep.toolchain_request` — chain-shaped like :attr:`source_unit`, and produced
    #: by the same toolchain. Empty when the plan asked for nothing beyond placing files. A fact here
    #: means *the thing it describes is in place*, which is what a wheel reads to decide how it
    #: sources the program's types.
    prep_facts: dict[str, Any] = Field(default_factory=dict)
    #: The run's values for the wheel's own declared flags, keyed by argparse dest. Untyped: the
    #: wheel declares them, so the host has no schema for them.
    args: dict[str, Any] = Field(default_factory=dict)

    def with_props(self, props: list[Property]) -> Self:
        """This input with ``props`` replaced — the setup artifact's base input plus the properties
        it has to make checkable, which only exist after extraction."""
        return self.model_copy(update={"props": props})

    def with_prep_facts(self, prep_facts: dict[str, Any]) -> Self:
        """This input with what the workspace prep just established (the preflight gate re-renders
        the prep's input, and must see the workspace the prep actually set up)."""
        return self.model_copy(update={"prep_facts": prep_facts})


class PreflightInput(_AuthorInputBase):
    """Gate the prepared workspace before anything is authored: the wheel renders its own skeleton
    and ``compile`` builds it. Runs before analysis finishes, so it carries no model and no unit."""

    kind: Literal["preflight"] = "preflight"


class SetupInput(_AuthorInputBase):
    """Author the one shared artifact every unit builds on, from the analyzed model and *every*
    unit's properties."""

    kind: Literal["setup"] = "setup"
    #: The analyzed system model. Opaque to the host seam — its shape is the ecosystem's.
    model: dict[str, Any] = Field(default_factory=dict)


class ComponentInput(_AuthorInputBase):
    """Author (and gate) one unit's spec."""

    kind: Literal["component"] = "component"
    #: The unit being formalized (``FeatureUnit.feature_json()``). Opaque to the host seam.
    unit: dict[str, Any] = Field(default_factory=dict)


#: The input to every authoring/gating callout — tagged on ``kind`` (Rust ``Authored``). A variant
#: rather than one struct with a tag beside it: each kind carries something the other two have
#: nothing to say about, and none of them can be asked for another's payload.
AuthorInput = Annotated[
    PreflightInput | SetupInput | ComponentInput, Field(discriminator="kind")
]


class Delivered(WireModel):
    """What a component that reached the deliverable produced. Mirrors the Rust ``Delivered``."""

    status: Literal["delivered"] = "delivered"
    artifact_text: str = ""
    #: The validation targets this component's rows were checked by, in the order they ran — what a
    #: callout-mode wheel keys its deliverable sections and declared features on.
    targets: list[str] = Field(default_factory=list)
    property_units: list[tuple[str, list[str]]] = Field(default_factory=list)
    unit_file: str | None = None
    run_link: str | None = None


class ComponentGaveUp(WireModel):
    """Formalization gave up on this component; it contributes nothing to the deliverable."""

    status: Literal["gave_up"] = "gave_up"


#: A component's outcome — tagged on ``status`` (Rust ``ComponentOutcome``). A variant rather than a
#: ``delivered`` flag beside always-present fields: there is nothing to read on one that gave up.
ComponentOutcome = Annotated[Delivered | ComponentGaveUp, Field(discriminator="status")]


class FinalizeComponent(WireModel):
    """One component's line in the ``finalize`` payload."""

    name: str
    outcome: ComponentOutcome


class FinalizeInput(WireModel):
    """The full outcome set handed to ``finalize`` (Rust ``FinalizeInput``): everything a wheel
    needs to render the whole deliverable, including the same project facts the gated builds used —
    what ships must be what was checked."""

    program: str
    #: As every authoring callout received it — see :attr:`_AuthorInputBase.source_unit`.
    source_unit: dict[str, Any] = Field(default_factory=dict)
    #: As every authoring callout received it — see :attr:`_AuthorInputBase.prep_facts`.
    prep_facts: dict[str, Any] = Field(default_factory=dict)
    components: list[FinalizeComponent] = Field(default_factory=list)
    #: The compiled shared setup artifact, when the wheel declared one.
    setup: str | None = None


# ---------------------------------------------------------------------------
# Inbound — what a callout returns.
# ---------------------------------------------------------------------------

class Prompt(WireModel):
    """An authoring instruction for one LLM turn, plus an optional backend-defined system prompt
    (``None`` → the host's neutral default)."""

    instruction: str
    system: str | None


class CompileOk(WireModel):
    """The spec (or, in a preflight, the wheel's own skeleton) built."""

    status: Literal["ok"]


class CompileFailed(WireModel):
    """It did not build. ``errors`` is the diagnostics the wheel extracted, and becomes the revise
    context for the next authoring turn."""

    status: Literal["failed"]
    errors: str


#: ``compile``'s result — tagged on ``status`` (Rust ``CompileResult``).
CompileResult = Annotated[CompileOk | CompileFailed, Field(discriminator="status")]


class Unit(WireModel):
    """One report row: a property title and the backend's unit name for it (the rule the report keys
    by). ``target`` is the validation target the *host runs*; several units may share one, so the
    host runs each distinct target once and the wheel attributes the outcome back to each unit."""

    property: str
    unit: str
    target: str | None

    # A method, not a ``@property``: the ``property`` field above shadows that builtin inside this
    # class body. Mirrors the Rust ``Unit::target_or_unit``.
    def target_or_unit(self) -> str:
        """The target this unit is checked by — its own name unless it shares one."""
        return self.target or self.unit


class Target(WireModel):
    """One validation target the host runs, and the report units it covers — what ``validate`` must
    return a verdict for. Mirrors the Rust ``Target``.

    The host owns the grouping (it decides what to run, and in what order), so it hands the answer
    over rather than leaving the wheel to recover it by re-deriving its own ``units`` and filtering
    them by name."""

    #: What the backend selects when it runs the checker (Crucible: the component's ``c_<slug>``
    #: harness fn, which is also its Cargo feature).
    name: str
    #: Usually one row; several when a backend checks a whole property set in one run.
    units: list[Unit] = Field(default_factory=list)


class Verdict(WireModel):
    """One unit's outcome. Mirrors the Rust ``Verdict`` and maps onto the report's
    :class:`composer.spec.source.report.collect.Verdict` (whose ``message`` is this ``detail``)."""

    outcome: Outcome
    line: int | None
    duration_seconds: float | None
    unit_file: str | None
    #: Human-readable explanation of a non-GOOD outcome — the counterexample / assertion message for
    #: a BAD, the error text for an ERROR.
    detail: str | None

    @classmethod
    def with_outcome(cls, outcome: Outcome) -> "Verdict":
        """A bare verdict: the outcome, no diagnostics. Mirrors the Rust ``Verdict::with_outcome``,
        and exists for the same reason — every field being required is right for the wire and no
        reason for a caller that has only an outcome to spell four nulls to say so."""
        return cls(outcome=outcome, line=None, duration_seconds=None, unit_file=None, detail=None)


class ValidateBuildFailed(WireModel):
    """The shared build failed, so the whole spec is re-authored — no unit got a verdict."""

    kind: Literal["build_failed"]
    errors: str


class ValidateVerdicts(WireModel):
    """It built, and every report unit the target covers got a verdict, ``(unit, verdict)``."""

    kind: Literal["verdicts"]
    verdicts: list[tuple[str, Verdict]]


#: ``validate``'s result — tagged on ``kind`` (Rust ``ValidateOutcome``).
ValidateOutcome = Annotated[ValidateBuildFailed | ValidateVerdicts, Field(discriminator="kind")]


class WorkspacePrep(WireModel):
    """A pure plan for preparing the workspace: the wheel declares it, the host executes it (so the
    network posture stays Python-owned and the wheel never supplies a command line).

    Two halves, split by who can execute them — writing files is the same in every ecosystem, while
    preparing a *project* means driving a build system the host does not understand."""

    #: Files to write under the workdir, path-confined. Contents only.
    files: dict[str, str]
    #: What the chain's :class:`~composer.rustapp.toolchain.ProjectToolchain` should do beyond
    #: writing :attr:`files` — warm a dependency cache, build the program, derive a client from it.
    #: Chain-shaped and opaque here (Solana: ``{warm_dirs, build_program, idl_dest}``): the host
    #: forwards it and only asks whether it is empty, which is what keeps a new ecosystem a
    #: registration rather than a field on this model. Whatever it establishes comes back as
    #: :attr:`_AuthorInputBase.prep_facts`.
    toolchain_request: dict[str, Any]

    @property
    def needs_toolchain(self) -> bool:
        """Whether anything beyond writing :attr:`files` is asked for. A plan that only places files
        is complete once they are written — nothing to warm or build, so nothing that needs a
        toolchain, a sandbox or the network."""
        return bool(self.toolchain_request)


class SandboxGrants(WireModel):
    """Extra grants a wheel needs unioned into the host-authored policy. Pure data — the wheel
    declares grants, Python decides the policy."""

    extra_ro: list[str]
    #: Extra env *names* to pass through confinement.
    extra_env: list[str]


# ---------------------------------------------------------------------------
# The FFI surface itself.
# ---------------------------------------------------------------------------

class RustAppModule(Protocol):
    """The compiled wheel's module-level surface — what ``autoprover_sdk::export_app!`` exports.

    Members are declared as callables rather than methods so :data:`CALLOUTS` can be derived from
    the annotations: the import-time check and this contract then cannot drift. Everything is
    synchronous and speaks JSON strings; ``compile`` and ``validate`` block (they spawn
    ``run-confined`` and release the GIL), which is why the host runs them off the event loop."""

    #: ``() -> AppDescriptor`` JSON. The declarative spine.
    descriptor: Callable[[], str]
    #: ``(args_json) -> error | None``. A precondition the wheel checks before the run starts.
    validate_preconditions: Callable[[str], str | None]
    #: ``(input_json) -> list[Unit]`` JSON. The report rows this input formalizes.
    units: Callable[[str], str]
    #: ``(input_json, failure_json | None) -> Prompt`` JSON.
    author_prompt: Callable[[str, str | None], str]
    #: ``(input_json, spec) -> Prompt | None`` JSON. ``None`` ⇒ this wheel has no judge.
    judge_prompt: Callable[[str, str], str | None]
    #: ``(input_json, spec, workdir, sandbox_json) -> CompileResult`` JSON. **Blocking.**
    compile: Callable[[str, str, str, str], str]
    #: ``(input_json, spec, target_json, workdir, sandbox_json) -> ValidateOutcome`` JSON, where
    #: ``target_json`` is a :class:`Target` — the target to run and the rows it covers.
    #: **Blocking.**
    validate: Callable[[str, str, str, str, str], str]
    #: ``(input_json) -> WorkspacePrep`` JSON. Pure — the host executes the plan.
    workspace_prep: Callable[[str], str]
    #: ``(args_json) -> SandboxGrants`` JSON.
    sandbox_grants: Callable[[str], str]
    #: ``(outcomes_json) -> {relpath: contents} | None`` JSON.
    finalize: Callable[[str], str | None]


#: Every callout name the host may call, derived from :class:`RustAppModule` so the two can't drift.
#: Used to reject a module that isn't an AutoProver wheel (or is one built against an older SDK)
#: at load, with the missing callouts named.
CALLOUTS: tuple[str, ...] = tuple(RustAppModule.__annotations__)


# ---------------------------------------------------------------------------
# Parsing — one function per callout return, so every ``json.loads`` of a wheel's answer happens
# here and nowhere else. A malformed payload raises ``pydantic.ValidationError`` naming the field.
# ---------------------------------------------------------------------------

_COMPILE_RESULT: TypeAdapter[CompileOk | CompileFailed] = TypeAdapter(CompileResult)
_VALIDATE_OUTCOME: TypeAdapter[ValidateBuildFailed | ValidateVerdicts] = TypeAdapter(ValidateOutcome)
_UNITS: TypeAdapter[list[Unit]] = TypeAdapter(list[Unit])
_FILES: TypeAdapter[dict[str, str]] = TypeAdapter(dict[str, str])


def parse_compile(raw: str) -> CompileOk | CompileFailed:
    return _COMPILE_RESULT.validate_json(raw)


def parse_validate(raw: str) -> ValidateBuildFailed | ValidateVerdicts:
    return _VALIDATE_OUTCOME.validate_json(raw)


def parse_units(raw: str) -> list[Unit]:
    return _UNITS.validate_json(raw)


def parse_prompt(raw: str) -> Prompt:
    return Prompt.model_validate_json(raw)


def parse_workspace_prep(raw: str) -> WorkspacePrep:
    return WorkspacePrep.model_validate_json(raw)


def parse_sandbox_grants(raw: str) -> SandboxGrants:
    return SandboxGrants.model_validate_json(raw)


def parse_files(raw: str) -> dict[str, str]:
    """``finalize``'s ``{relpath: contents}`` map."""
    return _FILES.validate_json(raw)
