"""Signal 4 (surviving hostile primitives) — pure/offline: the generic-vs-curated
classifier, the linear-scaling exclusion, the surviving_hostile aggregation (ghost exclusion + per-primitive
reaching-method roll-up), and the detect_from fusion into an enriched candidate. The live POU fetch
(fetch_surviving_graphs) is exercised end-to-end against real runs, not here."""
from summarization_detector.detect import (
    DifficultyReport, classify_hostile, detect_from, surviving_hostile, _entry_of_rule,
)


def test_classify_generic_categories():
    def cat(name: str) -> str:
        m = classify_hostile(name)
        assert m is not None
        return m.category
    assert cat("BitLib.fls(uint256)") == "bitwise-scan"
    assert cat("Foo.popCount(uint256)") == "bitwise-scan"
    assert cat("ListLib.sortByKey(ListLib.List)") == "in-memory-sort"
    assert cat("SomeLib.pow(uint256,uint256)") == "symbolic-exp"
    assert cat("FooMath.mulDivDown(uint256,uint256,uint256)") == "nonlinear-mulDiv"


def test_generic_match_suggests_no_summary():
    # a generic (non-curated) match reports the category but leaves the summary to the consumer
    for name in ("ListLib.sortByKey(ListLib.List)", "BitLib.fls(uint256)"):
        m = classify_hostile(name)
        assert m is not None and m.curated is False and m.candidate_summary == ""


def test_curated_overlay_attaches_a_concrete_summary():
    oz = classify_hostile("Math.mulDiv(uint256,uint256,uint256)")
    assert oz is not None and oz.category == "nonlinear-mulDiv"
    assert oz.curated is True and oz.candidate_summary != ""


def test_generic_camelcase_exp_is_caught_without_hardcoding():
    # a camelCase `…Exp` name is matched structurally (no curated entry → no suggested summary)
    e = classify_hostile("PriceLib.computeExp(uint256,uint256)")
    assert e is not None and e.category == "symbolic-exp" and e.curated is False and e.candidate_summary == ""


def test_linear_constant_scaling_excluded():
    # multiply-by-constant conversions are cheap, not hostile
    assert classify_hostile("WadRayMath.bpsToWad(uint256)") is None
    assert classify_hostile("WadRayMath.toRay(uint256)") is None
    assert classify_hostile("Foo.normalizeDecimals(uint256,uint8,uint8)") is None


def _graph(method, names):
    return {"rule": f"sanity-{method}-Satisfy_x", "phase": "postOptimize", "procedures": [],
            "internalFunctions": [{"name": n, "summarizable": True} for n in names]}


def test_surviving_hostile_aggregates_reach_and_excludes_ghosts():
    graphs = [
        _graph("borrow(uint256)", ["ListLib.sortByKey(ListLib.List)", "BitLib.fls(uint256)",
                                   "CVL/Ghost Function 'mulDivUpSummary256(a,b,c)'"]),
        _graph("withdraw(uint256)", ["ListLib.sortByKey(ListLib.List)"]),   # sort reaches a 2nd method
        _graph("getX()", ["Foo.plainGetter()"]),                            # nothing hostile
    ]
    h = surviving_hostile(graphs)
    assert set(h) == {"ListLib.sortByKey(ListLib.List)", "BitLib.fls(uint256)"}  # ghost + non-hostile out
    assert sorted(h["ListLib.sortByKey(ListLib.List)"]["reaching_methods"]) == [
        "borrow(uint256)", "withdraw(uint256)"]
    assert h["BitLib.fls(uint256)"]["reaching_methods"] == ["borrow(uint256)"]
    assert h["ListLib.sortByKey(ListLib.List)"]["category"] == "in-memory-sort"


def test_detect_from_fuses_surviving_into_enriched_candidate():
    # the surviving name carries a signature; the candidate key is sig-stripped so it merges cleanly.
    # A generic match carries no summary; a curated one carries the concrete text.
    surv = surviving_hostile([_graph("borrow(uint256)", ["BitLib.fls(uint256)",
                                                         "WadRayMath.rayMul(uint256,uint256)"])])
    rep = detect_from([], DifficultyReport(), cut="Vault", surviving=surv)
    fls = next(c for c in rep.candidates if c.function == "BitLib.fls")
    assert "bitwise-scan" in fls.signals          # the hard-op class IS the signal (no separate category)
    assert fls.reaching_count == 1 and fls.candidate_summary == ""
    ray = next(c for c in rep.candidates if c.function == "WadRayMath.rayMul")
    assert "nonlinear-mulDiv" in ray.signals and ray.candidate_summary != ""


def test_entry_of_rule():
    assert _entry_of_rule("sanity-setFlag(uint256,bool,address)-Satisfy_x") == "setFlag(uint256,bool,address)"
    # the per-method Assertions graph must strip to the same bare method (not leak the raw rule name)
    assert _entry_of_rule("sanity-setFlag(uint256,bool,address)-Assertions") == "setFlag(uint256,bool,address)"


def test_surviving_dedups_satisfy_and_assertions_graphs():
    from summarization_detector.detect import surviving_hostile
    def g(rule):
        return {"rule": rule, "phase": "postOptimize", "procedures": [],
                "internalFunctions": [{"name": "BitLib.fls(uint256)", "summarizable": True}]}
    # a method's Satisfy + Assertions graphs both keep the primitive -> counted ONCE
    out = surviving_hostile([g("sanity-borrow(uint256)-Satisfy_sanity_check_failed_x"),
                             g("sanity-borrow(uint256)-Assertions")])
    assert out["BitLib.fls(uint256)"]["reaching_methods"] == ["borrow(uint256)"]
