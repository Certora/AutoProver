"""Input model + internal IR for the symbolic-model tool.

Two inputs (see INPUT.md): a setup .conf (assumed correct) naming the CUT, and a
flat list of CUT functions. classify.py splits the list into MODEL / OBS / HARNESS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Mutability = Literal["pure", "view", "nonpayable", "payable"]

# glue_args / frame_args sentinels (the driver's _resolve_arg / _frame_resolve interpret these):
CUT_ARG = "CUT"          # this arg is the CUT address (resolves to currentContract)
CALLER_ARG = "CALLER"    # this arg is the CALLING account (resolves to e.msg.sender) — used to key a
                         # per-caller observable (e.g. a getter `g(id, account)`) at the caller, so
                         # glue/stateEffect pin the model's slot for THIS caller (single-key scope).
FREE_PREFIX = "FREE:"    # a frame arg "FREE:<type>:<name>" declares a fresh free var (frames over all)


def free_var(ty: str, name: str) -> str:
    """A frame_args entry for a fresh free var of type `ty` named `name` (frames over all its values)."""
    return f"{FREE_PREFIX}{ty}:{name}"


@dataclass
class Param:
    type: str          # CVL type name (e.g. "uint256", "address", or a dotted "IFoo.Bar" struct type)
    name: str


@dataclass
class Signature:
    """The resolved signature of a CUT function — name, params, returns, mutability, visibility — in
    the CVL type-string form smtool emits (what `cvlx.ty` decodes). This is NOT hand-authored
    knowledge: in the real flow it is sourced from the compiled scene via `from_scene` (see
    smtool/scene.py), reusing AutoProver's `MethodParser` (loader) +
    `TypeAnalyzer._resolve_type_from_string().cvl_name` (the Solidity->CVL string mapper autosetup
    itself uses). We stay string-native and anchored to the composer/EVMVerifier CVL AST (via cvlx),
    rather than coupling to autosetup's parallel TypeInfo."""
    name: str
    params: list[Param] = field(default_factory=list)
    returns: list[str] = field(default_factory=list)   # CVL return type names
    mutability: Mutability = "nonpayable"
    visibility: str = "external"

    @classmethod
    def from_scene(cls, m: dict, resolve) -> "Signature":
        """Build FACTS from an `all_methods.json` entry `m` (as loaded by AutoProver's `MethodParser`).
        `resolve` maps a Solidity type string -> its CVL spelling (wire it to
        `TypeAnalyzer._resolve_type_from_string(s).cvl_name`; see smtool/scene.py). No hand-typing."""
        types = m.get("fullSignature", [])
        names = list(m.get("paramNames") or [])
        if len(names) < len(types):                     # unnamed / partially-named params -> synthesize
            names += [f"p{i}" for i in range(len(names), len(types))]
        params = [Param(resolve(t), n) for t, n in zip(types, names)]
        returns = [resolve(t) for t in m.get("returns", [])]
        return cls(name=m["name"], params=params, returns=returns,
                   mutability=m.get("stateMutability", "nonpayable"),
                   visibility=m.get("visibility", "external"))


@dataclass
class FunctionSpec:
    """One CUT function from input (2): its SIGNATURE (from the scene) plus smtool's MODELING
    directives. Mutability (part of the signature) drives classification. Construct offline via
    `FunctionSpec.of(...)` or from a scene via `FunctionSpec.from_scene(mdict, resolve, **modeling)`."""
    signature: Signature

    # ---- MODELING directives (smtool-specific; the AI agent / user chooses these) ----
    envfree: bool | None = None    # getter only: declare/call envfree? None => default = is_getter
    model: bool = False            # force a view/pure fn to be MODELED as a return-only method (agent-
                                   # filled body + return conformance, NO stored ghost / NO state-effect)
                                   # — for COMPUTED views (preview/quote/convert) whose value is derived
                                   # from other model state, not stored. State-changing fns are always
                                   # modeled; this is only for views.
    observable: bool = True        # getter only: modeled as a ghost (True) vs declared-only support
                                   # getter used solely to state a reachable invariant (False).
                                   # Declaration is template either way.

    # ---- getter-as-observable knobs (generalize where the getter comes from & how it's keyed) ----
    getter_host: str = "cut"       # "cut" => a CUT getter g(..) (unqualified => currentContract);
                                   #   "setup" => a setup CVL getter backed by a setup ghost
    declare_in_methods: bool = True  # emit an envfree methods{} decl? (setup CVL getters already exist)
    ghost_name: str | None = None    # override the default model-ghost name (cosmetic; shape is derived)
    reader_name: str | None = None   # override the default model-reader name
    bind_component: int | None = None  # multi-return getter: which return index the ghost tracks
    component_names: list[str] | None = None  # multi-return: local names for the tuple, e.g. ["u","d"]
    glue_args: list[str] | None = None  # arg NAMES for the getter/reader in the glue; CUT_ARG => the CUT.
                                        # None => the getter's own param names. Lets a binding be keyed
                                        # by a derived local (e.g. the underlying `u`) + the CUT address.
    glue_return: bool = False        # the bound local of this (multi-return) getter is the glue's return
    return_compare: list[bool] | None = None  # MODEL method w/ tuple return: which components to
                                              # compare in the return rule (others over-approximated
                                              # / NONDET). None => compare all.
    state_effect: bool = True        # compare this observable's POST value in the stateEffect rule.
                                     # Safety-by-default: ALL observables are compared (whole pi), so an
                                     # effect the model gets wrong is caught. Set False to opt an
                                     # observable OUT (e.g. one not yet provable whole) — a narrowing
                                     # that is also the future perf optimization (see build_state_effect_rule).
    frame_args: list[str] | None = None  # stateEffect arg names; free_var(type,name) => fresh free var
                                          # (framing over all e.g. accounts), else resolves like glue_args

    # ---- constructors ----
    @classmethod
    def of(cls, name: str, params=(), returns=(), mutability: Mutability = "nonpayable",
           visibility: str = "external", **modeling) -> "FunctionSpec":
        """Terse offline constructor (recon agent-sims): build the SIGNATURE inline + MODELING kwargs.
        In the real flow prefer `from_scene`, which sources the signature from the compiled scene."""
        return cls(Signature(name, list(params), list(returns), mutability, visibility), **modeling)

    @classmethod
    def from_scene(cls, m: dict, resolve, **modeling) -> "FunctionSpec":
        """SIGNATURE from an `all_methods.json` entry (via `Signature.from_scene`) + MODELING kwargs."""
        return cls(Signature.from_scene(m, resolve), **modeling)

    # ---- signature proxies (keep classify/driver/project/Binding reading .name/.params/... unchanged) ----
    @property
    def name(self) -> str:
        return self.signature.name

    @property
    def params(self) -> list[Param]:
        return self.signature.params

    @property
    def returns(self) -> list[str]:
        return self.signature.returns

    @property
    def mutability(self) -> Mutability:
        return self.signature.mutability

    @property
    def visibility(self) -> str:
        return self.signature.visibility

    @property
    def is_getter(self) -> bool:
        return self.mutability in ("view", "pure")

    @property
    def is_state_changing(self) -> bool:
        return self.mutability in ("nonpayable", "payable")

    @property
    def is_model_method(self) -> bool:
        """Modeled as an <f>CVL method (return conformance + agent-filled body): every state-changing
        fn, plus any view/pure fn flagged `model=True` (a computed view)."""
        return self.is_state_changing or self.model

    @property
    def effective_envfree(self) -> bool:
        return self.is_getter if self.envfree is None else self.envfree


@dataclass
class Binding:
    """Correspondence between a model ghost and a real OBS getter (an element of pi).

    The ghost's SHAPE is deterministic from the getter signature (key = params,
    value = the tracked return); the ghost/reader NAMES are defaults the AI may rename.
    """
    getter: FunctionSpec
    ghost_name: str
    reader_name: str
    key_types: list[str]   # getter param types -> mapping key nesting
    val_type: str          # tracked return type -> mapping value / scalar type

    @property
    def getter_host(self) -> str:
        return self.getter.getter_host

    @property
    def envful(self) -> bool:
        return not self.getter.effective_envfree

    @property
    def is_multi_return(self) -> bool:
        return len(self.getter.returns) > 1

    @property
    def component_index(self) -> int:
        return self.getter.bind_component if self.getter.bind_component is not None else 0

    @property
    def component_names(self) -> list[str]:
        return self.getter.component_names or [f"c{i}" for i in range(len(self.getter.returns))]

    @property
    def glue_arg_names(self) -> list[str]:
        return (self.getter.glue_args if self.getter.glue_args is not None
                else [p.name for p in self.getter.params])

    @property
    def glue_return(self) -> bool:
        return self.getter.glue_return

    @property
    def state_effect(self) -> bool:
        return self.getter.state_effect

    @property
    def frame_arg_names(self) -> list[str]:
        return self.getter.frame_args if self.getter.frame_args is not None else self.glue_arg_names


@dataclass
class ToolInput:
    cut: str                       # contract NAME == currentContract (verify target); qualifies methods{}
    functions: list[FunctionSpec]
    model_spec_name: str | None = None  # override; default derived from the CUT (see `model_spec`)
    conformance_prefix_name: str | None = None  # override; default derived from the CUT (see below)
    specs_dir: str = "certora/specs"  # where verify points, for the conf rewrite

    @property
    def model_spec(self) -> str:
        """The model spec filename — used both as the import in each conformance spec and as the
        written file. Defaults to `Symbolic<CUT>Model.spec`; override via `model_spec_name`."""
        return self.model_spec_name or f"Symbolic{self.cut}Model.spec"

    @property
    def conformance_prefix(self) -> str:
        """Prefix for the per-method conformance spec files, `<prefix><Method>Conformance.spec`.
        Defaults to the CUT name (generic — not protocol-specific); override via
        `conformance_prefix_name`."""
        return self.conformance_prefix_name or self.cut

    @property
    def reachable_spec(self) -> str:
        """The dedicated shared reachability spec filename (assumeReachable + CUT invariants),
        imported by every conformance spec. Derived from the CUT, like `model_spec`."""
        return f"{self.cut}Reachable.spec"

    @property
    def summary_spec(self) -> str:
        """The CONSUMER summary-application spec filename (imports the model, summarizes each CUT fn ->
        model). A downstream proof imports THIS to run against the model instead of the real CUT."""
        return f"Symbolic{self.cut}Summary.spec"
