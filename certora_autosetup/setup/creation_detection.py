"""
Contract-creation detector.

Scans an already-parsed AST dict (loaded from all_asts.json: dict[relative_path ->
dict[absolute_path -> dict[node_id -> node]]]) for contract creation reachable from in-scope
code, so dynamic_bound/dynamic_dispatch can default on in the base config. Detection is by
creation *instruction*, not by library name, so any minimal-proxy/factory library (OpenZeppelin
Clones, Solady LibClone, clones-with-immutable-args, CREATE3 wrappers, ...) is covered without
enumerating them. Two creation kinds are distinguished because they need different flags:

- Yul ``create``/``create2`` in inline assembly: the creation bytecode is assembled at runtime,
  so the prover cannot resolve calls on the created instance statically -- these need both
  --dynamic_bound and --dynamic_dispatch.
- ``new C()`` (a NewExpression of a contract type): the creation bytecode is C's, so under
  --dynamic_bound the prover resolves the instance to a clone of C and calls on it stay
  statically resolved -- only --dynamic_bound is needed.

Mechanics, per compilation unit (one top-level relative_path bucket): every
FunctionDefinition/ModifierDefinition/ContractDefinition is checked for direct creation over
its full embedded subtree (contracts thereby cover creation living outside any function:
state-variable initializers and inheritance-specifier base-constructor arguments), creation
kinds are then propagated up the intra-unit call graph (referencedDeclaration edges from call
sites and modifier invocations) to a fixpoint, and the unit's answer is the union over nodes
in in-scope files -- plus every base contract an in-scope contract linearizes over (via
linearizedBaseContracts), since inherited entry points execute as part of the verified
contract. This reachability step is what makes out-of-scope creation libraries
(node_modules/@openzeppelin, solady, ...) count when, and only when, in-scope code can reach
them.

Known under-approximations: calls through internal function pointers carry no
referencedDeclaration to the target, so creation reachable only that way is missed (inherent
to any static referencedDeclaration call graph); and solc < 0.6 emits inline assembly as a raw
source string (no structured Yul AST), so raw creates there are invisible -- a warning is
logged when such a node is seen.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Set, Tuple

from certora_autosetup.utils.scope import Scope

YUL_CREATE_OPCODES = {"create", "create2"}

# Node types that anchor the call graph. Functions and modifiers own executable bodies;
# contracts additionally own the constructor-time code that lives outside any of them
# (state-variable initializers, inheritance-specifier base-constructor arguments) -- a
# ContractDefinition's embedded subtree covers all of its members, so a contract node
# aggregates everything the contract can execute.
DEFINITION_NODE_TYPES = {"FunctionDefinition", "ModifierDefinition", "ContractDefinition"}


@dataclass
class CreationUsage:
    """Which contract-creation kinds are reachable from in-scope code."""

    new_expression: bool = False  # typed `new C()` -- creation bytecode statically known
    raw_create: bool = False  # Yul create/create2 -- creation bytecode assembled at runtime

    @property
    def found(self) -> bool:
        return self.new_expression or self.raw_create

    @property
    def complete(self) -> bool:
        """Both kinds seen -- nothing more to learn, scanning can stop early."""
        return self.new_expression and self.raw_create

    def merge(self, other: "CreationUsage") -> None:
        self.new_expression = self.new_expression or other.new_expression
        self.raw_create = self.raw_create or other.raw_create


def _walk(node: Any) -> Iterator[Dict[str, Any]]:
    """Yield every dict in an embedded AST subtree, including Yul sub-ASTs (whose nodes carry
    no ids and therefore only exist embedded inside their InlineAssembly node). Iterative:
    machine-generated code can nest expressions deeper than Python's recursion limit."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _analyze_definition(definition: Dict[str, Any]) -> Tuple[CreationUsage, Set[int], bool]:
    """One pass over a definition's subtree: which creation kinds it performs directly, the
    referencedDeclaration ids of everything it calls (call sites and modifier invocations),
    and whether it contains legacy unstructured inline assembly (solc < 0.6: a raw source
    string under `operations` instead of a Yul AST) that raw-create scanning cannot see."""
    usage = CreationUsage()
    callees: Set[int] = set()
    has_legacy_assembly = False
    for sub in _walk(definition):
        node_type = sub.get("nodeType")
        if node_type == "YulFunctionCall":
            if sub.get("functionName", {}).get("name") in YUL_CREATE_OPCODES:
                usage.raw_create = True
        elif node_type == "InlineAssembly":
            if "AST" not in sub:
                has_legacy_assembly = True
        elif node_type == "NewExpression":
            # `new C()` has a UserDefinedTypeName; `new uint[](n)` / `new bytes(n)` have an
            # ArrayTypeName and allocate memory, not a contract.
            if sub.get("typeName", {}).get("nodeType") == "UserDefinedTypeName":
                usage.new_expression = True
        elif node_type == "FunctionCall":
            callee = sub.get("expression", {})
            if callee.get("nodeType") == "FunctionCallOptions":  # f{value: ...}(...)
                callee = callee.get("expression", {})
            ref = callee.get("referencedDeclaration")
            if ref is not None:
                callees.add(ref)
        elif node_type == "ModifierInvocation":
            ref = sub.get("modifierName", {}).get("referencedDeclaration")
            if ref is not None:
                callees.add(ref)
    return usage, callees, has_legacy_assembly


def _relative_to_project(absolute_path: str, scope: Scope) -> Optional[Path]:
    """Map an AST absolute_path bucket to a project-relative path for scope checks; None for
    paths outside the project root (they can never be in scope)."""
    path_obj = Path(absolute_path)
    if not path_obj.is_absolute():
        return path_obj
    try:
        return path_obj.relative_to(scope.project_root.resolve())
    except ValueError:
        return None


def _scan_unit(
    log_func: Callable, relative_path: str, path_data: Dict[str, Any], scope: Scope
) -> CreationUsage:
    """Detect creation reachable from in-scope definitions of one compilation unit."""
    # id -> node index for THIS top-level relative_path's compilation unit only. Node ids are
    # unique across all of a compilation unit's absolute_path buckets but NOT globally across
    # other top-level relative_path entries in asts_data, so this index must be rebuilt per
    # relative_path rather than merged across the whole file -- otherwise a colliding id from
    # an unrelated compilation unit silently resolves to the wrong node.
    id_to_node: Dict[int, Dict[str, Any]] = {}
    in_scope_ids: Set[int] = set()
    for absolute_path, nodes in path_data.items():
        rel_path = _relative_to_project(absolute_path, scope)
        bucket_in_scope = rel_path is not None and scope.is_file_in_scope(rel_path)
        for node in nodes.values():
            if isinstance(node, dict) and "id" in node:
                id_to_node[node["id"]] = node
                if bucket_in_scope:
                    in_scope_ids.add(node["id"])

    # Direct creation kinds + call edges per definition.
    reachable: Dict[int, CreationUsage] = {}
    calls: Dict[int, Set[int]] = {}
    legacy_assembly_warned = False
    for node_id, node in id_to_node.items():
        if node.get("nodeType") in DEFINITION_NODE_TYPES:
            reachable[node_id], calls[node_id], has_legacy_assembly = _analyze_definition(node)
            if has_legacy_assembly and not legacy_assembly_warned:
                log_func(
                    f"Inline assembly without a structured Yul AST (solc < 0.6) in "
                    f"{relative_path}; raw create/create2 in it cannot be detected",
                    "WARNING",
                )
                legacy_assembly_warned = True

    # Fixpoint: a definition reaches every creation kind its callees reach. The call graph may
    # have cycles (recursion), so iterate until stable rather than topologically.
    changed = True
    while changed:
        changed = False
        for definition_id, callee_ids in calls.items():
            usage = reachable[definition_id]
            for callee_id in callee_ids:
                callee_usage = reachable.get(callee_id)
                if callee_usage is None:
                    continue
                if (callee_usage.new_expression and not usage.new_expression) or (
                    callee_usage.raw_create and not usage.raw_create
                ):
                    usage.merge(callee_usage)
                    changed = True

    # Roots whose reachability counts: nodes in in-scope files, plus every base contract an
    # in-scope contract linearizes over -- inherited entry points (and base constructor-time
    # code) run as part of the verified contract even though their defining file may be out of
    # scope. A base's contract node aggregates all of its members, so adding it suffices.
    root_ids: Set[int] = {definition_id for definition_id in reachable if definition_id in in_scope_ids}
    for node_id, node in id_to_node.items():
        if node.get("nodeType") == "ContractDefinition" and node_id in in_scope_ids:
            for base_id in node.get("linearizedBaseContracts", []):
                if base_id in reachable:
                    root_ids.add(base_id)

    unit_usage = CreationUsage()
    for root_id in root_ids:
        root_usage = reachable[root_id]
        if root_usage.found and not (
            (not root_usage.new_expression or unit_usage.new_expression)
            and (not root_usage.raw_create or unit_usage.raw_create)
        ):
            root = id_to_node[root_id]
            kinds = [
                kind
                for kind, present in (
                    ("assembly create/create2", root_usage.raw_create),
                    ("new-expression", root_usage.new_expression),
                )
                if present
            ]
            log_func(
                f"Contract creation ({', '.join(kinds)}) reachable from "
                f"{relative_path}: {root.get('name') or '<unnamed>'}"
            )
            unit_usage.merge(root_usage)
        if unit_usage.complete:
            break
    return unit_usage


def detect_contract_creation(log_func: Callable, asts_data: Dict[str, Any], scope: Scope) -> CreationUsage:
    """
    Detect contract creation (typed `new` and raw assembly create/create2) reachable from the
    in-scope portion of the compiled AST.

    Args:
        log_func: Logging function (signature: log_func(message, level="INFO")).
        asts_data: Parsed all_asts.json, UNFILTERED by scope -- creation-library definitions
            (e.g. under node_modules/@openzeppelin or solady) are typically themselves out of
            scope but must still be resolvable as call-graph targets.
        scope: Scope object; only the defining files of call-graph roots are filtered through it.

    Returns:
        CreationUsage saying which creation kinds were found.
    """
    log_func("Scanning AST for contract creation (new / assembly create/create2)...")

    total = CreationUsage()
    for relative_path, path_data in asts_data.items():
        if not scope.is_file_in_scope(Path(relative_path)):
            continue
        total.merge(_scan_unit(log_func, relative_path, path_data, scope))
        if total.complete:
            break

    if not total.found:
        log_func("No contract creation found in in-scope code.")
    return total
