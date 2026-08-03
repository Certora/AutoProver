"""Python mirror of the Rust SDK's **runtime** ABI — the payloads that cross the FFI on every call.

Peer of :mod:`composer.rustapp.descriptor`, which mirrors the *declarative* half (the
``AppDescriptor`` a wheel exports once). Together they are the whole seam: every string that goes
into or comes out of a wheel is one of these models, so the JSON shape lives in exactly two files
and a field renamed in ``rust/autoprover-sdk/src/lib.rs`` fails here — at the boundary, naming the
field — instead of silently reading as ``""`` three call frames later.

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
from typing import Annotated, Any, Callable, Literal, Protocol

from pydantic import BaseModel, Field, TypeAdapter, field_validator

from composer.spec.source.report.schema import Outcome

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Outbound — what the host sends into a callout.
# ---------------------------------------------------------------------------

#: What is being authored or gated. Mirrors the three ``AuthorInput::kind`` values the host sends
#: (see the Rust ``AuthorInput`` docs): a workspace gate before analysis, the one shared artifact,
#: or a single unit's spec.
AuthorKind = Literal["preflight", "setup", "component"]


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
    couldn't be read, which the wheel reads through its ``ProgramCrate::resolved`` fallback."""

    dir: str = ""
    package: str = ""
    lib: str = ""
    #: The crate's declared ``anchor-lang`` requirement, verbatim; ``""`` when it declares none.
    anchor: str = ""


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


class AuthorInput(BaseModel):
    """The input to every authoring/gating callout for one artifact.

    ``component`` and ``context`` are opaque JSON both sides agree on per application (the analyzed
    unit; the declared args + shared artifact), so they stay dicts — typing them would mean the host
    inventing a schema for values it only forwards."""

    kind: AuthorKind
    #: The *analysis* identifier of the program under test — a label and a namespace, never a Cargo
    #: name (that is :class:`ProgramCrate`).
    program: str
    program_crate: ProgramCrate = Field(default_factory=ProgramCrate)
    component: dict[str, Any] = Field(default_factory=dict)
    props: list[Property] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)

    def with_props(self, props: list[Property]) -> "AuthorInput":
        """This input with ``props`` replaced — the setup artifact's base input plus the properties
        it has to make checkable, which only exist after extraction."""
        return self.model_copy(update={"props": props})

    def with_context(self, context: dict[str, Any]) -> "AuthorInput":
        """This input under a different ``context`` (the preflight gate re-renders the prep's input
        with the IDL key the prep just reported)."""
        return self.model_copy(update={"context": context})


class FinalizeComponent(BaseModel):
    """One component's outcome in the ``finalize`` payload."""

    name: str
    delivered: bool
    unit_file: str | None = None
    run_link: str | None = None
    artifact_text: str = ""
    property_units: list[tuple[str, list[str]]] = Field(default_factory=list)
    #: The validation targets this component's rows were checked by, in the order they ran — what a
    #: callout-mode wheel keys its deliverable sections and declared features on.
    targets: list[str] = Field(default_factory=list)


class FinalizeInput(BaseModel):
    """The full outcome set handed to ``finalize``. The Rust side takes this as an opaque
    ``serde_json::Value`` (there is no ``Outcomes`` struct yet), so this model is the only written
    definition of the shape — keep it in step with what the SDK's ``finalize`` docs promise."""

    program: str
    program_crate: ProgramCrate = Field(default_factory=ProgramCrate)
    #: Where workspace prep placed the program's IDL; ``""`` when it placed none.
    idl: str = ""
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

    # A method rather than a ``@property``, deliberately: the field above shadows that builtin
    # inside this class body. It also reads as the peer of the Rust ``Unit::target_or_unit`` it
    # mirrors — the rule lives in one place per side, not inline at each call.
    def target_or_unit(self) -> str:
        """The target this unit is checked by — its own name unless it shares one."""
        return self.target or self.unit


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
    #: ``(input_json, spec, target, workdir, sandbox_json) -> ValidateOutcome`` JSON. **Blocking.**
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
