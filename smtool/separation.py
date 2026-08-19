"""Separation prover — the soundness gate for a PARTIAL model.

smtool models a SUBSET of a contract C: pi = the Router-reached functions+storage we replace with a
symbolic model (ghost storage), rho = the Router-reached functions+storage we KEEP REAL. Unreached
functions are out of scope (Router never calls them, so they never execute in the proof). So
pi ∪ rho ⊆ C, and rho is reachability-derived (from the surviving-call-graph): the C functions
reachable from Router, minus pi.

smtool's own conformance proves the modeled functions match real on the pi observables. That is NOT
enough for a partial model — installing it means, in the consumer proof, pi-functions run as the model
over GHOST pi-storage (real pi-storage is never written) while rho-functions run REAL. The hybrid
diverges from the all-real execution in exactly three ways, so there are three obligations:

  (W1) a rho-function WRITES pi-storage   -> the ghost goes stale (model reads ghost; reality saw the write)
  (W2) a pi-function  WRITES rho-storage  -> the model drops a write to real storage rho still observes
  (R)  a rho-function READS  pi-storage   -> rho reads the initial real pi-storage; reality reads the live value
       (only via a RAW read that bypasses the modeled getter; a read THROUGH the getter hits the ghost,
        so it is already consistent)

KEY SIMPLIFICATION (EVM encapsulation): a contract's storage is private — external code touches it only
through C's functions. So separation is a WITHIN-CONTRACT property: we range over C's own functions, not
the whole scene.

This module implements the GETTER-BASED checks (robust to assembly/opaque storage — they reason over
getter VALUES, never raw slots, which is exactly why the conformance already worked for Solady tokens):

  * W1/W2 — build_frame_rule: run the REAL function, assert every getter on the OTHER side is unchanged
    on real success. Load-bearing and cheap (one call each).

TODO / follow-ups (documented, not built here):
  * R (read separation) — getter-based NON-INTERFERENCE: run f from two states that AGREE on the rho
    getters but leave the pi getters free, assert f's return + rho-effect are equal (catches even a raw
    read, without hooking it). The CVL 2-copy mechanic is the fiddly part; and for DISJOINT-CONCERN
    contracts (registry vs balances, as in our tokens) rho never reads pi-storage, so R is trivially
    satisfied. Track it as the second getter-based pass.
  * HOOK OPTIMIZATION — a faster uniform check when the storage is CVL-referenceable: `hook Sload/Sstore`
    on pi-storage set a ghost flag, run each rho-function, assert the flag stayed false (catches reads
    AND writes in one run). Gated by a CANARY (run a known accessor — the getter itself for a read, a
    witnessed getter value-change for a write — and assert the hook FIRED; if not, hooks are dead on this
    (assembly) storage and we use the getter path). Not built: getter path suffices for most cases.
"""
from dataclasses import dataclass

import composer.cvl.schema as S

from . import cvlx as x
from .ir import Signature


@dataclass
class Observable:
    """A getter whose backing storage a frame rule shows is untouched. `getter` is the scene Signature
    (an envfree view). A frame rule declares one fresh var per getter argument, framing over ALL keys."""
    contract: str
    getter: Signature

    @property
    def arg_types(self) -> list:
        return [p.type for p in self.getter.params]

    @property
    def numeric(self) -> bool:
        rets = list(self.getter.returns)
        if len(rets) != 1:
            return False
        t = rets[0]
        return t.startswith("uint") or t.startswith("int") or t == "mathint"


def _read(obs: Observable, arg_names: list):
    call = x.call(obs.getter.name, [x.ident(a) for a in arg_names], host=obs.contract)
    return x.call("to_mathint", [call]) if obs.numeric else call


def _compare_type(obs: Observable) -> str:
    return "mathint" if obs.numeric else list(obs.getter.returns)[0]


def build_frame_rule(fn: Signature, fn_contract: str, preserved: list, *,
                     alias: str | None = None) -> S.RuleBlock:
    """`separation_<fn>`: run the REAL `fn` and assert every observable in `preserved` is unchanged —
    i.e. `fn` does not WRITE the storage those observables read. Each observable is framed with fresh
    free vars over its keys. A PLAIN call (no `@withrevert`) prunes reverting paths — an implicit
    success assumption — so the frame is asserted only where `fn` succeeds; on a revert all storage
    rolls back, so the frame holds trivially and needs no check.

      direction W1: `fn` in rho, `preserved` = the pi observables.
      direction W2: `fn` in pi,  `preserved` = the rho observables.
    """
    host = alias or fn_contract
    rule_params = [(p.type, p.name) for p in fn.params]
    frame_names: list = []
    for oi, obs in enumerate(preserved):
        names = [f"o{oi}_k{ai}" for ai in range(len(obs.arg_types))]
        frame_names.append(names)
        rule_params += list(zip(obs.arg_types, names))

    cmds: list = []
    pure = fn.mutability == "pure"
    if not pure:
        cmds.append(x.declare("env", "e"))
    # snapshot each preserved observable PRE
    pre = []
    for oi, obs in enumerate(preserved):
        pn = f"pre_o{oi}"
        pre.append(pn)
        cmds.append(x.declare(_compare_type(obs), pn, _read(obs, frame_names[oi])))
    # call the REAL fn PLAIN — a revert prunes the path (implicit success assumption)
    lead = [] if pure else [x.ident("e")]
    cmds.append(x.apply(x.call(fn.name, lead + [x.ident(p.name) for p in fn.params], host=host)))
    # assert each preserved observable UNCHANGED
    for oi, obs in enumerate(preserved):
        cmds.append(x.assert_(
            x.binop("eq", x.ident(pre[oi]), _read(obs, frame_names[oi])),
            f"separation: {fn.name} must not write {obs.contract}.{obs.getter.name} storage"))
    return x.rule(f"separation_{fn.name}", rule_params, cmds)


def build_separation_rules(pi_fns: list, rho_fns: list, pi_obs: list, rho_obs: list,
                           contract: str, *, alias: str | None = None) -> list:
    """The two-direction write separation for a partial model of `contract`:
      W1: every rho-function preserves the pi observables,
      W2: every pi-function  preserves the rho observables.
    `pi_fns`/`rho_fns` are Signatures; `pi_obs`/`rho_obs` are Observables. rho is reachability-derived
    (Router-reached, not modeled); unreached functions are omitted (never execute in the proof)."""
    rules = [build_frame_rule(f, contract, pi_obs, alias=alias) for f in rho_fns]   # W1
    rules += [build_frame_rule(f, contract, rho_obs, alias=alias) for f in pi_fns]  # W2
    return rules


def build_separation_spec(setup_import: str | None, rules: list) -> S.CVLFile:
    """Wrap separation rules in a runnable spec. Imports the conformance-capable setup so the effective
    scene (links, existing summaries) matches the real run; tokens stay REAL — NO model installed here."""
    imports = [setup_import] if setup_import else ()
    return x.spec_file(imports=imports, blocks=list(rules))
