"""
OpenZeppelin Clones library usage detector.

Scans an already-parsed AST dict (as produced by SetupProver.generate_ast_graph from
all_asts.json: dict[relative_path -> dict[absolute_path -> dict[node_id -> node]]]) for calls
that create a new minimal-proxy clone via OpenZeppelin's Clones library -- both the direct
call form (Clones.clone(...)) and the `using Clones for address; addr.clone()` attached-call
form -- so dynamic_bound/dynamic_dispatch can default on in the base config: minimal-proxy
clones are only resolvable by the prover dynamically, not statically.
"""

from pathlib import Path
from typing import Any, Callable, Dict

from certora_autosetup.utils.scope import Scope

# Only members that actually deploy a new minimal-proxy instance need dynamic_bound/
# dynamic_dispatch. predictDeterministicAddress/fetchCloneArgs only compute
# addresses/args and don't by themselves require dynamic dispatch.
CLONE_CREATION_MEMBERS = {"clone", "cloneDeterministic", "cloneWithImmutableArgs"}
CLONES_LIBRARY_TYPESTRING = "type(library Clones)"


def detect_clones_usage(log_func: Callable, asts_data: Dict[str, Any], scope: Scope) -> bool:
    """
    Detect calls that create an OpenZeppelin Clones minimal-proxy instance, anywhere in the
    in-scope portion of the compiled AST.

    Args:
        log_func: Logging function (signature: log_func(message, level="INFO")).
        asts_data: Parsed all_asts.json, UNFILTERED by scope -- library declarations (e.g.
            under node_modules/@openzeppelin) are typically themselves out of scope but must
            still be resolvable for the `using X for address` attached-call path below.
        scope: Scope object; only call-site files are filtered through it.

    Returns:
        True if a Clones-creation call was found in an in-scope file, False otherwise.
    """
    log_func("Scanning AST for OpenZeppelin Clones library usage...")

    for relative_path, path_data in asts_data.items():
        if not scope.is_file_in_scope(Path(relative_path)):
            continue

        # id -> node index for THIS top-level relative_path's compilation unit only. Node ids
        # are unique across all of a compilation unit's absolute_path buckets but NOT globally
        # across other top-level relative_path entries in asts_data, so this index must be
        # rebuilt per relative_path rather than merged across the whole file -- otherwise a
        # colliding id from an unrelated compilation unit silently resolves to the wrong node.
        id_to_node: Dict[int, Dict[str, Any]] = {}
        for _absolute_path, nodes in path_data.items():
            for node in nodes.values():
                if isinstance(node, dict) and "id" in node:
                    id_to_node[node["id"]] = node

        def _is_clones_library_call(callee: Dict[str, Any]) -> bool:
            # Direct-call form: Clones.clone(...) -- base expression's static type IS the
            # library type.
            base = callee.get("expression", {})
            if base.get("typeDescriptions", {}).get("typeString") == CLONES_LIBRARY_TYPESTRING:
                return True

            # Attached-call form: using Clones for address; addr.clone() -- resolve the member
            # access's referencedDeclaration to the FunctionDefinition, then check ITS
            # enclosing library via the `scope` field.
            ref_id = callee.get("referencedDeclaration")
            if ref_id is None:
                return False
            func_def = id_to_node.get(ref_id)
            if not func_def or func_def.get("nodeType") != "FunctionDefinition":
                return False
            library_node = id_to_node.get(func_def.get("scope"))
            if not library_node or library_node.get("nodeType") != "ContractDefinition":
                return False
            return (
                library_node.get("contractKind") == "library"
                and library_node.get("name") == "Clones"
            )

        for absolute_path, nodes in path_data.items():
            try:
                abs_path_obj = Path(absolute_path)
                if abs_path_obj.is_absolute():
                    rel_path_for_scope = abs_path_obj.relative_to(scope.project_root.resolve())
                else:
                    rel_path_for_scope = abs_path_obj
            except ValueError:
                continue
            if not scope.is_file_in_scope(rel_path_for_scope):
                continue

            for _node_id, node in nodes.items():
                if not isinstance(node, dict) or node.get("nodeType") != "FunctionCall":
                    continue

                callee = node.get("expression", {})
                if (
                    callee.get("nodeType") != "MemberAccess"
                    or callee.get("memberName") not in CLONE_CREATION_MEMBERS
                ):
                    continue

                if _is_clones_library_call(callee):
                    log_func(
                        f"Clones library call detected: {relative_path} calls "
                        f"Clones.{callee.get('memberName')}(...)"
                    )
                    return True

    log_func("No OpenZeppelin Clones library usage found.")
    return False
