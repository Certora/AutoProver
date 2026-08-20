"""Step 0: classify the function list into MODEL / OBS, and derive default bindings.

MODEL   = state-changing methods -> each gets an <f>CVL + a conformance pair.
OBS     = view/pure getters      -> each defines a pi element (ghost + glue equality).
HARNESS = requested getters for quantities a MODEL method needs but OBS doesn't cover
          (the sufficiency requirement); surfaced, not auto-created.
"""
from __future__ import annotations

from dataclasses import dataclass

from .ir import Binding, FunctionSpec


@dataclass
class ModelLayout:
    """How an input function list is organized for modeling (the output of `classify`):
    `model` = the state-changing methods to model, `getters` = all view/pure getters (declared by the
    template), `bindings` = the observable→ghost correspondences derived for the observable getters."""
    model: list[FunctionSpec]
    getters: list[FunctionSpec]
    bindings: list[Binding]


def default_ghost_name(getter_name: str) -> str:
    """Uniform: keep the Solidity name + `CVL`.
    getFoo -> getFooCVL ; balanceOf -> balanceOfCVL. (cosmetic; overridable)"""
    return getter_name + "CVL"


def default_reader_name(getter_name: str) -> str:
    """The model reader function — the Solidity name + `CVLReader`, distinct from the ghost (`<g>CVL`)
    and the real getter. getFoo -> getFooCVLReader ; balanceOf -> balanceOfCVLReader."""
    return getter_name + "CVLReader"


def binding_for(getter: FunctionSpec) -> Binding:
    """Derive the model Binding for an observable getter: the ghost/reader names (overridable defaults),
    the mapping key types (= the getter's param types), and the tracked value type (= the single return,
    or the `bind_component`-th return for a multi-return getter). Raises if a multi-return getter didn't
    say which component the ghost tracks."""
    if len(getter.returns) == 1:
        val_type = getter.returns[0]
        base = getter.name
    else:
        if getter.bind_component is None:
            raise ValueError(
                f"getter {getter.name} has {len(getter.returns)} returns; set "
                f"bind_component to say which one the ghost tracks.")
        val_type = getter.returns[getter.bind_component]
        # Multi-return: each component gets its OWN ghost + reader, so their default names MUST
        # disambiguate by component — else both bindings collapse onto `<getter>CVL` and the model
        # spec redeclares the ghost / overloads the reader (a typecheck error the agent can't fix).
        # Prefer the caller's component_names label; fall back to the component index.
        cn = getter.component_names
        comp = getter.bind_component
        suffix = cn[comp] if (cn and comp < len(cn)) else str(comp)
        base = f"{getter.name}_{suffix}"
    return Binding(
        getter=getter,
        ghost_name=getter.ghost_name or default_ghost_name(base),
        reader_name=getter.reader_name or default_reader_name(base),
        key_types=[p.type for p in getter.params],
        val_type=val_type,
    )


def classify(functions: list[FunctionSpec]) -> ModelLayout:
    """Split the input functions into MODEL (state-changing → each gets an <f>CVL + conformance) and
    getters (view/pure), and derive a Binding for each OBSERVABLE getter. All view getters are declared
    by the template; only observable ones become model ghosts."""
    model = [f for f in functions if f.is_model_method]
    # a view flagged model=True is a return-only MODEL method (a computed view), not a ghost observable
    getters = [f for f in functions if f.is_getter and not f.model]
    # all (non-model) view getters are DECLARED by the template; only observable ones are modeled as ghosts
    bindings = [binding_for(g) for g in getters if g.observable]
    return ModelLayout(model=model, getters=getters, bindings=bindings)
