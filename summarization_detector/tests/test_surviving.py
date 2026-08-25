"""Surviving-call-graph signal (surviving.py) — pure/offline: the generic-vs-curated classifier, the
linear-scaling exclusion, the ghost/raw split, and the per-primitive reaching-method roll-up. The live
POU fetch (fetch_surviving_postoptimize) is exercised end-to-end against real runs, not here."""
from summarization_detector.surviving import classify, scan_surviving, parse_surviving_graph


def test_classify_generic_categories():
    # generic operation tokens — no project names
    def cat(name: str) -> str:
        m = classify(name)
        assert m is not None
        return m.category
    assert cat("BitLib.fls(uint256)") == "bitwise-scan"
    assert cat("Foo.popCount(uint256)") == "bitwise-scan"
    assert cat("ListLib.sortByKey(ListLib.List)") == "in-memory-sort"
    assert cat("SomeLib.pow(uint256,uint256)") == "symbolic-exp"
    assert cat("FooMath.mulDivDown(uint256,uint256,uint256)") == "nonlinear-mulDiv"


def test_generic_sort_and_bitwise_are_over_approx_only():
    m = classify("ListLib.sortByKey(ListLib.List)")
    assert m is not None and m.exact_summary_available is False and m.curated is False
    b = classify("BitLib.fls(uint256)")
    assert b is not None and b.exact_summary_available is False


def test_curated_overlay_upgrades_to_exact():
    # a curated PUBLIC-library entry upgrades the candidate to an EXACT summary
    oz = classify("Math.mulDiv(uint256,uint256,uint256)")
    assert oz is not None and oz.category == "nonlinear-mulDiv"
    assert oz.curated is True and oz.exact_summary_available is True


def test_generic_camelcase_exp_is_caught_without_hardcoding():
    # a camelCase `…Exp` name is matched structurally (no curated entry → generic over-approx candidate)
    e = classify("PriceLib.computeExp(uint256,uint256)")
    assert e is not None and e.category == "symbolic-exp" and e.curated is False


def test_linear_constant_scaling_excluded():
    # multiply-by-constant conversions are cheap, not hostile
    assert classify("WadRayMath.bpsToWad(uint256)") is None
    assert classify("WadRayMath.toRay(uint256)") is None
    assert classify("Foo.normalizeDecimals(uint256,uint8,uint8)") is None


def test_scan_splits_raw_candidates_from_ghosts_and_counts_methods():
    def graph(method, names):
        return {"rule": f"sanity-{method}-Satisfy_x", "phase": "postOptimize",
                "procedures": [], "internalFunctions": [{"name": n, "summarizable": True} for n in names]}
    graphs = [
        graph("borrow(uint256)", ["ListLib.sortByKey(ListLib.List)", "BitLib.fls(uint256)",
                                  "CVL/Ghost Function 'mulDivUpSummary256(a,b,c)'"]),
        graph("withdraw(uint256)", ["ListLib.sortByKey(ListLib.List)"]),   # sort reaches a 2nd method
        graph("getX()", ["Foo.plainGetter()"]),                            # nothing hostile
    ]
    rep = scan_surviving(graphs)
    assert rep.n_methods == 3
    names = {c.name: c for c in rep.candidates}
    assert set(names) == {"ListLib.sortByKey(ListLib.List)", "BitLib.fls(uint256)"}   # ghost excluded
    assert len(names["ListLib.sortByKey(ListLib.List)"].reaching_methods) == 2        # borrow + withdraw
    assert names["BitLib.fls(uint256)"].reaching_methods == ["borrow(uint256)"]
    assert rep.already_summarized == ["CVL/Ghost Function 'mulDivUpSummary256(a,b,c)'"]


def test_parse_extracts_entry_method_and_summarizable_flag():
    entry, names = parse_surviving_graph({
        "rule": "sanity-updateFlag(uint256,bool,address)-Satisfy_sanity_check_failed",
        "phase": "postOptimize",
        "procedures": [{"procId": "Vault.updateFlag(uint256,bool,address)", "range": {}}],
        "internalFunctions": [{"name": "BitLib.fls(uint256)", "summarizable": False}]})
    assert entry == "updateFlag(uint256,bool,address)"
    assert ("BitLib.fls(uint256)", False) in names
