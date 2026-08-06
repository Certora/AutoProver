"""Scene-sourced input front-end: build smtool `ToolInput`s from an IDENTIFIER-LIST config + the
compiled scene, instead of hand-typing signatures.

The per-CUT config is pure data — for each method to model, its name (+ `model=True` for a computed
view) and the observables it corresponds on, each given by NAME plus the RESIDUAL knobs that are NOT
scene facts: keying (`key`), state-effect (`se`), env-freeness (`envfree`), and the multi-return
component. Everything else — params, returns, mutability, visibility, ghost/reader names — is pulled
from `all_methods.json` via `FunctionSpec.from_scene` (see scene.py) and the driver's default names.

An input module using this exposes `CUT` + `MODEL` (the data) + `build(scene_path)`; `run_smtool --scene`
calls `build` with the compiled scene under the setup's sources tree. See demo/spoke_hub_input_scene.py.

Residual knobs are what a future static front-end would DERIVE (env-read pass for `envfree`; storage
write-set / access-path analysis for `se` + `key`). Until then they're explicit, with conservative
intent (frame mapping observables universally; check every observable the consumer reads).

Setup getters (a CVL getter backed by the setup, e.g. `tokenBalanceOf`) are NOT in `all_methods.json`,
so an observable may carry an explicit `params`/`returns` + `host="setup"` to hand-source that one
signature; CUT getters need none of that.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .ir import ToolInput, FunctionSpec, Param, CUT_ARG, CALLER_ARG, free_var
from . import scene as _scene


# ---- config records (pure data; no scene needed to construct) ----

@dataclass
class Method:
    """A CUT method (or a `model=True` computed view) to model. Signature comes from the scene."""
    name: str
    model: bool = False


@dataclass
class Obs:
    """An observable getter the method corresponds on. Facts from the scene; only residual knobs here.

    key: {param_name: 'caller'|'cut'|'free'|'<local>'} — how to key the getter. 'caller' => e.msg.sender,
         'cut' => the CUT address, 'free' => a fresh universally-quantified var in the state rule (the
         glue, which can't take a free var, falls back to 'caller'), any other string => a derived local
         (e.g. 'u'). Absent params keep their own name. None => the getter's own params (identity keying).
    se: compare this observable's POST value in the state-effect rule (the write-set residual).
    envfree: None => default (a getter is envfree); set False for a getter that reads block/msg (accrual).
    component: multi-return getter — which return the ghost tracks.
    host: 'cut' (default) or 'setup' (a setup CVL getter -> not declared, sig from `params`/`returns`).
    glue_return: this (multi-return) getter's tracked component is the glue's return (a derived key like `u`).
    component_names: local names for a multi-return tuple (e.g. ['u','d']); default c0,c1,...
    params/returns: ONLY for a setup getter absent from the scene (hand-sourced signature).
    """
    name: str
    key: dict | None = None                # keying for BOTH glue and frame (unless overridden below)
    glue_key: dict | None = None           # glue-only keying override (falls back to `key`)
    frame_key: dict | None = None          # frame-only keying override (falls back to `key`)
    se: bool = True
    envfree: bool | None = None
    component: int | None = None
    host: str = "cut"
    glue_return: bool = False
    component_names: list[str] | None = None
    params: list[tuple] | None = None      # setup getter: [(type,name),...]
    returns: list[str] | None = None       # setup getter: [type,...]


# ---- helpers so a config module reads terse ----

def method(name: str, *, model: bool = False) -> Method:
    return Method(name, model=model)

def obs(name: str, **knobs) -> Obs:
    return Obs(name, **knobs)


class SceneInput:
    """Bound to a compiled scene; turns `Method`/`Obs` records into `FunctionSpec`s with facts sourced
    from `all_methods.json` (CUT methods/getters) + auto-derived names."""

    def __init__(self, cut: str, certora_internal_path: str):
        self.cut = cut
        self.parser = _scene.load_methods(certora_internal_path)
        try:
            self._resolve = _scene.type_resolver(certora_internal_path)
        except Exception:
            self._resolve = lambda s: s   # fallback: identity (fine for primitive types)

    def _scene_dict(self, name: str) -> dict:
        d = _scene.method_dict(self.parser, self.cut, name)
        if d is None:
            raise ValueError(f"method {self.cut}.{name} not in the scene's all_methods.json "
                             f"(a setup getter needs explicit params/returns; a harness getter needs "
                             f"the harness compiled into the scene)")
        return d

    # ---- keying ----
    def _keys(self, params: list[Param], override: dict | None, *, frame: bool) -> list[str]:
        out = []
        for p in params:
            o = (override or {}).get(p.name)
            if   o == "caller": out.append(CALLER_ARG)
            elif o == "cut":    out.append(CUT_ARG)
            elif o == "free":   out.append(free_var(p.type, p.name) if frame else CALLER_ARG)
            elif o is None:     out.append(p.name)
            else:               out.append(o)      # a derived local (e.g. 'u')
        return out

    # ---- builders ----
    def method_spec(self, m: Method) -> FunctionSpec:
        return FunctionSpec.from_scene(self._scene_dict(m.name), self._resolve, model=m.model)

    def obs_spec(self, o: Obs) -> FunctionSpec:
        if o.host == "setup":
            if o.params is None or o.returns is None:
                raise ValueError(f"setup getter {o.name} needs explicit params/returns (not in the scene)")
            params = [Param(self._resolve(t), n) for t, n in o.params]
            sig_params = params
            sig = FunctionSpec.of(o.name, params, [self._resolve(t) for t in o.returns], "view",
                                  observable=True, getter_host="setup", declare_in_methods=False)
        else:
            d = self._scene_dict(o.name)
            sig = FunctionSpec.from_scene(d, self._resolve, observable=True)
            sig_params = sig.params
        # residual knobs
        if o.envfree is not None:
            sig.envfree = o.envfree
        sig.state_effect = o.se
        if o.component is not None:
            sig.bind_component = o.component
        if o.component_names is not None:
            sig.component_names = o.component_names
        sig.glue_return = o.glue_return
        gk = o.glue_key if o.glue_key is not None else o.key
        fk = o.frame_key if o.frame_key is not None else o.key
        if gk is not None:
            sig.glue_args = self._keys(sig_params, gk, frame=False)
        if fk is not None:
            sig.frame_args = self._keys(sig_params, fk, frame=True)
        return sig

    def tool_input(self, m: Method, observables: list[Obs]) -> ToolInput:
        return ToolInput(cut=self.cut,
                         functions=[self.method_spec(m)] + [self.obs_spec(o) for o in observables])


def build_specs(cut: str, certora_internal_path: str, model: dict):
    """`model` = {method_name: (Method, [Obs, ...])} OR {method_name: [Obs, ...]} (method defaults).
    Returns (specs_fn, all_methods) matching run_smtool's --input contract: specs(methods)->[ToolInput],
    ALL_METHODS list."""
    si = SceneInput(cut, certora_internal_path)
    def entry(name, v):
        if isinstance(v, tuple):
            m, obss = v
        else:
            m, obss = method(name), v
        return si.tool_input(m, obss)
    inputs = {name: entry(name, v) for name, v in model.items()}
    all_methods = list(model)
    return (lambda methods=None: [inputs[x] for x in (methods or all_methods)]), all_methods
