"""Project: the mutable set of artifacts the mutation tools operate on.

Holds the model spec + per-method conformance specs (as CVL AST) + confs. Mutations
apply on a deep copy, then the project validates (structural + linter); a mutation is
committed only if validation is clean, otherwise rejected — so the project is always
discipline-compliant by construction of its mutators.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path

import composer.cvl.schema as S
from composer.cvl.pretty_print import pretty_print

from .ir import ToolInput
from .classify import classify, ModelLayout
from . import driver

# Perf settings applied to every generated conf, to speed up the heavy CUT conformance runs:
#  - prover_args (as {name: value}, empty value == a bare flag): `-calltraceFreeOpt twostage` (defers
#    call-trace construction), `-split false` (disables splitting), `-backendStrategy singlerace` (single
#    solver race) — these almost always help here.
#  - top-level `smt_timeout`: a MODEST budget (default 600s) so hard instances TIME OUT FAST and the
#    refine loop reaches the timeout-resolution step sooner, rather than burning ~25min on a doomed
#    proof. Override via env `SMTOOL_SMT_TIMEOUT` (e.g. 300 for quick debugging).
import os as _os
PERF_PROVER_ARGS = {"calltraceFreeOpt": "twostage", "split": "false", "backendStrategy": "singlerace"}
PERF_PROPERTIES = {"smt_timeout": int(_os.environ.get("SMTOOL_SMT_TIMEOUT", "600"))}


def _set_perf(conf: dict) -> dict:
    """Set smtool's perf settings on a conf dict AT CREATION (alongside the verify/msg/rule rewrite in
    driver.rewrite_conf): PERF_PROPERTIES (smt_timeout) as top-level keys, and PERF_PROVER_ARGS as
    `-<flag> <value>` prover_args entries (deduped against any inherited args). Doing it here — the one
    place a conf is created from the base — means it's set once per conf and needs no separate
    re-application pass on every write (which re-ran + re-logged for all confs each check/verify)."""
    conf.update(PERF_PROPERTIES)
    args = list(conf.get("prover_args", []))
    # a conformance proof must run FULL formula checking — drop the scene's -skipFormulaChecking if inherited
    args = [a for a in args if a and a.lstrip("-").split()[0] != "skipFormulaChecking"]
    have = {a.lstrip("-").split()[0] for a in args if a}
    for k, v in PERF_PROVER_ARGS.items():
        if k not in have:
            args.append(f"-{k} {v}")
    conf["prover_args"] = args
    return conf


@dataclass
class Project:
    inp: ToolInput
    cls: ModelLayout
    model_spec: S.CVLFile
    conformance: dict[str, S.CVLFile]      # method name -> conformance spec
    confs: dict[str, dict]                 # method name -> conf dict
    reachable: S.CVLFile | None = None     # shared reachable spec (assumeReachable + CUT invariants)
    setup_spec_import: str | None = None   # the setup spec the conformance/proof specs import
    scene_mutability: object = None        # optional resolve(name)->stateMutability from the compiled
                                           # scene (scene.mutability_resolver), for the add_nondet
                                           # cross-check; None => the check is caller-trusted (see below)
    verified_invariants: set = field(default_factory=set)  # reachable invariants the prover DISCHARGED
                                           # (populated by verify.prune_reachable). An assumed invariant
                                           # not in here is an unproven assumption — check_consistency flags it.

    # -------------------------------------------------- construction
    @classmethod
    def from_input(cls, inp: ToolInput, setup_conf: dict | None = None,
                   setup_spec_import: str | None = None, declared=None) -> "Project":
        """Build a Project from ONE ToolInput (all methods share that input's function list). The
        multi-method production path is `from_method_specs`; this single-input path is used offline
        (e.g. the recons build one method at a time)."""
        c = classify(inp.functions)
        model = driver.build_model_spec(inp, c)
        reachable = driver.build_reachable_spec(inp, c)
        conf_specs, confs = {}, {}
        for m in c.model:
            conf_specs[m.name] = driver.build_conformance_spec(
                inp, c, m, setup_spec_import, declared, reachable_spec_import=inp.reachable_spec)
            if setup_conf is not None:
                confs[m.name] = driver.rewrite_conf(setup_conf, inp, m)
        return cls(inp=inp, cls=c, model_spec=model, conformance=conf_specs, confs=confs,
                   reachable=reachable, setup_spec_import=setup_spec_import)

    @classmethod
    def from_method_specs(cls, method_specs: list[ToolInput],
                          setup_spec_import: str | None = None, declared=None,
                          precise_reverts: bool = False, loop_iter: int = 3) -> "Project":
        """Build ONE Project with a SINGLE shared model + per-method conformance. The model is the
        union of the per-method observables (deduped by getter name; the observable occurrence wins,
        so it defines the ghost) plus one <f>CVL stub per method. Each method's conformance is built
        from ITS OWN spec (per-method getter roles), importing the one shared model. No merge.
        `precise_reverts` (the single smtool switch) sets EXACT revert conformance everywhere; default
        False = the sound OVER-APPROXIMATION."""
        for spec in method_specs:
            spec.precise_reverts = precise_reverts
            spec.loop_iter = loop_iter
        base = method_specs[0]
        methods: dict[str, object] = {}
        getters: dict[str, object] = {}
        for spec in method_specs:
            for f in spec.functions:
                if f.is_state_changing:
                    methods.setdefault(f.name, f)
                else:
                    # key by (name, bound component): two components of ONE multi-return getter
                    # (e.g. `getPair() -> (a, b)`) get distinct ghosts; the same observable declared
                    # across methods still merges to one.
                    key = (f.name, f.bind_component)
                    cur = getters.get(key)
                    if cur is None or (f.observable and not cur.observable):
                        getters[key] = f
        union = list(methods.values()) + list(getters.values())
        model_input = ToolInput(
            cut=base.cut, functions=union, alias=base.alias, model_spec_name=base.model_spec_name,
            conformance_prefix_name=base.conformance_prefix_name, specs_dir=base.specs_dir,
            precise_reverts=precise_reverts, loop_iter=loop_iter)
        model_cls = classify(union)
        model = driver.build_model_spec(model_input, model_cls)
        reachable = driver.build_reachable_spec(model_input, model_cls)   # ONE shared reachable spec
        # the reachable key SLOTS are a property of the WHOLE model (their declaration lives in the shared
        # reachable spec) — thread them so every per-method conformance rule calls assumeReachable with
        # the declared slots, even for methods whose own layout would pick different keys.
        reachable_keys = driver._reachable_keys(model_cls)
        conformance: dict[str, S.CVLFile] = {}
        for spec in method_specs:
            c = classify(spec.functions)          # per-method classification (per-method getter roles)
            m = c.model[0]
            conformance[m.name] = driver.build_conformance_spec(
                spec, c, m, setup_spec_import, declared, reachable_spec_import=model_input.reachable_spec,
                reachable_keys=reachable_keys)
        return cls(inp=model_input, cls=model_cls, model_spec=model, conformance=conformance,
                   confs={}, reachable=reachable, setup_spec_import=setup_spec_import)

    # -------------------------------------------------- lookups
    def model_function_names(self) -> set[str]:
        """Names of all functions in the model spec (readers, mirrors/helpers, and <f>CVL bodies)."""
        return {b.name for b in self.model_spec.blocks if isinstance(b, S.FunctionDef)}

    def function_mutability(self, name: str) -> str | None:
        """Mutability of a function: from the modeled inputs first, else the compiled scene
        (`scene_mutability`, if wired), else None (unknown). Used by add_nondet to refuse NONDET of a
        state-changing function."""
        for f in self.inp.functions:
            if f.name == name:
                return f.mutability
        return self.scene_mutability(name) if self.scene_mutability else None

    def reader_names(self) -> set[str]:
        """The model reader names (the glue's model-side accessor functions), one per binding."""
        return {b.reader_name for b in self.cls.bindings}

    def find_func(self, spec: S.CVLFile, name: str) -> S.FunctionDef | None:
        """The FunctionDef named `name` in the given spec (model / conformance / reachable), or None."""
        for b in spec.blocks:
            if isinstance(b, S.FunctionDef) and b.name == name:
                return b
        return None

    def find_glue(self, method: str) -> S.FunctionDef | None:
        """The correspondence function — identified STRUCTURALLY as the sole FunctionDef in a
        conformance spec (model fns live in the model spec; assumeReachable in the reachable spec),
        not by a name prefix."""
        fns = [b for b in self.conformance[method].blocks if isinstance(b, S.FunctionDef)]
        return fns[0] if fns else None

    def reachable_invariant_names(self) -> list[str]:
        """Names of the invariants DECLARED in the reachable spec (the candidate set to prove)."""
        return driver.invariant_names(self.reachable) if self.reachable else []

    def assumed_invariant_names(self) -> list[str]:
        """Invariants ASSUMED (requireInvariant in assumeReachable) — each must be prover-DISCHARGED
        (in verified_invariants) before the conformance results may be trusted."""
        fn = self.find_func(self.reachable, driver.ASSUME) if self.reachable else None
        return [c.invariant_name for c in fn.block.commands
                if isinstance(c, S.AssumeInvariantCmd)] if fn else []

    def drop_invariants(self, names) -> None:
        """Best-effort prune: remove each named invariant (its declaration in the reachable spec AND
        its `requireInvariant` in assumeReachable) — used for the ones that did NOT verify. Re-write
        the project afterwards to regenerate the reachable/proof/conformance artifacts without them.
        See smtool.verify.prune_reachable."""
        names = set(names)
        if self.reachable is None or not names:
            return
        self.reachable.blocks = [b for b in self.reachable.blocks
                                 if not (isinstance(b, S.Invariant) and b.name in names)]
        fn = self.find_func(self.reachable, driver.ASSUME)
        if fn is not None:
            fn.block.commands = [c for c in fn.block.commands
                                 if not (isinstance(c, S.AssumeInvariantCmd) and c.invariant_name in names)]

    def find_rule(self, method: str, rule_name: str) -> S.RuleBlock | None:
        """The rule named `rule_name` in `method`'s conformance spec, or None."""
        for b in self.conformance[method].blocks:
            if isinstance(b, S.RuleBlock) and b.rule_name == rule_name:
                return b
        return None

    def find_ghost(self, name: str) -> S.GhostDef | None:
        """The ghost named `name` in the model spec, or None."""
        for b in self.model_spec.blocks:
            if isinstance(b, S.GhostDef) and b.ghost_name == name:
                return b
        return None

    # -------------------------------------------------- rendering
    def render_model(self) -> str:
        """The model spec as CVL text."""
        return pretty_print(self.model_spec)

    def render_conformance(self, method: str) -> str:
        """A method's conformance spec as CVL text."""
        return pretty_print(self.conformance[method])

    def render_summary(self) -> str:
        """The CONSUMER summary-application spec: imports the model and summarizes each real CUT function
        with its model counterpart, so a downstream proof runs against the (conformance-verified) symbolic
        model instead of the heavy real CUT. Apply only AFTER conformance passes."""
        return pretty_print(driver.build_summary_spec(self.inp, self.cls))

    def snapshot(self) -> "Project":
        """A deep copy — a mutation applies to the snapshot, then _commit accepts or discards it."""
        return copy.deepcopy(self)

    def build_conf(self, method: str, setup_conf: dict) -> dict:
        """Final .conf for a method, built AFTER mutations so its `rule` list is complete —
        it reads the post-mutation conformance spec (which includes discharge invariants added
        by add_requireInvariant). Do NOT derive the rule list from the skeleton."""
        m = next(f for f in self.cls.model if f.name == method)
        # The shared reachable invariants are NOT proven here — they're proven ONCE against the
        # (unchanging) CUT by the dedicated reachable conf, and each conformance run only ASSUMES
        # them via requireInvariant. (Listing an imported invariant in this run's `rule` filter is
        # also a CVL error unless `use invariant`'d.) See TODO(prove-once) in driver / add_requireInvariant.
        return _set_perf(driver.rewrite_conf(setup_conf, self.inp, m, conformance_spec=self.conformance[method]))

    # -------------------------------------------------- output + consistency
    def write(self, out_dir: str, setup_conf: dict) -> list[str]:
        """Write the ONE shared model + each method's conformance spec and conf. The confs are
        built post-mutation (complete `rule` list). Returns the paths written."""
        out = Path(out_dir)
        (out / "specs").mkdir(parents=True, exist_ok=True)
        (out / "conf").mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        mp = out / "specs" / self.inp.model_spec
        mp.write_text(self.render_model())
        written.append(str(mp))
        # the consumer summary-application spec (imports the model, summarizes each CUT fn -> model);
        # a downstream proof imports THIS to run against the trusted model instead of the real CUT.
        smp = out / "specs" / self.inp.summary_spec
        smp.write_text(self.render_summary())
        written.append(str(smp))
        inv_names = self.reachable_invariant_names()
        if self.reachable is not None:
            rp = out / "specs" / self.inp.reachable_spec
            rp.write_text(pretty_print(self.reachable))
            written.append(str(rp))
        if inv_names:   # prove-incrementally: one proof spec + conf that discharges the shared invariants
            psp = out / "specs" / f"{self.inp.cut}ReachableProof.spec"
            psp.write_text(pretty_print(
                driver.build_reachable_proof_spec(self.inp, self.setup_spec_import, inv_names)))
            pcf = out / "conf" / f"{self.inp.cut}Reachable.conf"
            pcf.write_text(json.dumps(_set_perf(driver.rewrite_reachable_conf(setup_conf, self.inp, inv_names)), indent=4))
            written += [str(psp), str(pcf)]
        for method in self.conformance:
            base = f"{self.inp.conformance_prefix}{driver._cap(method)}Conformance"
            sp = out / "specs" / f"{base}.spec"
            sp.write_text(self.render_conformance(method))
            cp = out / "conf" / f"{base}.conf"
            cp.write_text(json.dumps(self.build_conf(method, setup_conf), indent=4))
            written += [str(sp), str(cp)]
        return written

    def check_consistency(self) -> list[str]:
        """Are the final specs consistent with the final shared model? (structural/typecheck, NOT
        the SMT proof — that's `verify`). Returns a list of problems ([] == consistent):
          - the shared model type-checks standalone;
          - each method's <f>CVL exists in the model (the conformance references it);
          - the discipline linter passes (model purity, glue shape, no dangling requireInvariant)."""
        from .linter import lint
        from .typecheck import typecheck_spec
        problems: list[str] = []
        ok, out = typecheck_spec(self.render_model())
        if not ok:
            tail = out.strip().splitlines()[-1] if out.strip() else "(no output)"
            problems.append(f"model spec does not typecheck: {tail}")
        for method in self.conformance:
            if self.find_func(self.model_spec, driver.model_fn_name(method)) is None:
                problems.append(f"conformance[{method}] references {method}CVL, missing from the model")
        # H2 gate: every assumed reachable invariant must be prover-DISCHARGED (verify.prune_reachable
        # records the survivors). An unproven assumption makes the conformance results conditional.
        for inv in self.assumed_invariant_names():
            if inv not in self.verified_invariants:
                problems.append(f"invariant {inv} is ASSUMED (requireInvariant in assumeReachable) but "
                                f"not proven — run the reachable conf via verify.prune_reachable before "
                                f"trusting these conformance results")
        problems += lint(self)
        return problems


@dataclass
class Result:
    ok: bool
    message: str
    violations: list[str] = field(default_factory=list)
