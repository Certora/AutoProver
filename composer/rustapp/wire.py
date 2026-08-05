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

Direction matters for defaults. **Inbound** models (what a wheel returns) default every optional
field, so a wheel built against an older SDK still parses — pydantic ignores unknown fields, which
covers the newer-wheel direction too. **Outbound** models (what the host sends) require what the
wheel requires: an omitted ``kind`` or ``program`` is a host bug and should not be reachable.
"""

import enum
import logging
from typing import Annotated, Any, Callable, Literal, Protocol, Self

from pydantic import BaseModel, Field, TypeAdapter, field_validator

from composer.spec.source.report.schema import Outcome

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Outbound — what the host sends into a callout.
# ---------------------------------------------------------------------------

class Property(BaseModel):
    """One property to formalize, plus the host-assigned unique ``slug`` that names its unit."""

    title: str
    #: ``attack_vector`` | ``safety_property`` | ``invariant`` — ``PropertyFormulation.sort``,
    #: passed through as the wheel's own vocabulary for it is the report's, not ours.
    sort: str
    description: str
    slug: str = ""


class ProgramCrate(BaseModel):
    """Where the analyzed code lives as a compilation unit, for a wheel that must *depend* on it.

    Filled in by the chain's registered resolver (:func:`composer.rustapp.toolchain.source_crate`) —
    every field empty when there is none, when the language has no such unit, or when the layout
    couldn't be read. The wheel never sees that shape: the SDK's FFI boundary fills the gaps from
    the ``programs/<program>`` convention before any callout runs."""

    dir: str = ""
    package: str = ""
    lib: str = ""
    #: The crate's declared ``anchor-lang`` requirement, verbatim; ``""`` when it declares none.
    anchor: str = ""


class AppArgs(BaseModel):
    """The run's resolved inputs, as the two argument-shaped callouts (``validate_preconditions``,
    ``sandbox_grants``) receive them. Mirrors the Rust ``AppArgs``.

    Every part the host already knows is its own field: ``program`` and ``source_path`` are the two
    halves of the entry point's ``path:Name`` argument, split *here* so no wheel re-splits it."""

    #: The project root, absolute.
    project_root: str
    #: The analysis identifier — never a Cargo name (that is :class:`ProgramCrate`).
    program: str
    #: The main source file, project-root-relative.
    source_path: str = ""
    #: The design doc, when one was named on the command line.
    system_doc: str | None = None
    program_crate: ProgramCrate = Field(default_factory=ProgramCrate)
    #: The wheel's own declared flags, keyed by argparse dest (``--fuzz-timeout`` →
    #: ``fuzz_timeout``). Untyped: the *wheel* declares these, so the host has no schema for them.
    declared: dict[str, Any] = Field(default_factory=dict)


class FailureKind(str, enum.Enum):
    """Which gate rejected a draft — a judge rejection is *not* a build failure (it compiled)."""

    COMPILE = "compile"
    JUDGE = "judge"


class Failure(BaseModel):
    """Why a draft was rejected, fed into the next ``author_prompt`` as revise context. ``draft`` is
    carried because each authoring turn is fresh — the model has no memory of its prior attempt."""

    draft: str
    errors: str
    kind: FailureKind = FailureKind.COMPILE


class _AuthorInputBase(BaseModel):
    """What every authoring/gating callout is told, whatever is being authored."""

    #: The *analysis* identifier of the program under test — a label and a namespace, never a Cargo
    #: name (that is :class:`ProgramCrate`).
    program: str
    program_crate: ProgramCrate = Field(default_factory=ProgramCrate)
    #: The properties this artifact must make checkable.
    props: list[Property] = Field(default_factory=list)
    #: The compiled shared setup artifact, for a wheel that declared a
    #: :class:`~composer.rustapp.descriptor.SetupSpec`.
    setup: str | None = None
    #: Where workspace prep placed the program's IDL, project-root-relative — ``None`` when it
    #: placed none. Set means the file is *in place*, which is the signal a wheel reads to decide
    #: how it sources the program's types.
    idl: str | None = None
    #: The run's values for the wheel's own declared flags, keyed by argparse dest. Untyped: the
    #: wheel declares them, so the host has no schema for them.
    args: dict[str, Any] = Field(default_factory=dict)

    def with_props(self, props: list[Property]) -> Self:
        """This input with ``props`` replaced — the setup artifact's base input plus the properties
        it has to make checkable, which only exist after extraction."""
        return self.model_copy(update={"props": props})

    def with_idl(self, idl: str | None) -> Self:
        """This input with the IDL the workspace prep just placed (the preflight gate re-renders
        the prep's input, and must see the same crate the prep set up)."""
        return self.model_copy(update={"idl": idl})


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


class Delivered(BaseModel):
    """What a component that reached the deliverable produced. Mirrors the Rust ``Delivered``."""

    status: Literal["delivered"] = "delivered"
    artifact_text: str = ""
    #: The validation targets this component's rows were checked by, in the order they ran — what a
    #: callout-mode wheel keys its deliverable sections and declared features on.
    targets: list[str] = Field(default_factory=list)
    property_units: list[tuple[str, list[str]]] = Field(default_factory=list)
    unit_file: str | None = None
    run_link: str | None = None


class ComponentGaveUp(BaseModel):
    """Formalization gave up on this component; it contributes nothing to the deliverable."""

    status: Literal["gave_up"] = "gave_up"


#: A component's outcome — tagged on ``status`` (Rust ``ComponentOutcome``). A variant rather than a
#: ``delivered`` flag beside always-present fields: there is nothing to read on one that gave up.
ComponentOutcome = Annotated[Delivered | ComponentGaveUp, Field(discriminator="status")]


class FinalizeComponent(BaseModel):
    """One component's line in the ``finalize`` payload."""

    name: str
    outcome: ComponentOutcome


class FinalizeInput(BaseModel):
    """The full outcome set handed to ``finalize`` (Rust ``FinalizeInput``): everything a wheel
    needs to render the whole deliverable, including the same program crate and IDL the gated
    builds used — what ships must be what was checked."""

    program: str
    program_crate: ProgramCrate = Field(default_factory=ProgramCrate)
    #: Where workspace prep placed the program's IDL; ``None`` when it placed none.
    idl: str | None = None
    components: list[FinalizeComponent] = Field(default_factory=list)
    #: The compiled shared setup artifact, when the wheel declared one.
    setup: str | None = None


# ---------------------------------------------------------------------------
# Inbound — what a callout returns.
# ---------------------------------------------------------------------------

class Prompt(BaseModel):
    """An authoring instruction for one LLM turn, plus an optional backend-defined system prompt
    (``None`` → the host's neutral default)."""

    instruction: str
    system: str | None = None


class CompileOk(BaseModel):
    """The spec (or, in a preflight, the wheel's own skeleton) built."""

    status: Literal["ok"]


class CompileFailed(BaseModel):
    """It did not build. ``errors`` is the diagnostics the wheel extracted, and becomes the revise
    context for the next authoring turn."""

    status: Literal["failed"]
    errors: str = ""


#: ``compile``'s result — tagged on ``status`` (Rust ``CompileResult``).
CompileResult = Annotated[CompileOk | CompileFailed, Field(discriminator="status")]


class Unit(BaseModel):
    """One report row: a property title and the backend's unit name for it (the rule the report keys
    by). ``target`` is the validation target the *host runs*; several units may share one, so the
    host runs each distinct target once and the wheel attributes the outcome back to each unit."""

    property: str
    unit: str
    target: str | None = None

    # A method, not a ``@property``: the ``property`` field above shadows that builtin inside this
    # class body. Mirrors the Rust ``Unit::target_or_unit``.
    def target_or_unit(self) -> str:
        """The target this unit is checked by — its own name unless it shares one."""
        return self.target or self.unit


class Target(BaseModel):
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


class Verdict(BaseModel):
    """One unit's outcome. Mirrors the Rust ``Verdict`` and maps onto the report's
    :class:`composer.spec.source.report.collect.Verdict` (whose ``message`` is this ``detail``)."""

    outcome: Outcome
    line: int | None = None
    duration_seconds: float | None = None
    unit_file: str | None = None
    #: Human-readable explanation of a non-GOOD outcome — the counterexample / assertion message for
    #: a BAD, the error text for an ERROR.
    detail: str | None = None

    @field_validator("outcome", mode="before")
    @classmethod
    def _tolerate_unknown_outcome(cls, value: object) -> object:
        """An outcome this host doesn't know reads as UNKNOWN instead of failing the component.

        The label is a diagnostic, and a wheel emitting one we've never heard of is version skew —
        not a corrupt payload. Losing one row's wording beats losing the run's results."""
        if isinstance(value, str) and Outcome.parse(value) is None:
            _log.warning("wheel reported unknown outcome %r; recording UNKNOWN", value)
            return Outcome.UNKNOWN
        return value


class ValidateBuildFailed(BaseModel):
    """The shared build failed, so the whole spec is re-authored — no unit got a verdict."""

    kind: Literal["build_failed"]
    errors: str = ""


class ValidateVerdicts(BaseModel):
    """It built, and every report unit the target covers got a verdict, ``(unit, verdict)``."""

    kind: Literal["verdicts"]
    verdicts: list[tuple[str, Verdict]] = Field(default_factory=list)


#: ``validate``'s result — tagged on ``kind`` (Rust ``ValidateOutcome``).
ValidateOutcome = Annotated[ValidateBuildFailed | ValidateVerdicts, Field(discriminator="kind")]


class WorkspacePrep(BaseModel):
    """A pure plan for preparing the workspace: the wheel declares it, the host executes it with the
    shared toolchain helpers (so the network posture stays Python-owned and the wheel never supplies
    a command line)."""

    #: Files to write under the workdir, path-confined. Contents only.
    files: dict[str, str] = Field(default_factory=dict)
    #: Project-relative manifest dirs to ``cargo fetch`` (unconfined, network) so a later confined +
    #: offline build finds every dep warm.
    warm_dirs: list[str] = Field(default_factory=list)
    #: The build artifact the chain's :class:`~composer.rustapp.toolchain.WorkspaceToolchain` should
    #: produce (for Cargo, the crate's *lib* target — not the analysis identifier).
    build_program: str | None = None
    #: Where the wheel wants the program's IDL, workdir-relative. The host obtains it, writes it
    #: there, and echoes the path back as the ``idl`` context key.
    idl_dest: str | None = None

    @property
    def needs_toolchain(self) -> bool:
        """Whether anything beyond writing :attr:`files` is asked for. A plan that only places files
        is complete once they are written — no warm, no build, no IDL, so nothing that needs a
        toolchain, a sandbox or the network."""
        return bool(self.warm_dirs or self.build_program or self.idl_dest)


class SandboxGrants(BaseModel):
    """Extra grants a wheel needs unioned into the host-authored policy. Pure data — the wheel
    declares grants, Python decides the policy."""

    extra_ro: list[str] = Field(default_factory=list)
    #: Extra env *names* to pass through confinement.
    extra_env: list[str] = Field(default_factory=list)


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
