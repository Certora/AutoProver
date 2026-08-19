"""Orchestrator for a PARTIAL model: given a consumer `.conf` + pi + rho, produce a conformance-proven,
separation-checked, installed symbolic model of a dependency contract — WITHOUT changing the consumer's
verify target.

The consumer (e.g. a router) STAYS the verify target in every derived run. The modeled contract C
(e.g. a position/token manager) is a dependency reached via its `using` alias (e.g. `dep`). This is the safe
composition: the conformance + separation are proven in exactly the scene the model is installed into
(same parametric context, same dispatch/summaries), so they directly license the install. It relies on
the driver's alias support (`ToolInput.alias`, threaded through the call sites).

DETERMINISTIC pipeline (code, not a meta-agent). Reuses:
  * driver.py        — model spec (fCVL stubs) + conformance rules (alias-aware) + install summary.
  * the fill agent   — fills the fCVL bodies (the ONE AI step; run_smtool's graphcore loop). Templated
                       token semantics are a future shortcut; for now the agent proposes bodies and the
                       conformance disposes them.
  * separation.py    — the partial-model soundness gate (pi/rho storage-write-disjointness, getter-frame).
  * ProverRunner     — the cloud runs; each derived .conf reuses the consumer conf (files/links/solc/
                       packages), only swapping `verify` to the generated spec (tokens stay REAL for the
                       conformance/separation runs; the model is installed only in the consumer run).

FLOW (each step gates the next):
  1. layout   — build ToolInput(cut=C, alias) + ModelLayout from pi (functions + observables).
  2. model    — driver.build_model_spec (stubs) -> fill fCVL bodies (agent) -> Symbolic<C>Model.spec.
  3. conform  — driver conformance rules (alias-aware) over the consumer scene; RUN; require all VERIFIED
                (with the token SOLVENCY invariant when C is a token — removes only unreachable states).
  4. separate — separation.build_separation_rules(pi_fns, rho_fns, pi_obs, rho_obs); RUN; require VERIFIED.
  5. install  — driver.build_summary_spec -> add `import "Symbolic<C>Summary.spec";` to the consumer spec.
                Installed ONLY after 3 AND 4 pass. Optionally re-run the consumer conf for the perf delta.

This module provides the deterministic assembly (`emit`) that turns (conf, pi, rho) into the runnable
artifacts + the confs; the run/gate/install driving loop reuses smtool's ProverRunner + verify.py.
"""
from dataclasses import dataclass, field

from .ir import ToolInput, FunctionSpec, Signature
from .separation import Observable


@dataclass
class PartialModelSpec:
    """The whole input: a consumer `.conf` (stays the verify target) + the pi/rho projection of a
    dependency contract C. rho is REACHABILITY-DERIVED (Router-reached C functions minus pi — from the
    surviving-call-graph); unreached C functions are omitted (never execute in the proof)."""
    consumer_conf: str                 # path to the consumer run .conf
    setup_spec: str                    # the consumer's setup spec (imported so the effective scene matches)
    modeled_contract: str              # C — the dependency contract to model
    alias: str                         # C's `using` alias in the consumer scene (e.g. "dep")

    pi_functions: list = field(default_factory=list)     # FunctionSpec — modeled (state-changers + views)
    pi_observables: list = field(default_factory=list)   # Observable — pi getters (balanceOf, ...)
    rho_functions: list = field(default_factory=list)    # Signature — retained (reached, not modeled)
    rho_observables: list = field(default_factory=list)  # Observable — rho getters (moduleById, ...)

    solvency_invariant: str | None = None  # e.g. "requireSolventCollateral" — assumed in the conformance
                                           # for a token C (removes unreachable overflow states; sound)

    def tool_input(self) -> ToolInput:
        """The smtool ToolInput: CUT is the MODELED contract C (names methods{} + envfree decls), but the
        call-time host/self use `alias` so the consumer stays the verify target."""
        return ToolInput(cut=self.modeled_contract, functions=self.pi_functions, alias=self.alias)


def separation_spec(spec: PartialModelSpec):
    """The separation soundness gate for `spec`: pi/rho storage-write-disjointness (W1: rho-fns preserve
    pi observables; W2: pi-fns preserve rho observables), over the consumer scene (tokens real, no model).
    Returns a CVLFile. Read non-interference (R) + the hook fast-path are separation.py follow-ups."""
    from .separation import build_separation_rules, build_separation_spec
    pi_sigs = [f.signature for f in spec.pi_functions]
    rules = build_separation_rules(pi_sigs, spec.rho_functions, spec.pi_observables,
                                   spec.rho_observables, spec.modeled_contract, alias=spec.alias)
    return build_separation_spec(spec.setup_spec, rules)


def derived_conf(spec: PartialModelSpec, verify_spec: str, *, install: bool = False) -> dict:
    """A run .conf derived from the consumer conf: reuse everything (files/links/solc/packages/flags),
    swap `verify` to `<consumer_cut>:<verify_spec>` — the consumer STAYS the verify target. `install`
    is a marker for the consumer run that imports the model summary (vs the conformance/separation runs
    that keep the tokens real). Conformance needs full formula checking (drop -skipFormulaChecking)."""
    import json
    conf = json.loads(open(spec.consumer_conf).read())
    conf["verify"] = f"{_consumer_cut(conf)}:{verify_spec}"
    conf["prover_args"] = [a for a in conf.get("prover_args", [])
                           if a and a.lstrip("-").split()[0] != "skipFormulaChecking"]
    conf.pop("rule", None)
    return conf


def _consumer_cut(conf: dict) -> str:
    """The consumer's verify target (e.g. `Router`) — kept as the CUT in every derived run."""
    return conf["verify"].split(":", 1)[0]
