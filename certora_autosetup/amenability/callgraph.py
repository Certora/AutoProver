"""Static internal call graph of one compilation unit, plus its recursive clusters.

The prover's decompiler unfolds every recursive entry to a fixed depth
(`-recursionEntryLimit`) on every path, so a recursive cluster reached from
inside a loop multiplies the decompiled program out of the block/command budgets
before verification even starts. Finding those clusters needs a call graph; this
module builds one and nothing else (the scoring lives in signals/recursion.py).

Scoping rules that are load-bearing, not incidental:

- The graph is built PER COMPILATION UNIT (one outer key of the AST dump). Node
  ids are unique within a unit but collide across units — the same id names
  different functions in different units, so a cross-unit index silently produces
  wrong edges.
- Only edges the decompiler actually inlines are kept: internal/private calls,
  free-function calls, and modifier bodies. External calls and public/external
  library calls (DELEGATECALL) are separate programs to the decompiler and would
  fabricate cycles through interfaces.

Known limitations (all deliberate):

- Dynamic dispatch is expanded from a `virtual` declaration to the overrides that
  derive from the calling contract, except through `super.` (where the target is
  statically known and wholesale expansion invents cycles out of the `super.f()`
  idiom). Recursion realized only through an override that is not flagged `virtual`
  is invisible.
- Dispatch is resolved over the whole unit rather than per deployable contract, so
  two sibling contracts overriding hooks of a shared abstract base contribute their
  edges to one graph. A template-method pair whose siblings call back into the base
  therefore reads as a cycle even though no single instance can realize it. Fixing
  this means resolving each virtual call against one concrete contract's
  linearization and unioning the per-contract clusters.

- Yul-level recursion inside inline assembly is not modelled: Yul nodes carry no
  id, so they form a separate name-scoped world.
- Calls nested under an `UnknownNode` (a nodeType newer than the vendored schema)
  are skipped, like everywhere else in the typed AST layer.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from certora_autosetup.solidity_ast import (
    AstNode,
    ContractDefinition,
    DoWhileStatement,
    ForStatement,
    FunctionCall,
    FunctionCallOptions,
    FunctionDefinition,
    Identifier,
    MemberAccess,
    ModifierDefinition,
    SourceAst,
    WhileStatement,
    find_all,
)

LOOP_STATEMENTS = (ForStatement, WhileStatement, DoWhileStatement)

# Callable nodes the decompiler inlines into its caller.
CALLABLE_NODES = (FunctionDefinition, ModifierDefinition)

INLINED_VISIBILITIES = ("internal", "private")


@dataclass
class GraphNode:
    """One callable in the graph, with what evidence needs to point at it."""

    source_path: str
    label: str  # `Contract.function`, or the bare name for a free function
    byte_offset: int


@dataclass
class UnitGraph:
    """The internal call graph of one compilation unit."""

    nodes: dict[int, GraphNode] = field(default_factory=dict)
    # caller id -> callee ids
    callees: dict[int, set[int]] = field(default_factory=lambda: defaultdict(set))
    # edges whose call site is syntactically inside a loop
    loop_edges: set[tuple[int, int]] = field(default_factory=set)
    # public/external/constructor/fallback/receive members of deployable contracts
    entry_points: set[int] = field(default_factory=set)


@dataclass
class Cluster:
    """One recursive cluster (a strongly connected component, or a self-call)."""

    members: tuple[int, ...]
    loop_entered: bool  # an edge into or inside the cluster sits in a loop body
    reachable: bool  # reachable from a deployable contract's entry points
    entry_sites: int  # distinct callers outside the cluster that call into it

    @property
    def size(self) -> int:
        return len(self.members)


def _callee_expression(call: FunctionCall) -> AstNode:
    """The callee of a call, unwrapping one `f{value: v}(...)` options layer."""
    callee = call.expression
    if isinstance(callee, FunctionCallOptions):
        return callee.expression
    return callee


def _is_super_call(callee: AstNode) -> bool:
    return (
        isinstance(callee, MemberAccess)
        and isinstance(callee.expression, Identifier)
        and callee.expression.name == "super"
    )


def _is_entry_point(fn: FunctionDefinition, index: dict[int, AstNode]) -> bool:
    """True for a function callable from outside a deployable contract."""
    owner = index.get(fn.scope)
    if not (
        isinstance(owner, ContractDefinition)
        and owner.contractKind == "contract"
        and not owner.abstract
    ):
        return False
    return fn.visibility in ("public", "external") or fn.effective_kind in (
        "constructor",
        "fallback",
        "receive",
    )


def _label(definition: FunctionDefinition | ModifierDefinition, index: dict[int, AstNode]) -> str:
    """`Contract.name` for a member, the bare name for a free function."""
    name = definition.name
    if not name and isinstance(definition, FunctionDefinition):
        name = definition.effective_kind
    # ModifierDefinition carries no scope: an unowned definition keeps its bare name.
    scope = getattr(definition, "scope", None)
    owner = index.get(scope) if isinstance(scope, int) else None
    if isinstance(owner, ContractDefinition):
        return f"{owner.name}.{name}"
    return name


def _owning_contract(node: AstNode | None, index: dict[int, AstNode]) -> ContractDefinition | None:
    scope = getattr(node, "scope", None)
    owner = index.get(scope) if isinstance(scope, int) else None
    return owner if isinstance(owner, ContractDefinition) else None


def _dispatch_targets(
    base_id: int,
    caller_owner: ContractDefinition | None,
    overrides_of: dict[int, list[int]],
    index: dict[int, AstNode],
) -> set[int]:
    """Overrides a virtual call in `caller_owner` can actually dispatch to.

    An override is reachable only from code of a contract it derives from, so the
    candidate's linearization must contain the caller's contract. Without that test,
    sibling implementations of a shared abstract base get welded into one graph and
    the template-method pattern reads as a cycle. The walk is transitive: an override
    may itself be `virtual` and overridden further down the chain.
    """
    if caller_owner is None:
        return set()
    reachable: set[int] = set()
    stack = [base_id]
    seen = {base_id}
    while stack:
        for override_id in overrides_of.get(stack.pop(), ()):
            if override_id in seen:
                continue
            seen.add(override_id)
            stack.append(override_id)
            owner = _owning_contract(index.get(override_id), index)
            if owner is not None and caller_owner.id in owner.linearizedBaseContracts:
                reachable.add(override_id)
    return reachable


def _loop_call_ids(fn: AstNode) -> set[int]:
    """Ids of the call sites inside a loop body of `fn`."""
    return {
        call.id
        for loop in find_all(fn, LOOP_STATEMENTS)
        for call in find_all(loop, FunctionCall)
    }


def build_unit_graph(sources: Iterable[SourceAst]) -> UnitGraph:
    """Build the internal call graph of one compilation unit.

    `sources` are the parsed sources of a single unit; their prebuilt id indexes
    (`SourceAst.nodes`) are merged into the unit's declaration index, so resolving
    `referencedDeclaration` costs no extra traversal.
    """
    sources = list(sources)
    index: dict[int, AstNode] = {}
    for source in sources:
        index.update(source.nodes)

    # base declaration id -> ids of the functions overriding it, for dispatch expansion
    overrides_of: dict[int, list[int]] = defaultdict(list)
    for node in index.values():
        if isinstance(node, FunctionDefinition):
            for base_id in node.baseFunctions or ():
                overrides_of[base_id].append(node.id)

    graph = UnitGraph()
    for source in sources:
        if source.root is None:
            continue
        # find_all over the source root also picks up file-level free functions,
        # which contract-member iteration misses.
        for definition in find_all(source.root, CALLABLE_NODES):
            graph.nodes[definition.id] = GraphNode(
                source_path=source.source_path,
                label=_label(definition, index),
                byte_offset=definition.src_location.offset,
            )
            if isinstance(definition, FunctionDefinition) and _is_entry_point(definition, index):
                graph.entry_points.add(definition.id)
            for invocation in getattr(definition, "modifiers", []):
                # `baseConstructorSpecifier` invocations name a contract, not a modifier
                modifier_id = getattr(invocation.modifierName, "referencedDeclaration", None)
                if isinstance(modifier_id, int) and isinstance(
                    index.get(modifier_id), ModifierDefinition
                ):
                    graph.callees[definition.id].add(modifier_id)
            if definition.body is None:
                continue
            in_loop = _loop_call_ids(definition)
            for call in find_all(definition, FunctionCall):
                if call.kind != "functionCall":
                    continue  # a cast or struct constructor has no callee declaration
                callee = _callee_expression(call)
                if not isinstance(callee, (Identifier, MemberAccess)):
                    continue  # `new C(...)`, elementary-type conversions
                # Builtins carry negative ids and simply miss the index; error and
                # event "calls" resolve to non-callable declarations.
                referenced = callee.referencedDeclaration
                target = index.get(referenced) if referenced is not None else None
                if not isinstance(target, FunctionDefinition):
                    continue
                if isinstance(callee, MemberAccess) and target.visibility not in INLINED_VISIBILITIES:
                    continue  # external call or public-library delegatecall
                targets = [target.id]
                if target.virtual and not _is_super_call(callee):
                    targets.extend(_dispatch_targets(
                        target.id, _owning_contract(definition, index), overrides_of, index
                    ))
                for target_id in targets:
                    graph.callees[definition.id].add(target_id)
                    if call.id in in_loop:
                        graph.loop_edges.add((definition.id, target_id))
    return graph


def strongly_connected_components(callees: dict[int, set[int]]) -> list[list[int]]:
    """Tarjan's SCCs over an id->ids adjacency map (iterative: call graphs of
    generated code get deep enough to blow the interpreter's recursion limit)."""
    index_of: dict[int, int] = {}
    lowlink: dict[int, int] = {}
    on_stack: set[int] = set()
    stack: list[int] = []
    components: list[list[int]] = []
    counter = 0

    for root in list(callees):
        if root in index_of:
            continue
        # work items: (node, iterator over its successors, successor being returned from)
        work: list[tuple[int, list[int], int]] = [(root, sorted(callees.get(root, ())), 0)]
        index_of[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, successors, position = work[-1]
            if position < len(successors):
                work[-1] = (node, successors, position + 1)
                successor = successors[position]
                if successor not in index_of:
                    index_of[successor] = lowlink[successor] = counter
                    counter += 1
                    stack.append(successor)
                    on_stack.add(successor)
                    work.append((successor, sorted(callees.get(successor, ())), 0))
                elif successor in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[successor])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)
    return components


def recursive_clusters(
    callees: dict[int, set[int]],
    *,
    loop_edges: set[tuple[int, int]] | None = None,
    entry_points: set[int] | None = None,
) -> list[Cluster]:
    """Every recursive cluster of a call graph: each SCC of size > 1, plus each
    single node that calls itself. Nodes with no outgoing edges may be omitted from
    `callees`; unresolved callee ids are simply nodes with no successors.

    `reachable` is only ever True for a cluster reached from `entry_points`, so a
    unit with no deployable contract reports every cluster as unreachable — the
    caller damps rather than drops those, since "not provably live here" is not
    the same as dead.
    """
    loop_edges = loop_edges or set()
    reached = _reachable(callees, entry_points or set())
    clusters: list[Cluster] = []
    for component in strongly_connected_components(callees):
        members = set(component)
        if len(members) == 1:
            (only,) = members
            if only not in callees.get(only, ()):
                continue  # a plain node, not a self-call
        callers_in = {
            caller
            for caller, targets in callees.items()
            if caller not in members and targets & members
        }
        loop_entered = any(
            target in members and (caller in members or caller in callers_in)
            for caller, target in loop_edges
        )
        clusters.append(
            Cluster(
                members=tuple(sorted(members)),
                loop_entered=loop_entered,
                reachable=bool(members & reached),
                entry_sites=len(callers_in),
            )
        )
    return clusters


def _reachable(callees: dict[int, set[int]], entry_points: set[int]) -> set[int]:
    reached: set[int] = set()
    frontier = list(entry_points)
    while frontier:
        node = frontier.pop()
        if node in reached:
            continue
        reached.add(node)
        frontier.extend(callees.get(node, ()))
    return reached


def unit_clusters(graph: UnitGraph) -> list[Cluster]:
    """Recursive clusters of a built unit graph."""
    return recursive_clusters(
        graph.callees, loop_edges=graph.loop_edges, entry_points=graph.entry_points
    )


__all__ = [
    "Cluster",
    "GraphNode",
    "UnitGraph",
    "build_unit_graph",
    "recursive_clusters",
    "strongly_connected_components",
    "unit_clusters",
]
