"""Over-approximation generator (overapprox.py + overapprox_project.py) + the CVL cast round-trip.

Fast, GENERIC (a fictional CUT `C`), no LLM / prover / compiled scene. Guards: (1) the cvl_parse cast
kind decode (TO/REQUIRE/ASSERT) — the `to_bytes31`/`to_mathint` round-trip that a wrong default silently
corrupted to `require_*`; (2) the three emitted artifacts (Phi / summary / conformance) for the envfree
and env paths; (3) set_phi discipline; (4) write() output + conf shape."""
import json
import tempfile
from pathlib import Path

from composer.cvl.pretty_print import pretty_print

from smtool import cvlx as x
from smtool.cvl_parse import parse_commands
from smtool.ir import Signature, Param as P
from smtool.overapprox import OverApproxTarget, build_conformance_rule, build_revert_rule
from smtool.overapprox_project import OverApproxProject, conformance_rule_name


def _sig(name, params, returns, mutability="view"):
    return Signature(name=name, params=[P(t, n) for t, n in params], returns=list(returns),
                     mutability=mutability)


def _project(*targets, cut="C", setup="Setup.spec"):
    return OverApproxProject.of(cut, list(targets), setup_spec_import=setup)


# ---------------------------------------------------------------- cast round-trip (regression)
def test_cast_kinds_round_trip():
    """to_/require_/assert_ casts must survive parse->render. A wrong default folded TO into require_,
    yielding the non-existent require_bytes31 / require_mathint (broke the byte-extract + mathint idioms)."""
    cmds = parse_commands("bytes31 b = to_bytes31(v); mathint m = to_mathint(v); "
                          "uint256 u = require_uint256(m); uint256 a = assert_uint256(m); return b;",
                          [("uint248", "v")])
    txt = pretty_print(x.spec_file(blocks=[x.func("t", [("uint248", "v")], ["bytes31"], cmds)]))
    assert "to_bytes31(v)" in txt and "to_mathint(v)" in txt
    assert "require_uint256(m)" in txt and "assert_uint256(m)" in txt
    assert "require_bytes31" not in txt and "require_mathint" not in txt   # the corruption is gone


# ---------------------------------------------------------------- the three artifacts (envfree)
def test_envfree_conformance_and_summary_shape():
    pr = _project(OverApproxTarget(cut="C", sig=_sig("sqrt", [("uint256", "x")], ["uint256"])))
    pr.set_phi("sqrt", "mathint r = res; return r * r <= to_mathint(x) && to_mathint(x) < (r + 1) * (r + 1);")

    conf = pr.render_conformance("sqrt")
    assert 'import "Setup.spec";' in conf and 'import "sqrtPhi.spec";' in conf
    assert "function C.sqrt(uint256) external returns (uint256) envfree;" in conf
    assert "rule overApprox_sqrt(uint256 x)" in conf
    assert "sqrt@withrevert(x)" in conf                      # calls the REAL function, no env (envfree)
    assert "! realRev => sqrtPhi(x, retSol)" in conf         # assert real output satisfies Phi

    summ = pr.render_summary("sqrt")
    assert "function sqrtCVL(uint256 x) returns uint256" in summ
    assert 'require(sqrtPhi(x, res)' in summ                 # havoc res + require Phi (over-approx)
    assert "function C.sqrt(uint256 x) external returns (uint256) => sqrtCVL(x);" in summ

    phi = pr.render_phi("sqrt")
    assert "function sqrtPhi(uint256 x, uint256 res) returns bool" in phi
    assert "to_mathint(x)" in phi                            # cast preserved end-to-end


# ---------------------------------------------------------------- the env path (state-changing f)
def test_env_path_threads_env():
    pr = _project(OverApproxTarget(cut="C", sig=_sig("act", [("uint256", "amt")], ["uint256"],
                                                     mutability="nonpayable")))
    pr.set_phi("act", "return res <= amt;")
    conf = pr.render_conformance("act")
    assert "env e;" in conf and "act@withrevert(e, amt)" in conf     # env declared + threaded to the call
    summ = pr.render_summary("act")
    assert "function actCVL(uint256 amt, env e) returns uint256" in summ
    assert "with (env e) => actCVL(amt, e)" in summ                  # binding threads env


# ---------------------------------------------------------------- revert predicate Ψ (fidelity)
def test_no_psi_summary_never_reverts():
    """Default (no Ψ): the summary has no revert guard and there is no revert-conformance rule — the
    sound-but-coarse behavior. The value conformance is the only rule."""
    t = OverApproxTarget(cut="C", sig=_sig("f", [("uint256", "a")], ["uint256"]))
    pr = _project(t)
    pr.set_phi("f", "return res <= a;")
    assert build_revert_rule(pr.targets["f"]) is None
    assert "revert()" not in pr.render_summary("f")
    assert "revertConform_f" not in pr.render_conformance("f")


def test_psi_makes_summary_revert_and_adds_conformance():
    """With Ψ set: the summary reverts on Ψ FIRST (before havoc/require Phi), the conformance spec gains
    the dual rule `revertConform_f` asserting `Psi => realReverted` (the sound direction), and both the
    Phi and Ψ specs are imported."""
    t = OverApproxTarget(cut="C", sig=_sig("divmul", [("uint256", "a"), ("uint256", "c")], ["uint256"]))
    pr = _project(t)
    pr.set_phi("divmul", "return to_mathint(res) == to_mathint(a);")
    assert pr.set_psi("divmul", "return c == 0;").ok

    summ = pr.render_summary("divmul")
    assert "if(divmulReverts(a, c))" in summ and "revert(" in summ            # guard reverts where f does
    assert summ.index("revert(") < summ.index("divmulPhi(a, c, res)")        # revert BEFORE havoc/require Phi
    assert 'import "divmulReverts.spec";' in summ and 'import "divmulPhi.spec";' in summ

    psi = pr.render_psi("divmul")
    assert "function divmulReverts(uint256 a, uint256 c) returns bool" in psi
    assert "c == 0" in psi

    conf = pr.render_conformance("divmul")
    assert "rule overApprox_divmul" in conf                                   # value rule kept
    assert "rule revertConform_divmul" in conf                                # + the dual revert rule
    assert "divmulReverts(a, c) => realRev" in conf                           # Psi => realReverted
    assert 'import "divmulReverts.spec";' in conf


def test_set_psi_rejects_require():
    """Ψ is a pure boolean over params — a `require` (which would prune inputs) is rejected; a plain
    boolean `return` is accepted."""
    pr = _project(OverApproxTarget(cut="C", sig=_sig("f", [("uint256", "a")], ["uint256"])))
    pr.set_phi("f", "return res <= a;")
    assert pr.set_psi("f", "return a == 0;").ok
    r = pr.set_psi("f", "require a > 0; return a == 0;")
    assert not r.ok and any("PURE boolean" in v for v in r.violations)
    assert pr.targets["f"].psi_body is not None                              # rejected set_psi didn't clobber
    assert not pr.set_psi("nope", "return true;").ok                         # unknown target


def test_write_and_conf_include_revert_rule():
    """write() emits the Ψ spec and the conformance .conf runs BOTH rules when Ψ is set."""
    t = OverApproxTarget(cut="C", sig=_sig("f", [("uint256", "a")], ["uint256"]))
    pr = _project(t)
    pr.set_phi("f", "return res <= a;")
    pr.set_psi("f", "return a == 0;")
    with tempfile.TemporaryDirectory() as d:
        written = {Path(p).name for p in pr.write(d, {"files": ["C.sol"], "verify": "C:Setup.spec"})}
        assert "fReverts.spec" in written
        conf = json.loads((Path(d) / "conf" / "fConformance.conf").read_text())
        assert conf["rule"] == ["overApprox_f", "revertConform_f"]


# ---------------------------------------------------------------- set_phi discipline
def test_set_phi_rejects_and_preserves():
    pr = _project(OverApproxTarget(cut="C", sig=_sig("f", [("uint256", "a")], ["uint256"])))
    assert pr.set_phi("f", "return a <= 100;").ok
    good = pr.targets["f"].phi_body
    assert not pr.set_phi("nope", "return true;").ok                 # unknown target
    r = pr.set_phi("f", "return a <<< ;")                            # parse error
    assert not r.ok and "parse error" in r.message
    assert pr.targets["f"].phi_body is good                          # a rejected set_phi does not clobber


def test_set_phi_rejects_domain_restricting_require():
    """SOUNDNESS guardrail: a `require` that constrains only params/result (no fresh witness local) is a
    domain restriction — rejected. A `require` that pins a fresh witness local (byte-extract idiom) is OK."""
    pr = _project(OverApproxTarget(cut="C", sig=_sig("f", [("uint256", "x")], ["uint256"])))
    good = pr.set_phi("f", "return a <= 100;".replace("a", "x"))   # a plain bound in the RETURN is fine
    assert good.ok
    # a domain-restricting require on a param -> REJECTED
    r = pr.set_phi("f", "require x < 100; return x <= 100;")
    assert not r.ok and any("DOMAIN RESTRICTION" in v for v in r.violations)
    assert pr.targets["f"].phi_body is not None                    # rejected set_phi didn't clobber
    # a require restricting the result -> REJECTED
    r2 = pr.set_phi("f", "require res != 0; return res <= x;")
    assert not r2.ok and any("DOMAIN RESTRICTION" in v for v in r2.violations)
    # a WITNESS pin (require references a fresh local) -> ACCEPTED, even with an inequality bracket
    r3 = pr.set_phi("f", "uint256 k; require k * k <= x && (k + 1) * (k + 1) > x; return res <= x;")
    assert r3.ok


def test_set_phi_invalidates_stale_verdict():
    pr = _project(OverApproxTarget(cut="C", sig=_sig("f", [("uint256", "a")], ["uint256"])))
    pr.set_phi("f", "return a <= 100;")
    pr.verified.add("f")
    pr.set_phi("f", "return a <= 200;")
    assert "f" not in pr.verified                                    # Phi changed => prior verdict is stale


# ---------------------------------------------------------------- write() output + conf
def test_write_emits_artifacts_and_conf():
    pr = _project(OverApproxTarget(cut="C", sig=_sig("f", [("uint256", "a")], ["uint256"])))
    pr.set_phi("f", "return a <= 100;")
    with tempfile.TemporaryDirectory() as d:
        setup_conf = {"files": ["C.sol"], "solc": "solc8.20", "verify": "C:Setup.spec"}
        written = {Path(p).name for p in pr.write(d, setup_conf)}
        assert {"fPhi.spec", "fSummary.spec", "fConformance.spec", "fConformance.conf"} <= written
        conf = json.loads((Path(d) / "conf" / "fConformance.conf").read_text())
        assert conf["verify"] == "C:certora/specs/fConformance.spec"
        assert conf["rule"] == [conformance_rule_name("f")] == ["overApprox_f"]
        assert conf["files"] == ["C.sol"] and "smt_timeout" in conf   # scene inherited + perf applied


# ---------------------------------------------------------------- budget + verified fallback
def _passing(rule):
    from smtool import verify as V
    return V.VerifyResult(conf=f"/x/{rule}.conf", success=True, job_url="u",
                          rules=[V.RuleVerdict(rule=rule, status="VERIFIED", passed=True)])


def test_verify_budget_refuses_when_spent(monkeypatch):
    """The verify tool must refuse a prover run once the budget is spent (bounds cost), and say so."""
    import asyncio
    from smtool.agent import overapprox_refine as R
    pr = _project(OverApproxTarget(cut="C", sig=_sig("f", [("uint256", "a")], ["uint256"])))
    pr.set_phi("f", "return a <= 100;")
    calls = {"n": 0}

    async def fake_prove(project, cfg):
        calls["n"] += 1
        return {"/x/overApprox_f.conf": _passing("overApprox_f")}
    monkeypatch.setattr(R, "_prove_and_verify", fake_prove)

    budget = [2]
    verify = R._make_verify(pr, cfg=None, budget=budget)
    assert "VERIFIED" in asyncio.run(verify())        # 1st: runs
    assert "VERIFIED" in asyncio.run(verify())        # 2nd: runs
    msg = asyncio.run(verify())                        # 3rd: refused
    assert "BUDGET SPENT" in msg and calls["n"] == 2   # prover was invoked exactly twice


def test_restore_best_verified_ships_proven_phi():
    """A tighter-but-unproven Phi left at budget-exhaustion is discarded; the last PROVEN Phi is shipped."""
    pr = _project(OverApproxTarget(cut="C", sig=_sig("f", [("uint256", "a")], ["uint256"])))
    pr.set_phi("f", "return a <= 100;")
    pr.mark_verified("f")                              # this Phi proved -> it's the fallback
    proven = pr.render_phi("f")
    pr.set_phi("f", "return a <= 50;")                 # agent tightens; budget cuts off before verify
    assert pr.render_phi("f") != proven
    pr.restore_best_verified()
    assert pr.render_phi("f") == proven                # shipped Phi is the proven one


# ---------------------------------------------------------------- specs_dir path consistency
def test_write_honors_specs_dir_basename():
    """write() must put specs in the subdir named by specs_dir's last component, so it agrees with the
    conf's specs_dir-relative `verify` path — e.g. a scene using `certora/spec` (singular), not `specs`."""
    pr = OverApproxProject.of("C", [OverApproxTarget(cut="C", sig=_sig("f", [("uint256", "a")], ["uint256"]))],
                              setup_spec_import="Setup.spec", specs_dir="certora/spec")
    pr.set_phi("f", "return a <= 100;")
    with tempfile.TemporaryDirectory() as d:
        pr.write(d, {"files": ["C.sol"], "verify": "C:Setup.spec"})
        assert (Path(d) / "spec" / "fConformance.spec").exists()          # subdir = specs_dir basename
        assert not (Path(d) / "specs").exists()                            # not the hardcoded "specs"
        conf = json.loads((Path(d) / "conf" / "fConformance.conf").read_text())
        assert conf["verify"] == "C:certora/spec/fConformance.spec"         # verify agrees with where it landed


# ---------------------------------------------------------------- void f -> no conformance rule
def test_void_target_has_no_rule():
    t = OverApproxTarget(cut="C", sig=_sig("poke", [("uint256", "a")], [], mutability="nonpayable"))
    assert build_conformance_rule(t) is None
    pr = _project(t)
    assert pr.provable_targets() == []                              # nothing to prove for a void f
