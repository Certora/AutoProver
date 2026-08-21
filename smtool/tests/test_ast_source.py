"""AST-backed source oracle (smtool.ast_source.function_source, exposed as the get_function tool): exact
bodies via `src` byte-ranges + callees via `referencedDeclaration`, streamed + cached. Tested on a
synthetic `.asts.json` mirroring the real schema dict[unit][file][id]=node — no real source involved."""
import json
from smtool import ast_source


def test_callee_ids_and_arity_helpers():
    node = {"nodeType": "FunctionDefinition", "parameters": {"parameters": [{}, {}]},
            "body": {"statements": [
                {"nodeType": "FunctionCall", "expression": {"referencedDeclaration": 7}},
                {"nodeType": "FunctionCall", "expression": {"referencedDeclaration": -18}},   # builtin -> dropped
                {"x": [{"nodeType": "FunctionCall", "expression": {"referencedDeclaration": 9}}]}]}}
    assert ast_source._arity(node) == 2
    assert sorted(ast_source._callee_ids(node)) == [7, 9]      # nested found, negative builtin excluded


def _write_scene(tmp_path, sol, asts):
    (tmp_path / "C.sol").write_text(sol)
    bd = tmp_path / ".certora_internal" / "BUILD"
    bd.mkdir(parents=True)
    (bd / ".asts.json").write_text(json.dumps(asts))
    ast_source._INDEX_CACHE.clear()                            # fresh index for this scene


def test_function_source_returns_body_and_callees(tmp_path):
    sol = "function act(uint256 x) external { helper(x); }\nfunction helper(uint256 x) internal { x; }\n"
    a0, a1 = sol.index("function act"), sol.index("}") + 1
    h0, h1 = sol.index("function helper"), sol.rindex("}") + 1
    asts = {"unit": {"C.sol": {
        "1": {"nodeType": "FunctionDefinition", "id": 1, "name": "act", "src": f"{a0}:{a1 - a0}:0",
              "visibility": "external", "implemented": True, "parameters": {"parameters": [{}]},
              "body": {"statements": [{"nodeType": "FunctionCall",
                                       "expression": {"referencedDeclaration": 2}}]}},
        "2": {"nodeType": "FunctionDefinition", "id": 2, "name": "helper", "src": f"{h0}:{h1 - h0}:0",
              "visibility": "internal", "implemented": True, "parameters": {"parameters": [{}]},
              "body": {"statements": []}}}}}
    _write_scene(tmp_path, sol, asts)
    out = ast_source.function_source(str(tmp_path), "act")
    assert "### act" in out and "function act(uint256 x)" in out          # exact body, sliced by src
    assert "helper (C.sol)" in out and "get_function" in out             # callee listed, expand hint
    # helper is fetched on demand, one hop at a time (NOT inlined here)
    assert "function helper(uint256 x)" not in out
    assert ast_source.function_source(str(tmp_path), "helper") is not None


def test_function_source_prefers_implemented_over_interface(tmp_path):
    sol = "function act(uint256 x) external { x; }\n"
    asts = {"u": {"C.sol": {
        "1": {"nodeType": "FunctionDefinition", "id": 1, "name": "act", "src": f"0:{len(sol)-1}:0",
              "visibility": "external", "implemented": True, "parameters": {"parameters": [{}]}, "body": {}},
        "2": {"nodeType": "FunctionDefinition", "id": 2, "name": "act", "src": "0:5:0",
              "visibility": "external", "implemented": False, "parameters": {"parameters": [{}]}, "body": {}}}}}
    _write_scene(tmp_path, sol, asts)
    out = ast_source.function_source(str(tmp_path), "act")
    assert out and "function act" in out                                 # the implemented def, not the decl


def test_function_source_missing_returns_none(tmp_path):
    _write_scene(tmp_path, "function act() external {}\n",
                 {"u": {"C.sol": {"1": {"nodeType": "FunctionDefinition", "id": 1, "name": "act",
                                        "src": "0:26:0", "implemented": True,
                                        "parameters": {"parameters": []}, "body": {}}}}})
    assert ast_source.function_source(str(tmp_path), "nope") is None      # -> caller falls back to get_file


def test_function_source_no_asts_returns_none(tmp_path):
    ast_source._INDEX_CACHE.clear()
    assert ast_source.function_source(str(tmp_path), "act") is None       # no .asts.json -> None
