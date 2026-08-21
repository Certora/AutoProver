"""OverApproxProject: the mutable state the over-approximation agent's ONE mutation (`set_phi`) drives.

This is the over-approx counterpart of `project.Project`, but far simpler: the whole model is a single
authored hole — the predicate `Phi(params, res)` per target function. Everything else (the summary, the
conformance rule) is DETERMINISTIC given Phi (see `overapprox.py`). So the agent's only lever is Phi, and
the loop's job is to make Phi as STRONG as the conformance proof allows (weaken on a counterexample).

Holds one `OverApproxTarget` per target function (keyed by name). `set_phi` parses CVL surface text into
the target's `phi_body` on a snapshot, validates structurally, and commits — mirroring the
snapshot→validate→commit discipline of the model mutations. `write` emits, per target, the three
artifacts (`<fn>Phi.spec`, `<fn>Summary.spec`, `<fn>Conformance.spec`) + the conformance `.conf`; the
runner then proves `overApprox_<fn>`. Reuses `overapprox.py` builders, `cvl_parse`, `project._set_perf`
(same perf conf settings as the model loop), and `pretty_print` — it changes no existing smtool file.
"""
import copy
import json
from dataclasses import dataclass, field
from pathlib import Path

from composer.cvl.pretty_print import pretty_print

from . import overapprox as oa
from .overapprox import OverApproxTarget
from .cvl_parse import parse_commands, CVLParseError
from .project import Result, _set_perf


def conformance_rule_name(fn: str) -> str:
    """The value-conformance rule the runner proves for target `fn` (matches build_conformance_rule)."""
    return "overApprox_" + fn


def conformance_rule_names(t: OverApproxTarget) -> list[str]:
    """All conformance rules to run for target `t`: the value rule `overApprox_<fn>` (single-return) plus,
    when Ψ is set, the revert rule `revertConform_<fn>` — the two the emitted conformance spec carries."""
    names: list[str] = []
    if oa.build_conformance_rule(t) is not None:
        names.append(conformance_rule_name(t.sig.name))
    if oa.build_revert_rule(t) is not None:
        names.append(oa.revert_rule_name(t.sig.name))
    return names


@dataclass
class OverApproxProject:
    """Targets keyed by function name; the shared CUT + scene setup import + specs dir. Each target's
    `phi_body` is the hole `set_phi` fills. `verified` records the targets whose conformance rule the
    prover discharged (so the summary may be installed)."""
    cut: str
    targets: dict[str, OverApproxTarget]
    setup_spec_import: str | None = None
    specs_dir: str = "certora/specs"
    verified: set = field(default_factory=set)
    last_verified: dict = field(default_factory=dict)   # fn -> the phi_body that last PROVED (the sound
                                                         # fallback: on budget-exhaustion we ship this,
                                                         # never a tighter-but-failing Phi)

    # -------------------------------------------------- construction
    @classmethod
    def of(cls, cut: str, targets: list[OverApproxTarget], setup_spec_import: str | None = None,
           specs_dir: str = "certora/specs") -> "OverApproxProject":
        """Build from a list of targets. Stamps each target's `cut`/`setup_spec_import` from the project
        so the emitted conformance spec imports the scene setup and calls the right CUT."""
        by_name: dict[str, OverApproxTarget] = {}
        for t in targets:
            t.cut = cut
            if t.setup_spec_import is None:
                t.setup_spec_import = setup_spec_import
            by_name[t.sig.name] = t
        return cls(cut=cut, targets=by_name, setup_spec_import=setup_spec_import, specs_dir=specs_dir)

    # -------------------------------------------------- the ONE mutation
    def set_phi(self, fn: str, cvl_text: str) -> Result:
        """Fill / replace target `fn`'s predicate body from CVL surface text. The body is a sequence of
        CVL statements ending in `return <bool>` (it may declare locals and `require` — the sqrt/tag
        idioms). Parsed with the Phi param scope ([params..., res]); a parse error is a REJECTED result
        the agent fixes. Applies on a snapshot and commits only if it re-renders (structurally valid)."""
        if fn not in self.targets:
            return Result(False, f"unknown target '{fn}' (targets: {sorted(self.targets)})")
        t = self.targets[fn]
        params = oa._phi_params(t)                      # [(type, name)...] including the trailing result
        try:
            cmds = parse_commands(cvl_text, params)
        except CVLParseError as e:
            return Result(False, f"CVL parse error in Phi body: {e}")
        snap = copy.deepcopy(t)
        snap.phi_body = cmds
        try:
            pretty_print(oa.build_phi_spec(snap))       # structural: the Phi spec must render
        except Exception as e:                          # pragma: no cover - defensive
            return Result(False, f"Phi does not render: {type(e).__name__}: {e}")
        bad = oa.lint_phi(snap)                         # SOUNDNESS guardrail: no domain-restricting require
        if bad:
            return Result(False, "Phi has a domain-restricting require (would be unsound)", violations=bad)
        self.targets[fn] = snap
        self.verified.discard(fn)                       # Phi changed → any prior verdict is stale
        return Result(True, f"set Phi for '{fn}' ({len(cmds)} statement(s))")

    def set_psi(self, fn: str, cvl_text: str) -> Result:
        """Fill / replace target `fn`'s REVERT predicate Ψ(params) from CVL surface text — a boolean
        formula over the params, true where the summary must revert (e.g. `return c == 0;`). Installs
        `if (Ψ(params)) revert();` at the top of the summary and adds the `revertConform_<fn>` rule
        (`Ψ => realReverted`). Ψ over the params only (no result, no witness locals), so a `require` is
        never legitimate here — the lint rejects it. Snapshot→validate→commit, like set_phi."""
        if fn not in self.targets:
            return Result(False, f"unknown target '{fn}' (targets: {sorted(self.targets)})")
        t = self.targets[fn]
        try:
            cmds = parse_commands(cvl_text, oa._psi_params(t))
        except CVLParseError as e:
            return Result(False, f"CVL parse error in Psi body: {e}")
        snap = copy.deepcopy(t)
        snap.psi_body = cmds
        try:
            pretty_print(oa.build_psi_spec(snap))       # structural: the Ψ spec must render
        except Exception as e:                          # pragma: no cover - defensive
            return Result(False, f"Psi does not render: {type(e).__name__}: {e}")
        bad = oa.lint_psi(snap)                          # SOUNDNESS guardrail: no require in Ψ
        if bad:
            return Result(False, "Psi must be a pure boolean revert predicate (no require)", violations=bad)
        self.targets[fn] = snap
        self.verified.discard(fn)                        # Ψ changed → any prior verdict is stale
        return Result(True, f"set Psi (revert predicate) for '{fn}' ({len(cmds)} statement(s))")

    # -------------------------------------------------- verified-fallback bookkeeping
    def mark_verified(self, fn: str) -> None:
        """Record that `fn`'s current Phi PROVED — add it to `verified` and snapshot the proven body as
        the fallback (so a later tighter-but-failing Phi never overwrites what we can ship)."""
        self.verified.add(fn)
        self.last_verified[fn] = copy.deepcopy(self.targets[fn].phi_body)

    def restore_best_verified(self) -> list[str]:
        """Reset each target whose current Phi is NOT its last-proven one back to that proven Phi. Called
        at loop end so the emitted summary is always a conformance-VERIFIED Phi, even if the agent left a
        tighter-but-failing one when the budget ran out. Returns the fns restored."""
        restored = []
        for fn, body in self.last_verified.items():
            if fn in self.targets and self.targets[fn].phi_body is not body:
                self.targets[fn].phi_body = copy.deepcopy(body)
                restored.append(fn)
        return restored

    # -------------------------------------------------- rendering
    def render_phi(self, fn: str) -> str:
        return pretty_print(oa.build_phi_spec(self.targets[fn]))

    def render_psi(self, fn: str) -> str:
        return pretty_print(oa.build_psi_spec(self.targets[fn]))

    def render_summary(self, fn: str) -> str:
        # the sound-by-construction HAVOC summary (require Phi; havoc'd result). The DETERMINISTIC-ghost
        # (memo) form — for consumer proofs that need f to behave as a function — is built separately by
        # smtool.detsummary from the same conformance-proven Phi.
        return pretty_print(oa.build_summary_spec(self.targets[fn]))

    def render_conformance(self, fn: str) -> str:
        return pretty_print(oa.build_conformance_spec(self.targets[fn]))

    # -------------------------------------------------- conf + output
    def provable_targets(self) -> list[str]:
        """Targets that yield at least one conformance rule: the value rule (single-return `f_sol`) or,
        when Ψ is set, the revert rule (a void `f_sol` has no value rule but can still have a Ψ)."""
        return [fn for fn, t in self.targets.items() if conformance_rule_names(t)]

    def _conf(self, fn: str, setup_conf: dict) -> dict:
        """The conformance .conf for `fn`: the setup conf (scene inherited untouched) with verify -> the
        conformance spec, rule = the value rule `overApprox_<fn>` plus `revertConform_<fn>` when Ψ is set
        (the setup's imported `sanity` rule stays defined-but-not-run), multi_assert_check on, and
        smtool's perf settings."""
        conf = copy.deepcopy(setup_conf)
        conf["verify"] = f"{self.cut}:{self.specs_dir}/{fn}Conformance.spec"
        conf["msg"] = f"overapprox {fn} conformance"
        conf["multi_assert_check"] = True
        conf["rule"] = conformance_rule_names(self.targets[fn])
        return _set_perf(conf)

    def conf_paths(self, out_dir: str) -> list[str]:
        """The conformance .conf path emitted per provable target (what the runner verifies)."""
        return [f"{out_dir}/conf/{fn}Conformance.conf" for fn in self.provable_targets()]

    def write(self, out_dir: str, setup_conf: dict) -> list[str]:
        """Write, per target: `<fn>Phi.spec`, `<fn>Summary.spec` (the installable deliverable), and — for
        a provable (single-return) target — `<fn>Conformance.spec` + `.conf`. Returns the paths written.
        Specs go in the subdir named by `specs_dir`'s last component (so it agrees with the conf's
        `verify` path, which is `specs_dir`-relative — e.g. `certora/spec` vs `certora/specs`)."""
        out = Path(out_dir)
        spec_dir = out / Path(self.specs_dir).name
        spec_dir.mkdir(parents=True, exist_ok=True)
        (out / "conf").mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for fn, t in self.targets.items():
            pp = spec_dir / t.phi_import
            pp.write_text(self.render_phi(fn))
            sp = spec_dir / f"{fn}Summary.spec"
            sp.write_text(self.render_summary(fn))
            written += [str(pp), str(sp)]
            if t.psi_body is not None:                      # the revert predicate Ψ (dual of Phi)
                rp = spec_dir / t.psi_import
                rp.write_text(self.render_psi(fn))
                written.append(str(rp))
            if not conformance_rule_names(t):               # void f_sol with no Ψ: no conformance to run
                continue
            cs = spec_dir / f"{fn}Conformance.spec"
            cs.write_text(self.render_conformance(fn))
            cc = out / "conf" / f"{fn}Conformance.conf"
            cc.write_text(json.dumps(self._conf(fn, setup_conf), indent=4))
            written += [str(cs), str(cc)]
        return written

    # -------------------------------------------------- consistency (no prover)
    def check_consistency(self) -> list[str]:
        """Structural/typecheck coherence (NOT the SMT proof — that's the loop's verify). Per target:
        the Phi spec type-checks standalone, Phi returns bool, and the conformance spec references it.
        Returns a list of problems ([] == consistent)."""
        from .typecheck import typecheck_spec
        problems: list[str] = []
        for fn, t in self.targets.items():
            if t.phi_body is None:
                problems.append(f"[{fn}] Phi is unfilled (still the `return true` stub) — call set_phi")
            if list(t.sig.returns) and len(t.sig.returns) != 1:
                problems.append(f"[{fn}] f_sol has {len(t.sig.returns)} returns; v1 over-approx is "
                                f"single-return only (multi-return Phi is a later generalization)")
            ok, tail = typecheck_spec(self.render_phi(fn))
            if not ok:
                last = tail.strip().splitlines()[-1] if tail.strip() else "(no output)"
                problems.append(f"[{fn}] Phi spec does not typecheck: {last}")
            problems += oa.lint_phi(t)                 # SOUNDNESS: no domain-restricting require in Phi
            if t.psi_body is not None:
                ok, tail = typecheck_spec(self.render_psi(fn))
                if not ok:
                    last = tail.strip().splitlines()[-1] if tail.strip() else "(no output)"
                    problems.append(f"[{fn}] Psi spec does not typecheck: {last}")
                problems += oa.lint_psi(t)             # SOUNDNESS: no require in the revert predicate Ψ
        return problems
