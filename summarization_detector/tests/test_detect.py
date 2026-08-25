"""Summarization-target detector (detect.py). Fast, offline, no prover/scene:
 - scan_ast: a synthetic solc-AST fixture exercises span-attribution, the `abi.`-base guard (a non-abi
   `.encode()` is NOT hashing), source-file dedup, and the lib/ dependency flag.
 - detect_from: the three-signal fusion + the whole-vs-part (over_approx vs symbolic_model) classifier.
No real `.asts.json` is needed — scan_ast streams any JSON of the documented shape."""
import json
import tempfile
from pathlib import Path

from summarization_detector.detect import (
    scan_ast, detect_from, HashSignal, reachable_from_main, cone_weights, _touch_missing_imported_specs,
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
    """Three signals fuse per function; an external contract with >=2 hotspots -> symbolic_model, a lone
    output function -> over_approx. Dependency hashing is dropped by default."""
    diff = DifficultyReport(hotspots=[
        Hotspot("Oracle.getPrice", 40, "O.sol:10"),     # external (not CUT), 2 methods -> whole-contract
        Hotspot("Oracle.latestRound", 20, "O.sol:20"),
        Hotspot("C.mulThing", 55, "C.sol:5"),           # the CUT's own nonlinear math -> over_approx
    ])
    hs = [
        HashSignal("C.hashId", "C", "hashId", "pure", "internal", ("keccak256",), "src/C.sol", False),
        HashSignal("Dep.enc", "Dep", "enc", "pure", "internal", ("abi.encode",), "lib/x/Dep.sol", True),
    ]
    rep = detect_from(hs, diff, cut="C")
    by = {c.function: c for c in rep.candidates}

    assert by["Oracle.getPrice"].mode == "symbolic_model"      # external + >=2 methods -> whole contract
    assert "external" in by["Oracle.getPrice"].signals
    assert by["C.mulThing"].mode == "over_approx" and by["C.mulThing"].signals == ("nonlinear",)
    assert by["C.hashId"].mode == "over_approx" and "hashing" in by["C.hashId"].signals
    assert "Dep.enc" not in by                                  # dependency hashing filtered by default
    assert rep.candidates == sorted(rep.candidates, key=lambda c: c.score, reverse=True)   # ranked


def test_detect_from_drops_cvl_ghost_hotspots():
    # a CVL/ghost hotspot is an ALREADY-APPLIED summary — must never be a candidate (it IS the summary)
    diff = DifficultyReport(hotspots=[
        Hotspot("CVL/Ghost Function 'mulDivDownSummary(x,y,denominator)'", 86, ""),   # already a summary
        Hotspot("AmountConverter.getExpectedOut", 10, "contracts/AmountConverter.sol:134"),
    ])
    rep = detect_from([], diff, cut="Stonks")
    fns = {c.function for c in rep.candidates}
    assert "AmountConverter.getExpectedOut" in fns                                    # real math kept
    assert not any("Ghost" in f or "CVL" in f for f in fns)                           # ghost summary dropped


def test_detect_from_strips_internal_prefix_so_cut_own_fn_is_not_external():
    # the prover prefixes inlined hotspots with "(internal)"; without stripping, the CUT's own function
    # parses to contract "(internal) Stonks" != cut and is wrongly flagged a resolved-external.
    diff = DifficultyReport(hotspots=[Hotspot("(internal) Stonks.estimateTradeOutput", 12, "S.sol:387")])
    c = detect_from([], diff, cut="Stonks").candidates[0]
    assert c.function == "Stonks.estimateTradeOutput"       # (internal) marker stripped
    assert c.contract == "Stonks"                           # parses to the CUT
    assert c.signals == ("nonlinear",) and "external" not in c.signals   # CUT's own math, NOT external


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


def test_caller_boundaries_prefers_expressible_pure_callers():
    # leaf <- encodeFromData (opaque) <- mutClose (clean but MUTATING, hop2) <- pureFar (clean+pure, hop3)
    edges = {
        "L.encodeFromData": {"computeBaseHash"},
        "M.mutClose": {"L.encodeFromData"},
        "M.pureFar": {"M.mutClose"},
    }
    sigs = {                                        # (signature, expressible, has_return, mutating)
        "L.encodeFromData": ("encodeFromData(uint256, bytes) -> C", False, True, False),  # opaque bytes arg
        "M.mutClose": ("mutClose(PositionId[]) -> C", True, True, True),                   # clean but mutating
        "M.pureFar": ("pureFar(PositionId[]) -> C", True, True, False),                    # clean + pure
    }
    reach = {"computeBaseHash", "L.encodeFromData", "M.mutClose", "M.pureFar"}
    b = _caller_boundaries(edges, sigs, "computeBaseHash", reach)
    # pure+clean wins even though FARTHER; mutating-clean next; opaque last (view-preference beats distance)
    assert [x.function for x in b] == ["M.pureFar", "M.mutClose", "L.encodeFromData"]
    assert b[0].expressible and not b[0].mutating
    assert b[1].expressible and b[1].mutating
    assert not b[2].expressible


def test_caller_boundaries_filters_to_reachable():
    edges = {"C.caller": {"computeBaseHash"}}
    sigs = {"C.caller": ("caller(uint256) -> bytes32", True, True, False)}
    assert _caller_boundaries(edges, sigs, "computeBaseHash", reachable={"computeBaseHash"}) == []  # caller unreached
    assert _caller_boundaries(edges, sigs, "computeBaseHash", reachable=None)                       # no filter -> kept


def test_boundary_tags_distinguish_no_return_from_opaque_sig():
    from summarization_detector.detect import DetectionReport, Candidate, Boundary
    rep = DetectionReport(candidates=[Candidate(
        "leaf", "", ("hashing",), "over_approx", 10.0, "ev",
        boundaries=[
            # void (all value-type args, no return) — NOT opaque, just nothing to model
            Boundary("A.consumePermit", 1, "consumePermit(address, uint256)", expressible=False,
                     has_return=False, mutating=True),
            # has a return but an opaque `bytes` param — genuinely opaque signature
            Boundary("A.encodeFromData", 1, "encodeFromData(bytes) -> b32", expressible=False,
                     has_return=True, mutating=False),
        ])])
    lines = rep.format().splitlines()
    permit_line = next(ln for ln in lines if "consumePermit" in ln)
    enc_line = next(ln for ln in lines if "encodeFromData" in ln)
    assert "no return value" in permit_line and "opaque sig" not in permit_line   # void -> distinct tag
    assert "opaque sig" in enc_line                                               # opaque-type keeps its tag


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
        "V.previewDeposit", "V", ("nonlinear",), "over_approx", 100.0, "ev",
        boundaries=[Boundary("MathLib.mulDivDown", 2, "mulDivDown(uint256, uint256, uint256) -> uint256",
                             expressible=True, has_return=True, mutating=False, direction="down", shared=8)])])
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
