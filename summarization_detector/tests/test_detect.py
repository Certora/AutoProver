"""Summarization-target detector (detect.py). Fast, offline, no prover/scene:
 - scan_ast: a synthetic solc-AST fixture exercises span-attribution, the `abi.`-base guard (a non-abi
   `.encode()` is NOT hashing), source-file dedup, and the lib/ dependency flag.
 - detect_from: the signal fusion (per-function candidates, ranked).
No real `.asts.json` is needed — scan_ast streams any JSON of the documented shape."""
import json
import tempfile
from pathlib import Path

from summarization_detector.detect import (
    scan_ast, detect_from, HashSignal, reachable_from_main, cone_weights, _touch_missing_imported_specs,
    _function_locations,
    _survives, _caller_boundaries, _expressible_typename,
)
from summarization_detector.difficulty import DifficultyReport, Hotspot


def _node(nt, off, length, **kw):
    return {"nodeType": nt, "src": f"{off}:{length}:1", **kw}


def _args(*specs):
    """Each spec is a type string, or a (type, ref_id) tuple attaching a `referencedDeclaration` — used to
    model an argument that reads a function PARAMETER (the user-input test for dynamic-length hashing)."""
    out = []
    for s in specs:
        if isinstance(s, tuple):
            out.append({"typeDescriptions": {"typeString": s[0]}, "referencedDeclaration": s[1]})
        else:
            out.append({"typeDescriptions": {"typeString": s}})
    return out


def _fdef(off, length, name, *, vis="internal", mut="pure", param_ids=()):
    return _node("FunctionDefinition", off, length, name=name, stateMutability=mut, visibility=vis,
                 parameters={"parameters": [{"id": i} for i in param_ids]})


def _ident_call(off, name, *type_strings):
    return _node("FunctionCall", off, 8, arguments=_args(*type_strings),
                 expression={"nodeType": "Identifier", "name": name})


def _member_call(off, member, base, *type_strings):
    return _node("FunctionCall", off, 8, arguments=_args(*type_strings),
                 expression={"nodeType": "MemberAccess", "memberName": member,
                             "expression": {"nodeType": "Identifier", "name": base}})


def _write_ast(tmp, files: dict) -> Path:
    """files: {abs_path: {node_id: node}} -> a .asts.json keyed by one compilation unit per file."""
    doc = {abs_path: {abs_path: nodes} for abs_path, nodes in files.items()}
    p = Path(tmp) / "x.asts.json"
    p.write_text(json.dumps(doc))
    return p


def test_scan_ast_attributes_guards_and_flags():
    nodes = {
        "1": _node("ContractDefinition", 0, 200, name="Lib", contractKind="library"),
        "2": _node("FunctionDefinition", 10, 90, name="hashIt", stateMutability="pure", visibility="internal"),
        "3": _ident_call(50, "keccak256"),                 # in hashIt -> flagged
        "4": _member_call(60, "encodePacked", "abi"),      # in hashIt -> flagged
        "5": _node("FunctionDefinition", 110, 80, name="plain", stateMutability="view", visibility="public"),
        "6": _member_call(150, "encode", "myCodec"),       # NON-abi .encode in plain -> NOT flagged
        "7": _ident_call(195, "keccak256"),                # file-scope (outside any fn) -> skipped
    }
    with tempfile.TemporaryDirectory() as d:
        sigs = scan_ast(_write_ast(d, {"src/A.sol": nodes}))
    assert len(sigs) == 1
    s = sigs[0]
    assert s.function == "Lib.hashIt" and s.contract == "Lib" and s.name == "hashIt"
    assert s.mutability == "pure" and s.visibility == "internal"
    assert s.patterns == ("abi.encodePacked", "keccak256")   # sorted; the non-abi encode is excluded
    assert s.is_dependency is False


def test_scan_ast_requires_hash_trigger_and_classifies_length():
    """Only a function that calls a HASH builtin is a candidate — an encoder-only function (serialization
    or calldata via encodeWithSelector) is NOT. Length class: a hash over dynamic data (bytes/string) is
    dynamic-input; over fixed-size fields it is fixed."""
    nodes = {
        "1": _node("ContractDefinition", 0, 400, name="H", contractKind="contract"),
        # fixed-size digest: keccak(abi.encode(uint256, address))
        "2": _fdef(10, 80, "hashFixed"),
        "3": _ident_call(30, "keccak256", "bytes memory"),
        "4": _member_call(40, "encode", "abi", "uint256", "address"),
        # dynamic digest over a PARAMETER (id 900): keccak(abi.encodePacked(bytes param))
        "5": _fdef(100, 80, "hashDyn", param_ids=(900,)),
        "6": _ident_call(120, "keccak256", "bytes memory"),
        "7": _member_call(130, "encodePacked", "abi", ("bytes memory", 900)),
        # serialization only (abi.encode, no hash) -> dropped
        "8": _fdef(200, 80, "serialize"),
        "9": _member_call(220, "encode", "abi", "uint256"),
        # calldata construction (encodeWithSelector is not even a tracked encoder) -> dropped
        "10": _fdef(300, 80, "callData", vis="internal", mut="view"),
        "11": _member_call(320, "encodeWithSelector", "abi", "bytes4", "address"),
    }
    with tempfile.TemporaryDirectory() as d:
        sigs = {s.function: s for s in scan_ast(_write_ast(d, {"src/H.sol": nodes}))}
    assert set(sigs) == {"H.hashFixed", "H.hashDyn"}       # serialize + callData dropped (no hash trigger)
    assert sigs["H.hashFixed"].dynamic_input is False
    assert sigs["H.hashDyn"].dynamic_input is True         # dynamic operand reads a parameter


def test_scan_ast_constant_dynamic_typed_input_is_not_dynamic():
    """A hash over a dynamic-TYPED but non-parameter operand (a constant/immutable/local, e.g.
    `keccak256(bytes(name))` for a fixed EIP-712 name) is NOT dynamic-input — it is bounded/cheap."""
    nodes = {
        "1": _node("ContractDefinition", 0, 200, name="E", contractKind="contract"),
        "2": _fdef(10, 90, "nameHash", vis="internal", mut="view"),        # no params
        "3": _ident_call(40, "keccak256", "bytes memory"),                 # arg reads a LOCAL, not a param
    }
    with tempfile.TemporaryDirectory() as d:
        sigs = {s.function: s for s in scan_ast(_write_ast(d, {"src/E.sol": nodes}))}
    assert set(sigs) == {"E.nameHash"}
    assert sigs["E.nameHash"].dynamic_input is False                       # constant-valued -> not expensive


def test_scan_ast_drops_ecrecover_only():
    """`ecrecover`-only functions are dropped (a recovered address has no over-approximable property); a
    function that also hashes is kept."""
    nodes = {
        "1": _node("ContractDefinition", 0, 300, name="S", contractKind="contract"),
        "2": _fdef(10, 90, "recover", param_ids=(900,)),                   # only ecrecover -> dropped
        "3": _ident_call(40, "ecrecover", "bytes32", "uint8", "bytes32", "bytes32"),
        "4": _fdef(110, 90, "digestAndRecover", param_ids=(901,)),         # keccak + ecrecover -> kept
        "5": _ident_call(140, "keccak256", ("bytes memory", 901)),
        "6": _ident_call(160, "ecrecover", "bytes32", "uint8", "bytes32", "bytes32"),
    }
    with tempfile.TemporaryDirectory() as d:
        sigs = {s.function: s for s in scan_ast(_write_ast(d, {"src/S.sol": nodes}))}
    assert set(sigs) == {"S.digestAndRecover"}                            # recover (ecrecover-only) dropped


def test_scan_ast_dedups_and_flags_dependency():
    """A source file recurs under many compilation units — processed ONCE. A lib/ path is a dependency."""
    lib_nodes = {
        "1": _node("ContractDefinition", 0, 100, name="ERC20", contractKind="contract"),
        "2": _node("FunctionDefinition", 10, 80, name="permit", stateMutability="nonpayable", visibility="public"),
        "3": _ident_call(40, "keccak256"),
    }
    # same abs path appears under two compilation units (two top-level rel keys)
    p = Path(tempfile.mkdtemp()) / "x.asts.json"
    p.write_text(json.dumps({
        "unitA": {"lib/solady/src/tokens/ERC20.sol": lib_nodes},
        "unitB": {"lib/solady/src/tokens/ERC20.sol": lib_nodes},
    }))
    sigs = scan_ast(p)
    assert len(sigs) == 1 and sigs[0].function == "ERC20.permit"   # deduped, not counted twice
    assert sigs[0].is_dependency is True                            # under lib/


def test_detect_from_fuses_and_classifies():
    """Signals fuse per function: a cross-contract hotspot gets `external`+`nonlinear`; the CUT's own
    INLINED internal math gets `nonlinear`; a hasher gets `hashing`. The CUT's own EXTERNAL method (an
    unmarked `<CUT>.m` — a rule subject) is dropped. Dependency hashing is dropped by default."""
    diff = DifficultyReport(hotspots=[
        Hotspot("Oracle.getPrice", 40, "O.sol:10"),          # external (not the CUT)
        Hotspot("(internal) C.mulThing", 55, "C.sol:5"),     # the CUT's own inlined internal math
        Hotspot("C.borrow", 70, "C.sol:9"),                  # the CUT's own EXTERNAL method (rule subject)
    ])
    hs = [
        HashSignal("C.hashId", "C", "hashId", "pure", "internal", ("keccak256",), "src/C.sol", False),
        HashSignal("Dep.enc", "Dep", "enc", "pure", "internal", ("abi.encode",), "lib/x/Dep.sol", True),
    ]
    rep = detect_from(hs, diff, cut="C")
    by = {c.function: c for c in rep.candidates}

    assert "external" in by["Oracle.getPrice"].signals
    assert by["C.mulThing"].signals == ("nonlinear",)          # CUT's own internal math, not external
    assert "C.borrow" not in by                                # CUT's external entry method dropped
    assert "hashing" in by["C.hashId"].signals
    assert "Dep.enc" not in by                                  # dependency hashing filtered by default
    assert rep.candidates == sorted(rep.candidates, key=lambda c: (-c.score, c.function))   # ranked


def test_detect_from_surfaces_hot_cvl_ghost_but_drops_cheap_one():
    # A CVL/ghost hotspot is an already-applied summary. A cheap one is dropped (it IS the summary and no
    # longer a problem); one STILL contributing nonlinear ops means its summary is itself nonlinear (e.g. an
    # exact mulDiv summary) and is surfaced as an `already-summarised` coarsening target.
    from summarization_detector.detect import ALREADY_SUMMARISED_MIN_PCT
    diff = DifficultyReport(hotspots=[
        Hotspot("CVL/Ghost Function 'mulDivDownSummary(x,y,denominator)'", 86, ""),           # still hot
        Hotspot("CVL/Ghost Function 'edgeGhost(a)'", ALREADY_SUMMARISED_MIN_PCT, ""),          # at floor -> kept
        Hotspot("CVL/Ghost Function 'cheapGhost(a,b)'", ALREADY_SUMMARISED_MIN_PCT - 1, ""),   # below -> dropped
        Hotspot("AmountConverter.getExpectedOut", 20, "contracts/AmountConverter.sol:134"),    # external callee
    ])
    rep = detect_from([], diff, cut="Stonks")
    by = {c.function: c for c in rep.candidates}
    assert "AmountConverter.getExpectedOut" in by                                    # real raw math still kept
    # the hot ghost is surfaced under its summary name, flagged already-summarised, with coarsen guidance
    hot = by["mulDivDownSummary(x,y,denominator)"]
    assert hot.signals == ("already-summarised",)
    assert "coarsen" in hot.evidence.lower()
    assert hot.file == "" and hot.line is None                                        # a ghost has no source loc
    assert "edgeGhost(a)" in by                                                       # exactly at the floor -> kept
    assert not any("cheapGhost" in f for f in by)                                     # below the floor -> dropped


def test_detect_from_keeps_cut_internal_math_but_drops_cut_external_method():
    # (internal)-marked CUT hotspot = an inlined internal fn (summarizable); an unmarked CUT hotspot = the
    # CUT's own external method (a rule subject) and is dropped.
    diff = DifficultyReport(hotspots=[
        Hotspot("(internal) Stonks.estimateTradeOutput", 20, "S.sol:387"),
        Hotspot("Stonks.swap", 80, "S.sol:40"),
    ])
    by = {c.function: c for c in detect_from([], diff, cut="Stonks").candidates}
    assert by["Stonks.estimateTradeOutput"].signals == ("nonlinear",)   # internal CUT math kept, not external
    assert "Stonks.swap" not in by                                      # CUT's external method dropped


def test_detect_from_nonlinear_floor_and_internal_dedup():
    from summarization_detector.detect import NONLINEAR_MIN_PCT
    diff = DifficultyReport(hotspots=[
        Hotspot("Hub.previewShares", NONLINEAR_MIN_PCT, "Hub.sol:471"),  # exactly at the floor -> kept
        Hotspot("Hub.getIndex", NONLINEAR_MIN_PCT - 1, "Hub.sol:508"),   # below the floor -> dropped
        Hotspot("(internal) Hub.calcRay", 27, "AssetLogic.sol:153"),     # kept (top instance of calcRay)
        Hotspot("(internal) Spoke.calcRay", 25, "Spoke.sol:639"),        # same bare name, lower -> deduped
    ])
    by = {c.function: c for c in detect_from([], diff, cut="Spoke").candidates}
    assert "Hub.previewShares" in by and "Hub.getIndex" not in by
    assert "Hub.calcRay" in by and "Spoke.calcRay" not in by            # caller-attribution dedup by bare name


def _fndef(off, length, name, vis="internal"):
    return _node("FunctionDefinition", off, length, name=name, stateMutability="pure", visibility=vis)


def _call_ref(off, ref):
    return _node("FunctionCall", off, 8, expression={"nodeType": "Identifier", "name": "x", "referencedDeclaration": ref})


def _reach_fixture(tmp, orphan_name="orphan"):
    """Main.entry (external) internally calls `reached`; `<orphan_name>` is defined but uncalled. Both
    reached and orphan hash (keccak) so scan_ast flags them; reachability decides which survives."""
    nodes = {
        "1": _node("ContractDefinition", 0, 400, name="Main", contractKind="contract"),
        "10": _fndef(10, 90, "entry", vis="external"),
        "11": _call_ref(50, 20),                            # entry -> reached (referencedDeclaration=20)
        "20": _fndef(110, 90, "reached"),
        "21": _ident_call(150, "keccak256", "bytes memory"),
        "22": _member_call(155, "encodePacked", "abi", "bytes"),
        "30": _fndef(210, 90, orphan_name),
        "31": _ident_call(250, "keccak256", "bytes memory"),
        "32": _member_call(255, "encodePacked", "abi", "bytes"),
    }
    return _write_ast(tmp, {"src/Main.sol": nodes})


def test_survives_matches_free_functions_by_bare_name():
    # The prover attributes a free (file-level) function to an arbitrary host in the surviving set.
    surviving = {"Ownable.computeBaseHash", "CombinatorialModule.getConditionId", "Router.convert"}
    bare = {n.rpartition(".")[2] for n in surviving}
    # host-less candidate (a free function) matches by bare name despite the mis-attributed host
    assert _survives("computeBaseHash", surviving, bare)
    # hosted candidate matches only exactly — a genuinely-unreachable CTHelpers.getConditionId is NOT
    # resurrected by the same-named method on another contract
    assert not _survives("CTHelpers.getConditionId", surviving, bare)
    assert _survives("CombinatorialModule.getConditionId", surviving, bare)
    # host-less candidate with no bare-name match stays out
    assert not _survives("notReachable", surviving, bare)


def _elem(name):
    return {"nodeType": "ElementaryTypeName", "name": name}


def _arr(base):
    return {"nodeType": "ArrayTypeName", "baseType": base}


def _udt(ref):
    return {"nodeType": "UserDefinedTypeName", "referencedDeclaration": ref}


def test_expressible_typename_recurses_arrays_and_structs():
    # value types / fixed bytes expressible; dynamic bytes/string not
    assert _expressible_typename(_elem("uint256"), {}) and _expressible_typename(_elem("bytes32"), {})
    assert not _expressible_typename(_elem("bytes"), {}) and not _expressible_typename(_elem("string"), {})
    # NESTING the flat check missed: bytes[] and struct-with-a-bytes-field are NOT expressible
    assert _expressible_typename(_arr(_elem("uint256")), {})
    assert not _expressible_typename(_arr(_elem("bytes")), {})            # bytes[]
    merged = {
        "10": {"nodeType": "UserDefinedValueTypeDefinition"},             # a UDVT (e.g. PositionId)
        "20": {"nodeType": "StructDefinition", "members": [{"typeName": _elem("bytes")}]},
        "30": {"nodeType": "StructDefinition", "members": [{"typeName": _elem("uint256")}]},
        "50": {"nodeType": "StructDefinition",                            # struct WITH a mapping field
               "members": [{"typeName": {"nodeType": "Mapping"}}, {"typeName": _elem("uint256")}]},
    }
    assert _expressible_typename(_udt(10), merged)                        # UDVT -> value-like
    assert _expressible_typename(_arr(_udt(10)), merged)                  # PositionId[] -> expressible
    assert not _expressible_typename(_udt(20), merged)                   # struct WITH a bytes field
    assert _expressible_typename(_udt(30), merged)                       # struct of value types
    assert not _expressible_typename({"nodeType": "Mapping"}, merged)    # mapping
    assert not _expressible_typename(_udt(50), merged)                   # mapping nested in a struct


def test_caller_boundaries_keeps_only_expressible_internal_callers():
    # leaf <- encodeFromData (opaque, dropped) <- mutClose (clean+mutating) <- pureFar (clean+pure);
    # a public entry point that also calls the leaf is dropped (the rule's subject, not a boundary).
    edges = {
        "L.encodeFromData": {"computeBaseHash"},
        "M.mutClose": {"L.encodeFromData"},
        "M.pureFar": {"M.mutClose"},
        "C.borrow": {"computeBaseHash"},
    }
    facts = {
        "L.encodeFromData": FnFacts("encodeFromData(uint256, bytes) -> C", expressible=False),  # opaque -> dropped
        "M.mutClose": FnFacts("mutClose(PositionId[]) -> C", expressible=True, mutating=True),   # clean but mutating
        "M.pureFar": FnFacts("pureFar(PositionId[]) -> C", expressible=True, mutating=False),    # clean + pure
        "C.borrow": FnFacts("borrow(uint256) -> uint256", expressible=True, external=True),      # external -> dropped
    }
    reach = {"computeBaseHash", "L.encodeFromData", "M.mutClose", "M.pureFar", "C.borrow"}
    b = _caller_boundaries(edges, facts, "computeBaseHash", reach)
    # opaque + external-entry both dropped; pure+clean wins over mutating-clean (view-preference beats distance)
    assert [x.function for x in b] == ["M.pureFar", "M.mutClose"]
    assert not b[0].mutating and b[1].mutating


def test_caller_boundaries_filters_to_reachable():
    edges = {"C.caller": {"computeBaseHash"}}
    facts = {"C.caller": FnFacts("caller(uint256) -> bytes32", expressible=True)}   # expressible, pure, internal
    assert _caller_boundaries(edges, facts, "computeBaseHash", reachable={"computeBaseHash"}) == []  # caller unreached
    assert _caller_boundaries(edges, facts, "computeBaseHash", reachable=None)                       # no filter -> kept


def test_boundary_format_flags_mutating_vs_pure():
    from summarization_detector.detect import DetectionReport, Candidate, Boundary
    # only expressible, internal boundaries reach the report; mutating is the last remaining caveat
    rep = DetectionReport(candidates=[Candidate(
        "leaf", ("hashing",), 10.0, "ev",
        boundaries=[
            Boundary("A.pureView", 1, "pureView(address) -> b32", mutating=False),
            Boundary("A.mutClose", 2, "mutClose(address) -> b32", mutating=True),
        ])])
    lines = rep.format().splitlines()
    pure_line = next(ln for ln in lines if "pureView" in ln)
    mut_line = next(ln for ln in lines if "mutClose" in ln)
    assert "summarizable here" in pure_line
    assert "state-changing" in mut_line


def test_descend_to_prims_finds_shared_nonlinear_primitive():
    from summarization_detector.detect import _descend_to_prims, _is_nonlinear_prim
    assert _is_nonlinear_prim("MathLib.mulDivDown") and not _is_nonlinear_prim("V.previewDeposit")
    edges = {                                                   # both methods reach MathLib.mulDivDown
        "V.previewDeposit": {"V.accrueInterestView", "MathLib.mulDivDown"},
        "V.accrueInterestView": {"MathLib.mulDivDown"},
        "V.deposit": {"V.previewDeposit"},
    }
    assert _descend_to_prims(edges, "V.previewDeposit", None) == {"MathLib.mulDivDown": 1}
    assert _descend_to_prims(edges, "V.deposit", None) == {"MathLib.mulDivDown": 2}     # transitive, min depth
    # a primitive not in the reachable set is dropped (never suggest dead code)
    assert _descend_to_prims(edges, "V.previewDeposit", reachable={"V.previewDeposit"}) == {}


def test_boundary_down_direction_renders_shared_count():
    from summarization_detector.detect import DetectionReport, Candidate, Boundary
    rep = DetectionReport(candidates=[Candidate(
        "V.previewDeposit", ("nonlinear",), 100.0, "ev",
        boundaries=[Boundary("MathLib.mulDivDown", 2, "mulDivDown(uint256, uint256, uint256) -> uint256",
                             mutating=False, direction="down", shared=8)])])
    line = next(ln for ln in rep.format().splitlines() if "mulDivDown" in ln)
    assert "↓" in line and "shared ×8" in line and "summarizable here" in line


def test_reachable_from_main_internal_edges():
    """BFS from Main's external entry over AST referencedDeclaration edges: `reached` is in, `orphan` out.
    Gating scan_ast on the reachable set drops the scene-unreachable hashing candidate."""
    with tempfile.TemporaryDirectory() as d:
        ast = _reach_fixture(d)
        reach = reachable_from_main(ast, "Main")
        assert "Main.reached" in reach and "Main.orphan" not in reach
        cands = {h.function for h in scan_ast(ast)}
        assert cands == {"Main.reached", "Main.orphan"}                 # scan flags both
        gated = {c for c in cands if c in reach}
        assert gated == {"Main.reached"}                               # reachability drops the orphan


def test_cone_weights_sums_consumer_code_size():
    """Cone-of-influence heuristic: a function's weight is the total body size of its transitive consumers
    (reachable callers). `Main.reached` is consumed only by `Main.entry` (body size 90); a root has none;
    an unreachable function is not weighted."""
    with tempfile.TemporaryDirectory() as d:
        ast = _reach_fixture(d)
        cone = cone_weights(ast, "Main")
        assert cone["Main.reached"] == 90       # consumed by Main.entry (span 10..100 -> 90 bytes)
        assert cone["Main.entry"] == 0          # a root — nothing consumes it
        assert "Main.orphan" not in cone        # unreachable -> not weighted


def test_reachable_from_main_external_dispatch_edge():
    """A dispatch call site (no resolved contract, selector `merge`) in externalCallGraph.json makes the
    otherwise-unreachable `Main.merge` reachable via selector-name matching."""
    with tempfile.TemporaryDirectory() as d:
        ast = _reach_fixture(d, orphan_name="merge")
        assert "Main.merge" not in reachable_from_main(ast, "Main")     # unreachable without external edges
        ecg = Path(d) / "externalCallGraph.json"
        ecg.write_text(json.dumps({
            "Main": [{"caller": "entry(uint256)", "selectors": [{"kind": "sighash", "signature": "merge(uint256)"}],
                      "targets": [{"resolution": "symbolicOutput"}]}]           # dispatch: no contract
        }))
        reach = reachable_from_main(ast, "Main", ecg)
        assert "Main.merge" in reach                                    # selector-matched dispatch edge


def test_touch_missing_imported_specs_recreates_empty_placeholders():
    """The empty-file-skip workaround: parse certoraRun's missing-import error and recreate the (empty)
    imported spec so a re-run resolves it. Only acts on the missing-import error, and never clobbers."""
    with tempfile.TemporaryDirectory() as d:
        missing = Path(d) / "certora" / "specs" / "summaries" / "C_call_resolution.spec"
        err = (f'In {d}/certora/specs/sanity-C.spec, the following import declarations do not import '
               f'existing .spec files:\n2:2:"{missing}"\n')
        created = _touch_missing_imported_specs("", err)
        assert created == [missing]
        assert missing.exists() and missing.read_text() == ""      # empty placeholder, matches the real file
        assert _touch_missing_imported_specs("", err) == []        # already exists -> no-op, no clobber
        assert _touch_missing_imported_specs("some unrelated compile error") == []   # only the import error


def test_detect_from_includes_dependencies_when_asked():
    hs = [HashSignal("Dep.enc", "Dep", "enc", "pure", "internal", ("abi.encode",), "lib/x/Dep.sol", True)]
    rep = detect_from(hs, DifficultyReport(), cut="C", include_dependencies=True)
    assert any(c.function == "Dep.enc" for c in rep.candidates)


def test_function_locations_file_and_line():
    """file always (the AST node group's path); line resolved from the source byte-offset when
    `sources_root` is given and the file is readable, else None."""
    # "foo" starts at byte 12 = after "line1\n" (6) + "line2\n" (6) -> line 3
    nodes = {
        "1": _node("ContractDefinition", 0, 200, name="Lib"),
        "2": _node("FunctionDefinition", 12, 50, name="foo"),
    }
    with tempfile.TemporaryDirectory() as d:
        ast = _write_ast(d, {"src/A.sol": nodes})
        (Path(d) / "src").mkdir()
        (Path(d) / "src/A.sol").write_text("line1\nline2\nfunction foo() {}\n")
        with_src = _function_locations(ast, sources_root=d)
        no_src = _function_locations(ast)
    assert with_src["Lib.foo"] == ("src/A.sol", 3)
    assert no_src["Lib.foo"] == ("src/A.sol", None)          # file only, line unresolved without sources


def test_detect_from_caps_each_category_and_reports_dropped():
    from summarization_detector.detect import detect_from, HashSignal, MAX_PER_CATEGORY
    from summarization_detector.difficulty import DifficultyReport
    cap = MAX_PER_CATEGORY["hashing"]
    # more hashers than the hashing cap -> that category is bounded to its cap and `dropped` records the rest
    hs = [HashSignal(function=f"C.h{i}", contract="C", name=f"h{i}", mutability="view",
                     visibility="internal", patterns=("keccak256",), file="src/C.sol",
                     is_dependency=False, dynamic_input=True)
          for i in range(cap + 5)]
    rep = detect_from(hs, DifficultyReport(), cut="C")
    hashing = [c for c in rep.candidates if "hashing" in c.signals]
    assert len(hashing) == cap        # hashing category capped
    assert rep.dropped == 5           # the 5 over the cap are dropped


def test_surviving_score_is_flat_regardless_of_reach():
    from summarization_detector.detect import detect_from, SURVIVING_SCORE
    from summarization_detector.difficulty import DifficultyReport
    # two primitives, wildly different reach -> same (flat) score; reach rides along as reaching_count only
    surviving = {
        "L.wide(uint256)":   {"category": "symbolic-exp", "reason": "", "reaching_methods": [f"m{i}" for i in range(20)],
                              "summarizable": True, "candidate_summary": ""},
        "L.narrow(uint256)": {"category": "bitwise-scan", "reason": "", "reaching_methods": ["only"],
                              "summarizable": True, "candidate_summary": ""},
    }
    rep = detect_from([], DifficultyReport(), cut="C", surviving=surviving)
    by = {c.function: c for c in rep.candidates}
    assert by["L.wide"].score == SURVIVING_SCORE and by["L.narrow"].score == SURVIVING_SCORE
    assert by["L.wide"].reaching_count == 20 and by["L.narrow"].reaching_count == 1
    assert "reaches 20" in by["L.wide"].evidence      # count still reported in evidence


def test_fn_facts_gives_signature_and_mutating_with_bare_fallback():
    from summarization_detector.detect import _fn_facts, _by_qual_or_bare
    merged = {
        "1": {"nodeType": "ContractDefinition", "src": "0:200:1", "name": "AssetLogic"},
        "2": {"nodeType": "FunctionDefinition", "src": "10:80:1", "name": "calcRay", "stateMutability": "view",
              "visibility": "internal",
              "parameters": {"parameters": [{"typeDescriptions": {"typeString": "uint256"}}]},
              "returnParameters": {"parameters": [{"typeDescriptions": {"typeString": "uint256"}}]}},
        "3": {"nodeType": "ContractDefinition", "src": "300:200:1", "name": "Hub"},
        "4": {"nodeType": "FunctionDefinition", "src": "310:80:1", "name": "add", "stateMutability": "nonpayable",
              "visibility": "external",
              "parameters": {"parameters": [{"typeDescriptions": {"typeString": "uint256"}}]},
              "returnParameters": {"parameters": []}},
    }
    facts = _fn_facts(merged)
    assert facts["AssetLogic.calcRay"].signature == "calcRay(uint256) -> uint256"
    assert facts["AssetLogic.calcRay"].mutating is False
    assert facts["Hub.add"].signature == "add(uint256)" and facts["Hub.add"].mutating is True  # nonpayable
    assert facts["Hub.add"].external is True                                     # external -> a rule subject
    # the attach's bare-name fallback: a caller-attributed hotspot resolves by bare name
    hit = _by_qual_or_bare(facts, "X.calcRay")
    assert hit is not None and hit.signature == "calcRay(uint256) -> uint256"


def test_function_locations_normalizes_absolute_ast_paths():
    # solc records some source-unit keys absolute (remapped imports); the stored `file` must still be
    # project-relative (uniform with the relative-keyed ones), with the line read from the absolute path.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "inputs" / ".certora_sources"
        absfile = root / "src" / "math" / "MathUtils.sol"
        absfile.parent.mkdir(parents=True)
        absfile.write_text("a\nb\nfunction uncheckedExp() {}\n")
        doc = {
            str(absfile): {str(absfile): {                                    # ABSOLUTE source-unit key
                "1": {"nodeType": "ContractDefinition", "src": "0:80:1", "name": "MathUtils"},
                "2": {"nodeType": "FunctionDefinition", "src": "4:20:1", "name": "uncheckedExp"}}},
            "src/dep/LibBit.sol": {"src/dep/LibBit.sol": {                    # already-relative key
                "3": {"nodeType": "ContractDefinition", "src": "0:80:1", "name": "LibBit"},
                "4": {"nodeType": "FunctionDefinition", "src": "4:20:1", "name": "fls"}}},
        }
        ap = Path(d) / "x.asts.json"
        ap.write_text(json.dumps(doc))
        locs = _function_locations(ap, sources_root=root)
    assert locs["MathUtils.uncheckedExp"] == ("src/math/MathUtils.sol", 3)   # absolute key -> relative
    assert locs["LibBit.fls"][0] == "src/dep/LibBit.sol"                      # relative key -> unchanged


# --- toxic-entrypoint -> shallowest inner boundary ---------------------------

from summarization_detector.detect import (  # noqa: E402
    _shallowest_view_boundary, _entrypoint_in_edges,
)


def test_detect_from_collects_toxic_entrypoints_top5_by_pct():
    # Six CUT-external hotspots (rule subjects) -> not candidates, but retained as toxic entrypoints,
    # highest-% first, capped at 5.
    diff = DifficultyReport(hotspots=[
        Hotspot("C.liquidationCall", 78, "C.sol:1"),
        Hotspot("C.setUsingAsCollateral", 70, "C.sol:2"),
        Hotspot("C.updateUserRiskPremium", 70, "C.sol:3"),
        Hotspot("C.borrow", 58, "C.sol:4"),
        Hotspot("C.withdraw", 54, "C.sol:5"),
        Hotspot("C.getUserAccountData", 40, "C.sol:6"),   # 6th -> dropped by top-5
    ])
    rep = detect_from([], diff, cut="C")
    assert [f for f, _ in rep.toxic_entrypoints] == [
        "C.liquidationCall", "C.setUsingAsCollateral", "C.updateUserRiskPremium", "C.borrow", "C.withdraw",
    ]
    assert rep.candidates == []  # the external subjects themselves are never candidates


def _sig(sig, expressible, mutating, external):
    return FnFacts(sig, expressible=expressible, mutating=mutating, external=external)


def test_shallowest_view_boundary_picks_aggregating_view():
    # liquidationCall -> liquidateUser (mutating) -> _calculateLiquidationAmounts (view, expressible,
    # reaches mulDiv) -> mulDiv (primitive). The boundary is _calculateLiquidationAmounts.
    edges = {
        "C.liquidationCall": {"C.liquidateUser", "C.getConfig"},
        "C.liquidateUser": {"C._calculateLiquidationAmounts"},
        "C._calculateLiquidationAmounts": {"M.mulDiv"},
        "C.getConfig": set(),  # a trivial view getter, reaches no primitive
        "M.mulDiv": set(),
    }
    sigs = {
        "C.liquidationCall": _sig("liquidationCall()", True, True, True),      # external subject
        "C.liquidateUser": _sig("liquidateUser()", True, True, False),         # mutating -> not a boundary
        "C.getConfig": _sig("getConfig()->uint", True, False, False),          # view but no prim -> skip
        "C._calculateLiquidationAmounts": _sig("_calc()->Amounts", True, False, False),  # THE boundary
        "M.mulDiv": _sig("mulDiv()->uint", True, False, False),
    }
    assert _shallowest_view_boundary(edges, sigs, "C.liquidationCall") == "C._calculateLiquidationAmounts"


def test_shallowest_view_boundary_none_when_no_view_aggregator():
    edges = {"C.f": {"M.mulDiv"}, "M.mulDiv": set()}
    sigs = {"C.f": _sig("f()", True, True, True), "M.mulDiv": _sig("mulDiv()", True, False, False)}
    # f is mutating+external; mulDiv is a bare primitive (excluded) -> no boundary
    assert _shallowest_view_boundary(edges, sigs, "C.f") is None


def test_entrypoint_in_edges_bare_name_fallback():
    # difficulty names it by the CUT; the AST call graph keys it by the declaring (inherited) contract.
    edges = {"Spoke.liquidationCall": {"x"}}
    assert _entrypoint_in_edges(edges, "SpokeInstance.liquidationCall") == "Spoke.liquidationCall"
    assert _entrypoint_in_edges(edges, "Spoke.liquidationCall") == "Spoke.liquidationCall"
    # ambiguous bare name -> None
    edges2 = {"A.f": set(), "B.f": set()}
    assert _entrypoint_in_edges(edges2, "C.f") is None


# --- branching / path-count signal (the extension) ---------------------------

from summarization_detector.detect import BRANCHING_MIN_PCT, FnFacts, _is_reference_typename
from summarization_detector.difficulty import _parse_hotspots, _BRANCHING_NODE_LABEL, _BRANCHING_PCT_RE


def test_difficulty_parses_path_count_hotspots_separately_from_nonlinearity():
    tree = {"label": "root", "children": [
        {"label": "path count hotspots", "children": [
            {"label": "function: (internal) Router._insertLeg",
             "value": "contrib. to branching: 39 %",
             "jumpToDefinition": {"file": "Router.sol", "start": {"line": 433}}},
            {"label": "function: CombinatorialModule.getLegs", "value": "contrib. to branching: 9 %"},
        ]},
        {"label": "nonlinearity hotspots", "children": [
            {"label": "function: Lib.mulDiv", "value": "contrib. to nonlinear ops: 50 %"},
        ]},
    ]}
    br = {}
    _parse_hotspots(tree, br, _BRANCHING_NODE_LABEL, _BRANCHING_PCT_RE)
    assert br["(internal) Router._insertLeg"].pct == 39
    assert br["(internal) Router._insertLeg"].location == "Router.sol:433"
    assert br["CombinatorialModule.getLegs"].pct == 9
    assert not any("mulDiv" in k for k in br)          # the nonlinearity node is NOT collected as branching


def test_detect_from_emits_branching_candidates():
    diff = DifficultyReport(branching=[
        Hotspot("(internal) Router._insertLeg", 39, "Router.sol:433"),
        Hotspot("CombinatorialModule._storeLegsFromMemory", 20, "M.sol:1092"),
        Hotspot("Router.inject", 30, "Router.sol:1"),                  # CUT's own external = subject -> skip
        Hotspot("(internal) Router._tiny", BRANCHING_MIN_PCT - 1, ""),  # below the floor -> dropped
    ])
    by = {c.function: c for c in detect_from([], diff, cut="Router").candidates}
    ins = by["Router._insertLeg"]
    assert "branching" in ins.signals and "branching" in ins.evidence.lower()
    assert "CombinatorialModule._storeLegsFromMemory" in by
    assert "Router.inject" not in by                                   # the CUT external subject is skipped
    assert not any("_tiny" in f for f in by)                          # below the floor is dropped


def test_is_reference_typename_gates_nondet():
    m = {}
    arr = {"nodeType": "ArrayTypeName", "baseType": {"nodeType": "ElementaryTypeName", "name": "uint256"}}
    assert _is_reference_typename(arr, m) is True                     # PositionId[] -> not NONDET-able
    assert _is_reference_typename({"nodeType": "ElementaryTypeName", "name": "uint256"}, m) is False
    assert _is_reference_typename({"nodeType": "ElementaryTypeName", "name": "bytes"}, m) is True
    assert _is_reference_typename({"nodeType": "Mapping"}, m) is True
    m = {"5": {"nodeType": "StructDefinition"}, "6": {"nodeType": "EnumDefinition"}}
    assert _is_reference_typename({"nodeType": "UserDefinedTypeName", "referencedDeclaration": 5}, m) is True   # struct
    assert _is_reference_typename({"nodeType": "UserDefinedTypeName", "referencedDeclaration": 6}, m) is False  # enum


def test_caller_boundaries_nondet_filter_keeps_value_void_containers():
    # leaf `_insertLeg` (array return) is called by `_storeLegs` (value return) and `_derive` (array return);
    # with the nondet_ok filter, only the value/void container `_storeLegs` is offered as a boundary.
    edges = {"C._storeLegs": {"C._insertLeg"}, "C._derive": {"C._insertLeg"}}
    facts = {
        "C._insertLeg": FnFacts("_insertLeg(...) -> PositionId[]", expressible=True, mutating=False, nondet_ok=False),
        "C._storeLegs": FnFacts("_storeLegs(...) -> ConditionId", expressible=True, mutating=False, nondet_ok=True),
        "C._derive": FnFacts("_derive(...) -> PositionId[]", expressible=True, mutating=False, nondet_ok=False),
    }
    bs = _caller_boundaries(edges, facts, "C._insertLeg", None, nondet=True)
    names = {b.function for b in bs}
    assert "C._storeLegs" in names and "C._derive" not in names


def test_path_count_value_parses_pow2_and_int():
    from summarization_detector.difficulty import _path_count_value
    assert _path_count_value("approx. 2^51") == 2.0 ** 51
    assert _path_count_value("200") == 200.0
    assert _path_count_value("1") == 1.0
    assert _path_count_value("n/a") == 0.0


def test_scan_max_path_count_keeps_worst_incl_list_values():
    from summarization_detector.difficulty import _scan_max_path_count
    tree = {"children": [
        {"label": "path count", "value": "1"},
        {"label": "call #1", "children": [{"label": "path count", "value": "approx. 2^51"}]},
        {"label": "path count", "value": ["200"]},          # list-valued (as some nodes serialize)
    ]}
    best = [0.0, ""]
    _scan_max_path_count(tree, best)
    assert best[1] == "approx. 2^51"


def test_detect_from_branching_evidence_carries_absolute_path_count():
    diff = DifficultyReport(branching=[Hotspot("(internal) C._loop", 40, "")], max_path_count="approx. 2^51")
    by = {c.function: c for c in detect_from([], diff, cut="C").candidates}
    assert "2^51" in by["C._loop"].evidence and "branching" in by["C._loop"].evidence.lower()


def test_caller_boundaries_nondet_ranks_view_pure_before_mutating():
    # both callers are value/void-return (NONDET-able), so both are offered — but the view/pure one ranks
    # first, since only it is unconditionally sound to `=> NONDET` (a mutating boundary drops writes).
    edges = {"C._pureWrap": {"C._loop"}, "C._mutWrap": {"C._loop"}}
    facts = {
        "C._loop": FnFacts("_loop(...) -> uint256[]", expressible=True, mutating=False, nondet_ok=False),
        "C._pureWrap": FnFacts("_pureWrap(...) -> uint256", expressible=True, mutating=False, nondet_ok=True),
        "C._mutWrap": FnFacts("_mutWrap(...) -> uint256", expressible=True, mutating=True, nondet_ok=True),
    }
    bs = _caller_boundaries(edges, facts, "C._loop", None, nondet=True)
    names = [b.function for b in bs]
    assert names[0] == "C._pureWrap" and bs[0].mutating is False       # unconditionally-sound boundary first
    assert any(b.function == "C._mutWrap" and b.mutating for b in bs)  # mutating one still offered (flagged)


def test_candidate_schema_parity_locks_typeddicts_to_dataclasses():
    # schema.py's TypedDicts are a hand-written mirror of the Candidate/Boundary dataclasses; this pins
    # them so they cannot silently drift (the "keep them in step" hazard). Also checks that to_dict's
    # default-pruning (NotRequired keys) matches the fields that actually carry a default.
    from dataclasses import MISSING, fields
    from summarization_detector.detect import Candidate, Boundary
    from summarization_detector.schema import HostileCandidate, HostileBoundary
    assert set(HostileCandidate.__annotations__) == {f.name for f in fields(Candidate)}
    assert set(HostileBoundary.__annotations__) == {f.name for f in fields(Boundary)}
    opt = {f.name for f in fields(Candidate)
           if f.default is not MISSING or f.default_factory is not MISSING}
    assert set(HostileCandidate.__optional_keys__) == opt          # NotRequired == fields with a default
    assert set(HostileBoundary.__optional_keys__) == set()         # Boundary has no defaults -> all required
