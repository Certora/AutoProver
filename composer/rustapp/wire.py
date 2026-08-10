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

import json
from collections import Counter
from typing import TYPE_CHECKING, Annotated, Any, Callable, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from composer.spec.source.report.schema import Outcome
from composer.spec.types import CheckName, ComponentName, PropertyTitle, PropertyType

# ``TargetName``: what a backend selects when it invokes the checker — one :class:`Target`'s name
# (Crucible: the component's harness fn / Cargo feature). Phantom-typed like the vocabulary in
# :mod:`composer.spec.types`, and defined here because targets exist only on this seam. A sibling
# of ``CheckName``: a check that shares no target is *named after* its check name
# (:meth:`Check.target_or_name`), but the two namespaces are otherwise distinct.
if TYPE_CHECKING:
    class TargetName(str): ...
else:
    TargetName = str


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
    """One property to formalize, the unit it was inferred for, and the ``slug`` the host assigned
    it — unique within the batch, and what a backend names this property's :class:`Check` after
    (Crucible: ``c_<slug>``)."""

    #: The unit whose analysis produced this property (``FeatureUnit.display_name``, the report's
    #: component name). A title identifies a property only within its own unit, so a setup spec —
    #: which is sent every unit's properties at once — needs this to tell two same-titled ones apart
    #: and to know which unit's surface each has to be checkable against.
    component: ComponentName
    title: PropertyTitle
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
    source_path: str
    #: The design doc, when one was named on the command line.
    system_doc: str | None = None
    #: Where the analyzed code lives as a unit of its own build system, chain-shaped — see
    #: :attr:`_AuthorInputBase.source_unit`. These two callouts run before workspace prep, so there
    #: are no prep facts to accompany it.
    source_unit: dict[str, Any] = Field(default_factory=dict)
    #: The wheel's own declared flags, keyed by argparse dest (``--fuzz-timeout`` →
    #: ``fuzz_timeout``). Untyped: the *wheel* declares these, so the host has no schema for them.
    declared: dict[str, Any] = Field(default_factory=dict)


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
    #: **Every** property the run extracted, across all units, each naming the unit that owns it —
    #: run-level context like :attr:`prep_facts`, not something this artifact is answerable for.
    #: A shared setup spec is built into every unit's target, so a failure it reports can name a
    #: property belonging to a *different* unit; without the run's set a wheel cannot tell that from
    #: a title it has never seen, and the safe reading of an unplaceable failure — refute everything
    #: the target covers — is exactly wrong for the first case. Empty wherever the host does not hold
    #: the whole set at once: a preflight, and any wheel declaring no
    #: :class:`~composer.rustapp.descriptor.SetupSpec`.
    run_props: list[Property] = Field(default_factory=list)
    #: The compiled shared setup spec, for a wheel that declared a
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
        """This input with ``props`` replaced — the setup spec's base input plus the properties
        it has to make checkable, which only exist after extraction.

        Sets :attr:`run_props` to the same list: the setup spec is authored from *every* unit's
        properties, so on that turn the two coincide."""
        return self.model_copy(update={"props": props, "run_props": props})

    def with_prep_facts(self, prep_facts: dict[str, Any]) -> Self:
        """This input with what the workspace prep just established (the preflight gate re-renders
        the prep's input, and must see the workspace the prep actually set up)."""
        return self.model_copy(update={"prep_facts": prep_facts})


class PreflightInput(_AuthorInputBase):
    """Gate the prepared workspace before anything is authored: the wheel renders its own skeleton
    and ``compile`` builds it. Runs before analysis finishes, so it carries no model and no unit."""

    kind: Literal["preflight"] = "preflight"


class SetupInput(_AuthorInputBase):
    """Author the one shared spec every unit builds on, from the analyzed model and *every* unit's
    properties."""

    kind: Literal["setup"] = "setup"
    #: The analyzed system model. Opaque to the host seam — its shape is the ecosystem's.
    model: dict[str, Any] = Field(default_factory=dict)
    #: Every unit the run is about to formalize (``FeatureUnit.feature_json()``), as
    #: :attr:`CrateRootInput.units` carries them. The host holds the set at this point in the run, so
    #: a wheel whose setup gate builds the whole crate's scaffolding renders it here rather than a
    #: provisional form the crate-root hook would then complete. Not part of the setup cache identity
    #: (:func:`composer.rustapp.adapter._setup_identity`) — it does not change what gets authored.
    units: list[dict[str, Any]] = Field(default_factory=list)

    def with_units(self, units: list[dict[str, Any]]) -> Self:
        """This input with the run's unit set attached — known at the same moment :meth:`with_props`
        is applied, and kept separate for the same reason it is excluded from the cache identity."""
        return self.model_copy(update={"units": units})


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


class SkippedProperty(WireModel):
    """A property the author declined to formalize, with its justification. Mirrors the Rust
    ``SkippedProperty`` and carries the same fields as the host's own
    :class:`composer.authoring.state.SkippedProperty`, which is what it is built from."""

    property_title: PropertyTitle
    reason: str


class Delivered(WireModel):
    """What a component that reached the deliverable produced. Mirrors the Rust ``Delivered``."""

    status: Literal["delivered"] = "delivered"
    artifact_text: str = ""
    #: The validation targets this component's checks ran under, in the order they ran — what a
    #: callout-mode wheel keys its deliverable sections and declared features on.
    targets: list[TargetName] = Field(default_factory=list)
    property_checks: list[tuple[PropertyTitle, list[CheckName]]] = Field(default_factory=list)
    #: What the author declined to formalize, and why — disjoint from :attr:`property_checks`.
    skipped: list[SkippedProperty] = Field(default_factory=list)
    unit_file: str | None = None
    run_link: str | None = None


class ComponentGaveUp(WireModel):
    """Formalization gave up on this component: the author reached the point where anything it could
    publish would only *look* checked, and said so instead. Mirrors the Rust ``GaveUp``.

    It produced no spec and ran no build, so unlike :class:`Delivered` it has no ``targets`` — which
    is why it carries its ``unit``. A wheel whose deliverable declares a build target per unit needs
    to name the one behind this component, and re-deriving that name by re-slugifying ``name`` would
    put the host's slug rule in a second language."""

    status: Literal["gave_up"] = "gave_up"
    #: The unit being formalized, as its component callouts received it (``FeatureUnit.feature_json``).
    unit: dict[str, Any] = Field(default_factory=dict)
    #: The author's own account of why it stopped, from the give-up tool. Surfaced to the user, so it
    #: must not be reshaped into something that reads like a finding.
    reason: str = ""


#: A component's outcome — tagged on ``status`` (Rust ``ComponentOutcome``). A variant rather than a
#: ``delivered`` flag beside always-present fields: the two share no data.
ComponentOutcome = Annotated[Delivered | ComponentGaveUp, Field(discriminator="status")]


class CrateRootInput(WireModel):
    """What a wheel needs to render its build's scaffolding once, at the one point both halves are
    known: after the shared setup spec is authored, and before the units fan out (Rust
    ``CrateRootInput``).

    Scaffolding for a multi-unit build depends on the **whole unit set** — a Cargo manifest's feature
    list, a crate root's module declarations — which no per-unit callout can see. The host writes
    what comes back and does not write it again, so the wheel's per-unit callouts can emit only that
    unit's own files."""

    program: str
    source_unit: dict[str, Any] = Field(default_factory=dict)
    prep_facts: dict[str, Any] = Field(default_factory=dict)
    setup: str | None = None
    #: Every unit about to be formalized, in fan-out order — each the same object a component
    #: callout receives. The field the hook exists for.
    units: list[dict[str, Any]] = Field(default_factory=list)


class FinalizeComponent(WireModel):
    """One component's line in the ``finalize`` payload."""

    name: ComponentName
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
    #: The compiled shared setup spec, when the wheel declared one.
    setup: str | None = None


# ---------------------------------------------------------------------------
# Inbound — what a callout returns.
# ---------------------------------------------------------------------------

class Prompt(WireModel):
    """An authoring instruction for one LLM turn, plus an optional backend-defined system prompt
    (``None`` → the host's neutral default)."""

    instruction: str
    system: str | None


class Judge(WireModel):
    """The reviewer a wheel declares for an input — what is fixed for the whole authoring session,
    which is why ``judge`` is asked once and without a draft. What to ask about a *particular* draft
    is the per-round ``judge_instruction``."""

    #: The domain half of the reviewer's system prompt (``None`` → the host's neutral role). The
    #: host appends the review protocol either way.
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


class Check(WireModel):
    """One check the *author* declared: the backend's name for a runnable verification — a CVL rule,
    a foundry test, a tagged fuzz assertion. A check yields a :class:`Verdict` and becomes one row of
    the report.

    :attr:`properties` is what the author declared this check verifies — the mapping's own claim,
    carried verbatim rather than guessed by the wheel, and never empty (a check exists *because*
    something claimed it). Usually one title; several when one rule discharges several related
    invariants. It is here because some checkers speak in properties rather than in check names
    (Crucible tags each assertion message with its property title, so this is what lets it place a
    counterexample), while a backend whose checker reports per check can ignore it.

    ``target`` names the :class:`Target` this check runs under — one invocation of the checker,
    answered by the wheel's ``target_for``. Several checks may share one, so the host runs each
    distinct target once and the wheel attributes the outcome back to each check."""

    name: CheckName
    properties: list[PropertyTitle]
    target: TargetName | None

    # Mirrors the Rust ``Check::target_or_name``.
    def target_or_name(self) -> TargetName:
        """The target this check runs under — its own name unless it shares one."""
        return self.target or TargetName(self.name)


class Target(WireModel):
    """One invocation of the checker — one build + one run — and the checks it covers, which is what
    ``validate`` must return a verdict for. Mirrors the Rust ``Target``.

    Targets group *running*; checks group *reporting*. A target sits inside one unit's session, so
    the three nest: a unit's checks partition into its targets. A backend that checks a whole
    property set in one run therefore pays for one build and still reports a row per property.

    The host owns the grouping (it decides what to run, and in what order), so it hands the answer
    over rather than leaving the wheel to recover it by re-deriving its own ``checks`` and filtering
    them by name."""

    #: What the backend selects when it runs the checker (Crucible: the component's harness fn, which is also its Cargo
    #: feature).
    name: TargetName
    #: Usually one; several when a backend checks a whole property set in one run.
    checks: list[Check] = Field(default_factory=list)


class Verdict(WireModel):
    """One check's outcome. Mirrors the Rust ``Verdict`` and maps onto the report's
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
    """The shared build failed, so nothing was checked — no check got a verdict."""

    kind: Literal["build_failed"]
    errors: str


class ValidateCoverageError(RuntimeError):
    """``validate`` answered with a verdict set that is not the target's check set.

    A wheel bug, not a spec the author can fix, so it raises where a build failure returns a revise
    prompt."""


class ValidateVerdicts(WireModel):
    """It built, and every check the target covers got a verdict, ``(check_name, verdict)``."""

    kind: Literal["verdicts"]
    verdicts: list[tuple[CheckName, Verdict]]

    def resolve(self, target: Target) -> list[tuple[Check, Verdict]]:
        """Each of the target's checks paired with the verdict the wheel returned for it, in the
        target's own order.

        A verdict is keyed by check name on the wire so a wheel picks from the checks the host sent
        rather than restating them (a restated :class:`Check` could contradict the property→check
        map the host published). Resolving that key here is what makes the pairing a `Check` for
        everything upstream, and is the only place the docstring's "every check got a verdict" is
        established: a name no check has, a check left without a verdict, or the same check twice
        raises. Silence would be worse than a wrong verdict — a check whose verdict never arrives is
        one the publish gate has nothing to object to, so an empty answer would stamp a component
        nothing checked."""
        covered = [c.name for c in target.checks]
        answered = Counter(name for name, _ in self.verdicts)
        by_name = {name: verdict for name, verdict in self.verdicts}
        missing = [n for n in covered if n not in by_name]
        unknown = sorted(set(by_name) - set(covered))
        repeated = sorted(n for n, count in answered.items() if count > 1)
        if missing or unknown or repeated:
            raise ValidateCoverageError(
                f"target {target.name!r} covers {covered}, but validate answered for "
                f"{list(answered)}"
                + (f"; no verdict for {missing}" if missing else "")
                + (f"; no such check {unknown}" if unknown else "")
                + (f"; more than one verdict for {repeated}" if repeated else "")
            )
        return [(c, by_name[c.name]) for c in target.checks]


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


class CalloutError(WireModel):
    """Why a callout produced no payload. Mirrors the Rust ``CalloutError``.

    Success is the payload type unchanged; this is the only extra inbound JSON shape. A host bug
    (bad input JSON, a serialize failure) is this, not an empty plan, a skipped review, or a
    failed build the author should revise."""

    kind: Literal["error"]
    message: str


class CalloutFailed(RuntimeError):
    """A callout could not produce its payload — the Python face of :class:`CalloutError`."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


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
    #: ``(input_json, check) -> target | None``. Which invocation a declared check runs under;
    #: ``None`` makes it its own. Pure — the check *names* are the author's, and only the grouping
    #: is the wheel's to say.
    target_for: Callable[[str, str], TargetName | None]
    #: ``(input_json) -> Prompt`` JSON. Asked once per authoring session — the session keeps its
    #: own history, so there is no per-attempt revise prompt.
    author_prompt: Callable[[str], str]
    #: ``(input_json, spec) -> error | None``. Pure and cheap: the put-time gate on the buffer.
    check_syntax: Callable[[str, str], str | None]
    #: ``(input_json) -> Judge | None`` JSON. ``None`` ⇒ this wheel does not review this input.
    #: Takes no spec: it is asked once, when the session is built, and neither answer depends on a
    #: draft.
    judge: Callable[[str], str | None]
    #: ``(input_json, spec) -> str``. The instruction for one review round — the text itself, not
    #: JSON. Called only for an input ``judge`` claimed.
    judge_instruction: Callable[[str, str], str]
    #: ``(input_json, spec | None, workdir, sandbox_json) -> CompileResult`` JSON. ``None`` is the
    #: preflight, where nothing has been authored and the wheel builds its own skeleton — distinct
    #: from an authored spec that happens to be empty. **Blocking.**
    compile: Callable[[str, str | None, str, str], str]
    #: ``(input_json, spec, target_json, workdir, sandbox_json) -> ValidateOutcome`` JSON, where
    #: ``target_json`` is a :class:`Target` — the target to run and the checks it covers.
    #: **Blocking.**
    validate: Callable[[str, str, str, str, str], str]
    #: ``(input_json) -> WorkspacePrep`` JSON. Pure — the host executes the plan.
    workspace_prep: Callable[[str], str]
    #: ``(args_json) -> SandboxGrants`` JSON.
    sandbox_grants: Callable[[str], str]
    #: ``(input_json) -> {relpath: contents} | None`` JSON, from a :class:`CrateRootInput`. Pure.
    #: Called once per run, between the setup step and fan-out; the host writes the result and does
    #: not rewrite it, so a wheel that implements this emits only per-unit files from its gates.
    crate_root: Callable[[str], str | None]
    #: ``(outcomes_json) -> {relpath: contents} | None`` JSON.
    finalize: Callable[[str], str | None]


#: Every callout name the host may call, derived from :class:`RustAppModule` so the two can't drift.
#: Used to reject a module that isn't an AutoProver wheel (or is one built against an older SDK)
#: at load, with the missing callouts named.
CALLOUTS: tuple[str, ...] = tuple(RustAppModule.__annotations__)


# ---------------------------------------------------------------------------
# Parsing — one function per callout return, so every ``json.loads`` of a wheel's answer happens
# here and nowhere else. A malformed payload raises ``pydantic.ValidationError`` naming the field.
# A :class:`CalloutError` envelope raises :class:`CalloutFailed` before the payload parser runs.
# ---------------------------------------------------------------------------

_COMPILE_RESULT: TypeAdapter[CompileOk | CompileFailed] = TypeAdapter(CompileResult)
_VALIDATE_OUTCOME: TypeAdapter[ValidateBuildFailed | ValidateVerdicts] = TypeAdapter(ValidateOutcome)
_FILES: TypeAdapter[dict[str, str]] = TypeAdapter(dict[str, str])


def expect_payload(raw: str) -> str:
    """Return ``raw`` unless it is the :class:`CalloutError` envelope, in which case raise
    :class:`CalloutFailed`.

    Non-JSON text (a target name, a ``judge_instruction``, a precondition complaint) is returned
    as-is: those channels are not JSON, and only an envelope is a wire-level failure."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(data, dict) or data.get("kind") != "error":
        return raw
    raise CalloutFailed(CalloutError.model_validate(data).message)


def expect_text[T: str](raw: T | None) -> T | None:
    """``None`` is a successful empty answer. A :class:`CalloutError` envelope raises."""
    if raw is None:
        return None
    expect_payload(raw)
    return raw


def parse_compile(raw: str) -> CompileOk | CompileFailed:
    return _COMPILE_RESULT.validate_json(expect_payload(raw))


def parse_validate(raw: str) -> ValidateBuildFailed | ValidateVerdicts:
    return _VALIDATE_OUTCOME.validate_json(expect_payload(raw))


def parse_prompt(raw: str) -> Prompt:
    return Prompt.model_validate_json(expect_payload(raw))


def parse_judge(raw: str) -> Judge:
    return Judge.model_validate_json(expect_payload(raw))


def parse_workspace_prep(raw: str) -> WorkspacePrep:
    return WorkspacePrep.model_validate_json(expect_payload(raw))


def parse_sandbox_grants(raw: str) -> SandboxGrants:
    return SandboxGrants.model_validate_json(expect_payload(raw))


def parse_files(raw: str) -> dict[str, str]:
    """``finalize``'s ``{relpath: contents}`` map."""
    return _FILES.validate_json(expect_payload(raw))
